import sys
import pytz
import arrow
import traceback
import icalendar
from datetime import datetime, date
import icalendar
from icalendar import Calendar as iCalendar

from flanker import mime
from html2text import html2text
from util import serialize_datetime
from timezones import timezones_table
from inbox.models.event import Event, EVENT_STATUSES
from inbox.events.util import MalformedEventError
from inbox.util.addr import canonicalize_address
from inbox.models.action_log import schedule_action

from inbox.log import get_logger
log = get_logger()


STATUS_MAP = {'NEEDS-ACTION': 'noreply',
              'ACCEPTED': 'yes',
              'DECLINED': 'no',
              'TENTATIVE': 'maybe'}
INVERTED_STATUS_MAP = {value: key for key, value in STATUS_MAP.iteritems()}


def events_from_ics(namespace, calendar, ics_str):
    try:
        cal = iCalendar.from_ical(ics_str)
    except (ValueError, IndexError, KeyError):
        raise MalformedEventError()

    events = dict(invites=[], rsvps=[])

    # See: https://tools.ietf.org/html/rfc5546#section-3.2
    calendar_method = None

    for component in cal.walk():
        if component.name == "VCALENDAR":
            calendar_method = component.get('method')

        if component.name == "VTIMEZONE":
            tzname = component.get('TZID')
            assert tzname in timezones_table,\
                "Non-UTC timezone should be in table"

        if component.name == "VEVENT":
            # Make sure the times are in UTC.
            try:
                original_start = component.get('dtstart').dt
                original_end = component.get('dtend').dt
            except AttributeError:
                raise MalformedEventError("Event lacks start and/or end time")

            start = original_start
            end = original_end
            original_start_tz = None

            if isinstance(start, datetime) and isinstance(end, datetime):
                all_day = False
                original_start_tz = str(original_start.tzinfo)

                # icalendar doesn't parse Windows timezones yet
                # (see: https://github.com/collective/icalendar/issues/44)
                # so we look if the timezone isn't in our Windows-TZ
                # to Olson-TZ table.
                if original_start.tzinfo is None:
                    tzid = component.get('dtstart').params.get('TZID', None)
                    assert tzid in timezones_table,\
                        "Non-UTC timezone should be in table"

                    corresponding_tz = timezones_table[tzid]
                    original_start_tz = corresponding_tz

                    local_timezone = pytz.timezone(corresponding_tz)
                    start = local_timezone.localize(original_start)

                if original_end.tzinfo is None:
                    tzid = component.get('dtend').params.get('TZID', None)
                    assert tzid in timezones_table,\
                        "Non-UTC timezone should be in table"

                    corresponding_tz = timezones_table[tzid]
                    local_timezone = pytz.timezone(corresponding_tz)
                    end = local_timezone.localize(original_end)

            elif isinstance(start, date) and isinstance(end, date):
                all_day = True
                start = arrow.get(start)
                end = arrow.get(end)

            # Get the last modification date.
            # Exchange uses DtStamp, iCloud and Gmail LAST-MODIFIED.
            last_modified_tstamp = component.get('dtstamp')
            last_modified = None
            if last_modified_tstamp is not None:
                # This is one surprising instance of Exchange doing
                # the right thing by giving us an UTC timestamp. Also note that
                # Google calendar also include the DtStamp field, probably to
                # be a good citizen.
                if last_modified_tstamp.dt.tzinfo is not None:
                    last_modified = last_modified_tstamp.dt
                else:
                    raise NotImplementedError("We don't support arcane Windows"
                                              " timezones in timestamps yet")
            else:
                # Try to look for a LAST-MODIFIED element instead.
                # Note: LAST-MODIFIED is always in UTC.
                # http://www.kanzaki.com/docs/ical/lastModified.html
                last_modified = component.get('last-modified').dt
                assert last_modified is not None, \
                    "Event should have a DtStamp or LAST-MODIFIED timestamp"

            title = None
            summaries = component.get('summary', [])
            if not isinstance(summaries, list):
                summaries = [summaries]

            if summaries != []:
                title = " - ".join(summaries)

            description = component.get('description')
            if description is not None:
                description = unicode(description)

            event_status = component.get('status')
            if event_status is not None:
                event_status = event_status.lower()
            else:
                # Some providers (e.g: iCloud) don't use the status field.
                # Instead they use the METHOD field to signal cancellations.
                method = component.get('method')
                if method and method.lower() == 'cancel':
                    event_status = 'cancelled'
                elif calendar_method and calendar_method.lower() == 'cancel':
                    # So, this particular event was not cancelled. Maybe the
                    # whole calendar was.
                    event_status = 'cancelled'
                else:
                    # Otherwise assume the event has been confirmed.
                    event_status = 'confirmed'

            assert event_status in EVENT_STATUSES

            recur = component.get('rrule')
            if recur:
                recur = "RRULE:{}".format(recur.to_ical())

            participants = []

            organizer = component.get('organizer')
            if organizer:
                # Here's the problem. Gmail and Exchange define the organizer
                # field like this:
                #
                # ORGANIZER;CN="User";EMAIL="user@email.com":mailto:user@email.com
                # but iCloud does it like this:
                # ORGANIZER;CN=User;EMAIL=user@icloud.com:mailto:
                # random_alphanumeric_string@imip.me.com
                # so what we first try to get the EMAIL field, and only if
                # it's not present we use the MAILTO: link.
                if 'EMAIL' in organizer.params:
                    organizer_email = organizer.params['EMAIL']
                else:
                    organizer_email = unicode(organizer)
                    if organizer_email.startswith('mailto:'):
                        organizer_email = organizer_email[7:]

                if 'CN' in organizer.params:
                    organizer_name = organizer.params['CN']

            owner = "{} <{}>".format(organizer_name, organizer_email)

            if (namespace.account.email_address ==
                    canonicalize_address(organizer_email)):
                is_owner = True
            else:
                is_owner = False

            attendees = component.get('attendee', [])

            # the iCalendar python module doesn't return a list when
            # there's only one attendee. Go figure.
            if not isinstance(attendees, list):
                attendees = [attendees]

            for attendee in attendees:
                email = unicode(attendee)
                # strip mailto: if it exists
                if email.lower().startswith('mailto:'):
                    email = email[7:]
                try:
                    name = attendee.params['CN']
                except KeyError:
                    name = None

                status_map = {'NEEDS-ACTION': 'noreply',
                              'ACCEPTED': 'yes',
                              'DECLINED': 'no',
                              'TENTATIVE': 'maybe'}
                status = 'noreply'
                try:
                    a_status = attendee.params['PARTSTAT']
                    status = status_map[a_status]
                except KeyError:
                    pass

                notes = None
                try:
                    guests = attendee.params['X-NUM-GUESTS']
                    notes = "Guests: {}".format(guests)
                except KeyError:
                    pass

                participants.append({'email': email,
                                     'name': name,
                                     'status': status,
                                     'notes': notes,
                                     'guests': []})

            location = component.get('location')
            uid = str(component.get('uid'))
            if '@nylas.com' in uid:
                uid = uid[:-10]

            sequence_number = int(component.get('sequence'))

            event = Event(
                namespace=namespace,
                calendar=calendar,
                uid=uid,
                provider_name='ics',
                raw_data=component.to_ical(),
                title=title,
                description=description,
                location=location,
                reminders=str([]),
                recurrence=recur,
                start=start,
                end=end,
                busy=True,
                all_day=all_day,
                read_only=False,
                owner=owner,
                is_owner=is_owner,
                last_modified=last_modified,
                original_start_tz=original_start_tz,
                source='local',
                status=event_status,
                sequence_number=sequence_number,
                participants=participants)

            if calendar_method == 'REQUEST' or calendar_method == 'CANCEL':
                events['invites'].append(event)
            elif calendar_method == 'REPLY':
                events['rsvps'].append(event)

    return events


def process_invites(db_session, message, account, invites):
    # Check db if the invite alread
    new_uids = [event.uid for event in invites]

    # Get the list of events which share a uid with those we received.
    # Note that we're limiting this query to events in the 'emailed events'
    # calendar, because that's where all the invites go.
    existing_events = db_session.query(Event).filter(
        Event.calendar_id == account.emailed_events_calendar_id,
        Event.namespace_id == account.namespace.id,
        Event.uid.in_(new_uids)).all()

    existing_events_table = {event.uid: event for event in existing_events}
    for event in invites:
        if event.uid not in existing_events_table:
            # This is some SQLAlchemy trickery -- the events returned
            # by events_from_ics aren't bound to a session. Because of
            # this, we don't care if they get garbage-collected.
            # By associating the event to the message we make sure it
            # will be flushed to the db.
            event.calendar = account.emailed_events_calendar
            event.message = message
        else:
            # This is an event we already have in the db.
            # Let's see if the version we have is older or newer.
            existing_event = existing_events_table[event.uid]

            if existing_event.sequence_number <= event.sequence_number:
                merged_participants = existing_event.\
                    _partial_participants_merge(event)

                existing_event.update(event)
                existing_event.message = message

                # We have to do this mumbo-jumbo because MutableList does
                # not register changes to nested elements.
                # We could probably change MutableList to handle it (see:
                # https://groups.google.com/d/msg/sqlalchemy/i2SIkLwVYRA/mp2WJFaQxnQJ)
                # but this sounds very brittle.
                existing_event.participants = []
                for participant in merged_participants:
                    existing_event.participants.append(participant)


def process_rsvps(db_session, message, account, rsvps):
    new_uids = [event.uid for event in rsvps]

    # Get the list of events which share a uid with those we received.
    # Note that we're not limiting this query to events in the
    # "Emailed events" calendar because we may have received RSVPs to
    # an invite we previously sent.
    existing_events = db_session.query(Event).filter(
        Event.namespace_id == account.namespace.id,
        Event.calendar_id != account.emailed_events_calendar_id,
        Event.public_id.in_(new_uids)).all()

    existing_events_table = {event.public_id: event
                             for event in existing_events}
    for event in rsvps:
        if event.uid not in existing_events_table:
            # We've received an RSVP to an event we never heard about. Save it,
            # maybe we'll sync the invite later.
            event.calendar = account.emailed_events_calendar
            event.message = message
        else:
            # This is an event we already have in the db.
            # Let's see if the version we have is older or newer.
            existing_event = existing_events_table[event.uid]

            if existing_event.sequence_number == event.sequence_number:
                merged_participants = existing_event.\
                    _partial_participants_merge(event)

                # We have to do this mumbo-jumbo because MutableList does
                # not register changes to nested elements.
                # We could probably change MutableList to handle it (see:
                # https://groups.google.com/d/msg/sqlalchemy/i2SIkLwVYRA/mp2WJFaQxnQJ)
                # but it seems very brittle.
                existing_event.participants = []
                for participant in merged_participants:
                    existing_event.participants.append(participant)

                # We need to sync back changes to the event manually
                if existing_event.calendar != account.emailed_events_calendar:
                    schedule_action('update_event', existing_event,
                                    existing_event.namespace.id, db_session,
                                    calendar_uid=existing_event.calendar.uid)

                db_session.flush()


def import_attached_events(db_session, account, message):
    """Import events from a file into the 'Emailed events' calendar."""

    assert account is not None
    from_addr = message.from_addr[0][1]

    # FIXME @karim - Don't import iCalendar events from messages we've sent.
    # This is only a stopgap measure -- what we need to have instead is
    # smarter event merging (i.e: looking at whether the sender is the
    # event organizer or not, and if the sequence number got incremented).
    if from_addr == account.email_address:
        return

    for part in message.attached_event_files:
        try:
            new_events = events_from_ics(account.namespace,
                                         account.emailed_events_calendar,
                                         part.block.data)
        except MalformedEventError:
            log.error('Attached event parsing error',
                      account_id=account.id, message_id=message.id)
            continue
        except (AssertionError, TypeError, RuntimeError,
                AttributeError, ValueError):
            # Kind of ugly but we don't want to derail message
            # creation because of an error in the attached calendar.
            log.error('Unhandled exception during message parsing',
                      message_id=message.id,
                      traceback=traceback.format_exception(
                                    sys.exc_info()[0],
                                    sys.exc_info()[1],
                                    sys.exc_info()[2]))
            continue

        process_invites(db_session, message, account, new_events['invites'])

        # Gmail has a very very annoying feature: it doesn't use email to RSVP
        # to an invite sent by another gmail account. This makes it impossible
        # for us to update the event correctly. To work around this, we "spoof"
        # Google calendar invites by setting values similar to what it would
        # set. Of course, we handle # this ourselves for the other providers.
        # - karim
        if account.provider != 'gmail':
            process_rsvps(db_session, message, account, new_events['rsvps'])


def generate_icalendar_invite(event):
    # Generates an iCalendar invite from an event.

    if event.sequence_number is None:
        event.sequence_number = 0
    else:
        event.sequence_number += 1

    cal = iCalendar()
    cal.add('PRODID', '-//Nylas sync engine//nylas.com//')
    cal.add('METHOD', 'REQUEST')
    cal.add('VERSION', '2.0')
    cal.add('CALSCALE', 'GREGORIAN')

    icalendar_event = icalendar.Event()

    account = event.namespace.account

    if account.provider == 'gmail':
        organizer = icalendar.vCalAddress("MAILTO:{}".format(event.calendar.uid))
        icalendar_event['uid'] = "{}@google.com".format(event.uid)
    else:
        organizer = icalendar.vCalAddress("MAILTO:{}".format(
            account.email_address))
        icalendar_event['uid'] = "{}@nylas.com".format(event.public_id)
    if account.name is not None:
        organizer.params['CN'] = account.name

    icalendar_event['organizer'] = organizer
    icalendar_event['sequence'] = event.sequence_number
    icalendar_event['X-MICROSOFT-CDO-APPT-SEQUENCE'] = icalendar_event['sequence']
    icalendar_event['status'] = 'CONFIRMED'
    icalendar_event['last-modified'] = serialize_datetime(event.updated_at)
    icalendar_event['dtstamp'] = icalendar_event['last-modified']
    icalendar_event['created'] = serialize_datetime(event.created_at)
    icalendar_event['dtstart'] = serialize_datetime(event.start)
    icalendar_event['dtend'] = serialize_datetime(event.end)
    icalendar_event['transp'] = 'OPAQUE'

    if event.description is not None:
        icalendar_event['description'] = event.description

    if event.location is not None:
        icalendar_event['location'] = event.location
    else:
        icalendar_event['location'] = ''

    if event.title is not None:
        icalendar_event['summary'] = event.title
    else:
        icalendar_event['summary'] = ''
    # icalendar_event['transp'] = event.busy

    attendees = []
    for participant in event.participants:
        email = participant.get('email', None)

        # FIXME @karim: handle the case where a participant has no address.
        # We may have to patch the iCalendar module for this.
        assert email is not None and email != ""

        attendee = icalendar.vCalAddress("MAILTO:{}".format(email))
        name = participant.get('name', None)
        if name is not None:
            # attendee.params['CN'] = name
            pass

        attendee.params['RSVP'] = 'TRUE'
        attendee.params['ROLE'] = 'REQ-PARTICIPANT'
        attendee.params['CUTYPE'] = 'INDIVIDUAL'
        attendee.params['PARTSTAT'] = 'NEEDS-ACTION'
        attendees.append(attendee)

    if attendees != []:
        icalendar_event.add('ATTENDEE', attendees)

    cal.add_component(icalendar_event)
    return cal


def generate_invite_message(ical_txt, event, html_body, account):
    text_body = html2text(html_body)
    msg = mime.create.multipart('mixed')

    body = mime.create.multipart('alternative')
    body.append(
        mime.create.text('plain', text_body),
        mime.create.text('html', html_body),
        mime.create.text('calendar; method=REQUEST', ical_txt, charset='utf8'))

    attachment = mime.create.attachment(
                     'application/ics',
                     ical_txt,
                     'invite.ics',
                     disposition='attachment')

    msg.append(body)
    msg.append(attachment)

    msg.headers['From'] = account.email_address
    if account.provider == 'gmail':
        msg.headers['Reply-To'] = event.calendar.uid
    else:
        msg.headers['Reply-To'] = account.email_address

    msg.headers['Subject'] = "Invite: {}".format(event.title)
    return msg


def send_invite(ical_txt, event, html_body, account):
    from inbox.sendmail.base import get_sendmail_client, SendMailException

    statuses = {}
    for participant in event.participants:
        email = participant.get('email', None)
        if email is None:
            continue

        msg = generate_invite_message(ical_txt, event, html_body, account)
        msg.headers['To'] = email
        final_message = msg.to_string()

        try:
            sendmail_client = get_sendmail_client(account)
            sendmail_client.send_generated_email([email], final_message)
            statuses[email] = {'status': 'success'}
        except SendMailException as e:
            statuses[email] = {'status': 'failure', 'reason': str(e)}

    return statuses


def _generate_rsvp(status, account, event):
    # It seems that Google Calendar requires us to copy a number of fields
    # in the RVSP reply. I suppose it's for reconciling the reply with the
    # invite. - karim
    dtstamp = serialize_datetime(datetime.utcnow())

    cal = iCalendar()
    cal.add('PRODID', '-//Nylas sync engine//nylas.com//')
    cal.add('METHOD', 'REPLY')
    cal.add('VERSION', '2.0')
    cal.add('CALSCALE', 'GREGORIAN')

    icalevent = icalendar.Event()
    icalevent['uid'] = event.uid

    # For ahem, 'historic reasons', we're saving the owner field
    # as "Organizer <organizer@nylas.com>".
    organizer_name, organizer_email = event.owner.split('<')
    organizer_email = organizer_email[:-1]

    organizer = icalendar.vCalAddress("MAILTO:{}".format(organizer_email))
    icalevent['organizer'] = organizer

    icalevent['sequence'] = event.sequence_number
    icalevent['X-MICROSOFT-CDO-APPT-SEQUENCE'] = icalevent['sequence']

    if event.status == 'confirmed':
        icalevent['status'] = 'CONFIRMED'

    icalevent['last-modified'] = event.last_modified
    icalevent['dtstamp'] = icalevent['last-modified']
    icalevent['dtstart'] = event.start
    icalevent['dtend'] = event.end
    icalevent['description'] = event.description
    icalevent['location'] = event.location
    icalevent['summary'] = event.summary

    attendee = icalendar.vCalAddress('MAILTO:{}'.format(account.email_address))
    attendee.params['cn'] = account.name
    attendee.params['partstat'] = status
    event.add('attendee', attendee, encode=0)
    cal.add_component(event)

    ret = {}
    ret["cal"] = cal
    ret["organizer_email"] = organizer_email

    return ret


def generate_rsvp(event, participant, account):
    # Generates an iCalendar file to RSVP to an invite.
    status = INVERTED_STATUS_MAP.get(participant["status"])
    return _generate_rsvp(status, account, event)


def send_rsvp(ical_data, event, body_text, account):
    from inbox.sendmail.base import get_sendmail_client
    ical_file = ical_data["cal"]
    rsvp_to = ical_data["organizer_email"]
    ical_txt = ical_file.to_ical()

    sendmail_client = get_sendmail_client(account)

    msg = mime.create.multipart('mixed')

    body = mime.create.multipart('alternative')
    body.append(
        mime.create.text('html', body_text),
        mime.create.text('calendar;method=REPLY', ical_txt))

    attachment = mime.create.attachment(
                     'text/calendar',
                     ical_txt,
                     'invite.ics',
                     disposition='attachment')

    msg.append(body)
    msg.append(attachment)

    msg.headers['To'] = rsvp_to
    msg.headers['Reply-To'] = account.email_address
    msg.headers['From'] = account.email_address
    msg.headers['Subject'] = 'RSVP to "{}"'.format(event.title)

    final_message = msg.to_string()
    try:
        sendmail_client = get_sendmail_client(account)
        sendmail_client.send_generated_email([email], final_message)
    except SendMailException:
        return None
