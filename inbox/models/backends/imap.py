from datetime import datetime

from sqlalchemy import (Column, Integer, BigInteger, Boolean, Enum,
                        ForeignKey, Index, String, desc)
from sqlalchemy.schema import UniqueConstraint
from sqlalchemy.orm import relationship, backref
from sqlalchemy.sql.expression import false
from inbox.sqlalchemy_ext.util import (LittleJSON, JSON, MutableDict)
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.orm import object_session

from inbox.log import get_logger
log = get_logger()
from inbox.models.base import MailSyncBase
from inbox.models.account import Account
from inbox.models.thread import Thread
from inbox.models.message import Message
from inbox.models.folder import Folder
from inbox.models.mixins import HasRunState
from inbox.models.label import Label
from inbox.util.misc import cleanup_subject

PROVIDER = 'imap'


class ImapAccount(Account):
    id = Column(Integer, ForeignKey(Account.id, ondelete='CASCADE'),
                primary_key=True)

    _imap_server_host = Column(String(255), nullable=True)
    _imap_server_port = Column(Integer, nullable=False, server_default='993')

    _smtp_server_host = Column(String(255), nullable=True)
    _smtp_server_port = Column(Integer, nullable=False, server_default='587')

    @property
    def imap_endpoint(self):
        if self._imap_server_host is not None:
            return (self._imap_server_host, self._imap_server_port)
        else:
            return self.provider_info['imap']

    @imap_endpoint.setter
    def imap_endpoint(self, endpoint):
        host, port = endpoint
        self._imap_server_host = host
        self._imap_server_port = int(port)

    @property
    def smtp_endpoint(self):
        if self._smtp_server_host is not None:
            return (self._smtp_server_host, self._smtp_server_port)
        else:
            return self.provider_info['smtp']

    @smtp_endpoint.setter
    def smtp_endpoint(self, endpoint):
        host, port = endpoint
        self._smtp_server_host = host
        self._smtp_server_port = int(port)

    __mapper_args__ = {'polymorphic_identity': 'imapaccount'}


class ImapUid(MailSyncBase):
    """
    Maps UIDs to their IMAP folders and per-UID flag metadata.
    This table is used solely for bookkeeping by the IMAP mail sync backends.

    """
    account_id = Column(ForeignKey(ImapAccount.id, ondelete='CASCADE'),
                        nullable=False)
    account = relationship(ImapAccount)

    message_id = Column(Integer, ForeignKey(Message.id, ondelete='CASCADE'),
                        nullable=False)
    message = relationship(Message, backref=backref('imapuids',
                                                    passive_deletes=True))
    msg_uid = Column(BigInteger, nullable=False, index=True)

    folder_id = Column(Integer, ForeignKey(Folder.id, ondelete='CASCADE'),
                       nullable=False)
    # We almost always need the folder name too, so eager load by default.
    folder = relationship(Folder, lazy='joined',
                          backref=backref('imapuids', passive_deletes=True))

    labels = association_proxy(
        'labelitems', 'label',
        creator=lambda label: LabelItem(label=label))

    # Flags #
    # Message has not completed composition (marked as a draft).
    is_draft = Column(Boolean, server_default=false(), nullable=False)
    # Message has been read
    is_seen = Column(Boolean, server_default=false(), nullable=False)
    # Message is "flagged" for urgent/special attention
    is_flagged = Column(Boolean, server_default=false(), nullable=False)
    # session is the first session to have been notified about this message
    is_recent = Column(Boolean, server_default=false(), nullable=False)
    # Message has been answered
    is_answered = Column(Boolean, server_default=false(), nullable=False)
    # things like: ['$Forwarded', 'nonjunk', 'Junk']
    extra_flags = Column(LittleJSON, default=[], nullable=False)
    # labels (Gmail-specific)
    # TO BE DEPRECATED
    g_labels = Column(JSON, default=lambda: [], nullable=True)

    def update_flags(self, new_flags):
        """
        Sets flag and g_labels values based on the new_flags and x_gm_labels
        parameters. Returns True if any values have changed compared to what we
        previously stored.

        """
        changed = False
        new_flags = set(new_flags)
        col_for_flag = {
            u'\\Draft': 'is_draft',
            u'\\Seen': 'is_seen',
            u'\\Recent': 'is_recent',
            u'\\Answered': 'is_answered',
            u'\\Flagged': 'is_flagged',
        }
        for flag, col in col_for_flag.iteritems():
            prior_flag_value = getattr(self, col)
            new_flag_value = flag in new_flags
            if prior_flag_value != new_flag_value:
                changed = True
                setattr(self, col, new_flag_value)
            new_flags.discard(flag)
        extra_flags = sorted(new_flags)
        if extra_flags != self.extra_flags:
            changed = True
        self.extra_flags = extra_flags
        return changed

    def update_labels(self, new_labels):
        # TODO(emfree): This is all mad complicated. Simplify if possible?

        # Gmail IMAP doesn't use the normal IMAP \\Draft flag. Silly Gmail
        # IMAP.
        self.is_draft = '\\Draft' in new_labels
        self.is_starred = '\\Starred' in new_labels

        category_map = {
            '\\Inbox': 'inbox',
            '\\Important': 'important',
            '\\Sent': 'sent'
        }

        remote_labels = set()
        for label in new_labels:
            if label in ('\\Draft', '\\Starred'):
                continue
            elif label in category_map:
                remote_labels.add((category_map[label], category_map[label]))
            else:
                remote_labels.add((label, None))

        local_labels = {(l.name, l.canonical_name) for l in self.labels}

        remove = local_labels - remote_labels
        add = remote_labels - local_labels

        with object_session(self).no_autoflush as session:
            for name, canonical_name in remove:
                label = Label.find_or_create(session, self.account, name,
                                             canonical_name)
                self.labels.remove(label)

            for name, canonical_name in add:
                label = Label.find_or_create(session, self.account, name,
                                             canonical_name)
                self.labels.add(label)

    @property
    def namespace(self):
        return self.imapaccount.namespace

    @property
    def categories(self):
        categories = set([l.category for l in self.labels])
        categories.add(self.folder.category)
        return categories

    __table_args__ = (UniqueConstraint('folder_id', 'msg_uid', 'account_id',),)

# make pulling up all messages in a given folder fast
Index('account_id_folder_id', ImapUid.account_id, ImapUid.folder_id)


class ImapFolderInfo(MailSyncBase):
    """
    Per-folder UIDVALIDITY and (if applicable) HIGHESTMODSEQ.

    If the UIDVALIDITY value changes, it indicates that all UIDs for messages
    in the folder need to be thrown away and resynced.

    These values come from the IMAP STATUS or SELECT commands.

    See http://tools.ietf.org/html/rfc3501#section-2.3.1.1 for more info
    on UIDVALIDITY, and http://tools.ietf.org/html/rfc4551 for more info on
    HIGHESTMODSEQ.

    """
    account_id = Column(ForeignKey(ImapAccount.id, ondelete='CASCADE'),
                        nullable=False)
    account = relationship(ImapAccount)
    folder_id = Column(Integer, ForeignKey('folder.id', ondelete='CASCADE'),
                       nullable=False)
    # We almost always need the folder name too, so eager load by default.
    folder = relationship('Folder', lazy='joined',
                          backref=backref('imapfolderinfo',
                                          passive_deletes=True))
    uidvalidity = Column(BigInteger, nullable=False)
    # Invariant: the local datastore for this folder has always incorporated
    # remote changes up to _at least_ this modseq (we can't guarantee that we
    # haven't incorporated later changes too, since IMAP doesn't provide a true
    # transactional interface).
    #
    # Note that some IMAP providers do not support the CONDSTORE extension, and
    # therefore will not use this field.
    highestmodseq = Column(BigInteger, nullable=True)

    __table_args__ = (UniqueConstraint('account_id', 'folder_id'),)


def _choose_existing_thread_for_gmail(message, db_session):
    """
    For Gmail, determine if `message` should be added to an existing thread
    based on the value of `g_thrid`. If so, return the existing ImapThread
    object; otherwise return None.

    If a thread in Gmail (as identified by g_thrid) is split among multiple
    Inbox threads, try to choose which thread to put the new message in based
    on the In-Reply-To header. If that doesn't succeed because the In-Reply-To
    header is missing or doesn't match existing synced messages, return the
    most recent thread.

    """
    # TODO(emfree): also use the References header, or better yet, change API
    # semantics so that we don't have to do this at all.
    prior_threads = db_session.query(ImapThread).filter_by(
        g_thrid=message.g_thrid, namespace_id=message.namespace_id). \
        order_by(desc(ImapThread.recentdate)).all()
    if not prior_threads:
        return None
    if len(prior_threads) == 1:
        return prior_threads[0]
    if not message.in_reply_to:
        # If no header, add the new message to the most recent thread.
        return prior_threads[0]
    for prior_thread in prior_threads:
        prior_message_ids = [m.message_id_header for m in
                             prior_thread.messages]
        if message.in_reply_to in prior_message_ids:
            return prior_thread

    return prior_threads[0]


class ImapThread(Thread):
    """ TODO: split into provider-specific classes. """
    id = Column(Integer, ForeignKey(Thread.id, ondelete='CASCADE'),
                primary_key=True)

    # Only on messages from Gmail
    #
    # Gmail documents X-GM-THRID as 64-bit unsigned integer. Unique across
    # an account but not necessarily globally unique. The same message sent
    # to multiple users *may* have the same X-GM-THRID, but usually won't.
    g_thrid = Column(BigInteger, nullable=True, index=True, unique=False)

    @classmethod
    def from_gmail_message(cls, session, namespace, message):
        """
        Threads are broken solely on Gmail's X-GM-THRID for now. (Subjects
        are not taken into account, even if they change.)

        Returns the updated or new thread, and adds the message to the thread.
        Doesn't commit.

        """
        if message.thread is not None:
            # If this message *already* has a thread associated with it, just
            # update its g_thrid value.
            message.thread.g_thrid = message.g_thrid
            return message.thread
        if message.g_thrid is not None:
            thread = _choose_existing_thread_for_gmail(message, session)
            if thread is None:
                thread = cls(subject=message.subject, g_thrid=message.g_thrid,
                             recentdate=message.received_date,
                             namespace=namespace,
                             subjectdate=message.received_date,
                             snippet=message.snippet)
        return thread

    @classmethod
    def from_imap_message(cls, session, namespace, message):
        if message.thread is not None:
            # If this message *already* has a thread associated with it, don't
            # create a new one.
            return message.thread
        clean_subject = cleanup_subject(message.subject)
        thread = cls(subject=clean_subject, recentdate=message.received_date,
                     namespace=namespace, subjectdate=message.received_date,
                     snippet=message.snippet)
        return thread

    __mapper_args__ = {'polymorphic_identity': 'imapthread'}


class ImapFolderSyncStatus(MailSyncBase, HasRunState):
    """ Per-folder status state saving for IMAP folders. """
    account_id = Column(ForeignKey(ImapAccount.id, ondelete='CASCADE'),
                        nullable=False)
    account = relationship(ImapAccount,
                           backref=backref('foldersyncstatuses',
                                           passive_deletes=True))

    folder_id = Column(Integer, ForeignKey('folder.id', ondelete='CASCADE'),
                       nullable=False)
    # We almost always need the folder name too, so eager load by default.
    folder = relationship('Folder', lazy='joined', backref=backref(
        'imapsyncstatus', passive_deletes=True))

    # see state machine in mailsync/backends/imap/imap.py
    state = Column(Enum('initial', 'initial uidinvalid',
                   'poll', 'poll uidinvalid', 'finish'),
                   server_default='initial', nullable=False)

    # stats on messages downloaded etc.
    _metrics = Column(MutableDict.as_mutable(JSON), default={}, nullable=True)

    @property
    def metrics(self):
        status = dict(name=self.folder.name, state=self.state)
        status.update(self._metrics or {})

        return status

    def start_sync(self):
        self._metrics = dict(run_state='running',
                             sync_start_time=datetime.utcnow())

    def stop_sync(self):
        self._metrics['run_state'] = 'stopped'
        self._metrics['sync_end_time'] = datetime.utcnow()

    def kill_sync(self, error=None):
        self._metrics['run_state'] = 'killed'
        self._metrics['sync_end_time'] = datetime.utcnow()
        self._metrics['sync_error'] = error

    @property
    def is_killed(self):
        return self._metrics.get('run_state') == 'killed'

    def update_metrics(self, metrics):
        sync_status_metrics = ['remote_uid_count', 'delete_uid_count',
                               'update_uid_count', 'download_uid_count',
                               'uid_checked_timestamp',
                               'num_downloaded_since_timestamp',
                               'queue_checked_at', 'percent']

        assert isinstance(metrics, dict)
        for k in metrics.iterkeys():
            assert k in sync_status_metrics, k

        if self._metrics is not None:
            self._metrics.update(metrics)
        else:
            self._metrics = metrics

    @property
    def sync_enabled(self):
        # sync is enabled if the folder's run bit is set, and the account's
        # run bit is set. (this saves us needing to reproduce account-state
        # transition logic on the folder level, and gives us a comparison bit
        # against folder heartbeats.)
        return self.sync_should_run and self.account.sync_should_run

    __table_args__ = (UniqueConstraint('account_id', 'folder_id'),)


class LabelItem(MailSyncBase):
    """ Mapping between imapuids and labels. """
    imapuid_id = Column(Integer, ForeignKey(ImapUid.id, ondelete='CASCADE'),
                        nullable=False)
    imapuid = relationship(
        'ImapUid',
        backref=backref('labelitems',
                        collection_class=set,
                        cascade='all, delete-orphan'))

    label_id = Column(Integer, ForeignKey(Label.id, ondelete='CASCADE'),
                      nullable=False)
    label = relationship(
        Label,
        backref=backref('labelitems',
                        cascade='all, delete-orphan',
                        lazy='dynamic'))

    @property
    def namespace(self):
        return self.label.namespace

Index('imapuid_label_ids', LabelItem.imapuid_id, LabelItem.label_id)
