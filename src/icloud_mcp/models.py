"""Pydantic data models shared across icloud_mcp modules."""

# ``Reminder.list`` shadows the builtin ``list`` inside that class body, so the
# ``alarms`` field can't reference ``list[...]`` directly — it would resolve to
# the field. Alias the builtin here so the annotation/default stay correct.
from builtins import list as _list
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class Folder(BaseModel):
    """Represents an IMAP mailbox folder.

    Attributes:
        name: Full folder name (e.g. ``"INBOX"``, ``"Deleted Messages"``).
        delimiter: Hierarchy delimiter returned by IMAP LIST.
        flags: IMAP folder flags (e.g. ``\\Noselect``, ``\\HasChildren``).
    """

    name: str
    delimiter: str = "/"
    flags: list[str] = Field(default_factory=list)


class FolderStats(BaseModel):
    """Statistics for an IMAP mailbox folder.

    Attributes:
        folder: Full folder name.
        total_count: Total number of messages in the folder.
        unread_count: Number of unread (UNSEEN) messages.
    """

    folder: str
    total_count: int
    unread_count: int


class Attachment(BaseModel):
    """Metadata for an email attachment (no binary content).

    Attributes:
        filename: Original filename of the attachment.
        content_type: MIME type (e.g. ``"application/pdf"``).
        size: Size in bytes, if available.
    """

    filename: str
    content_type: str
    size: int | None = None


class Email(BaseModel):
    """Represents a single email message.

    Attributes:
        uid: IMAP UID of the message.
        folder: Folder where this email lives.
        subject: Email subject, decoded from RFC 2047.
        sender: From header value.
        to: List of To recipients.
        cc: List of CC recipients.
        date: Parsed date from the Date header.
        body_text: Plain text body.
        body_html: HTML body.
        is_read: Whether the ``\\Seen`` flag is set.
        message_id: RFC 2822 Message-ID header value.
        in_reply_to: In-Reply-To header (parent message-id).
        references: References header (space-separated chain of message-ids).
        reply_to: Reply-To header (user-specified reply address).
        attachments: Attachment metadata list.
    """

    uid: str
    folder: str
    subject: str = ""
    sender: str = ""
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    date: datetime | None = None
    body_text: str = ""
    body_html: str = ""
    is_read: bool = False
    message_id: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    reply_to: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)


class EmailListResult(BaseModel):
    """Paginated result for email listing.

    Attributes:
        emails: List of Email models for the current page.
        total_count: Total number of emails in the folder.
    """

    emails: list[Email]
    total_count: int


class SearchQuery(BaseModel):
    """Parameters for IMAP SEARCH operations.

    Attributes:
        folder: Folder to search in.
        sender: Maps to IMAP ``FROM`` criterion.
        subject: Maps to IMAP ``SUBJECT`` criterion.
        since: Maps to IMAP ``SINCE`` (inclusive).
        before: Maps to IMAP ``BEFORE`` (exclusive).
        body: Maps to IMAP ``BODY`` criterion.
        is_read: ``True`` → SEEN, ``False`` → UNSEEN, ``None`` → no filter.
        is_flagged: ``True`` → FLAGGED, ``False`` → UNFLAGGED, ``None`` → no filter.
        min_size: Minimum message size in bytes (maps to IMAP ``LARGER``).
        has_attachments: ``True`` → HEADER Content-Type multipart/mixed heuristic.
        limit: Maximum number of results to return (1–100).
    """

    folder: str = "INBOX"
    sender: str | None = None
    subject: str | None = None
    since: date | None = None
    before: date | None = None
    body: str | None = None
    is_read: bool | None = None
    is_flagged: bool | None = None
    min_size: int | None = Field(default=None, ge=0)
    has_attachments: bool | None = None
    limit: int = Field(default=20, ge=1, le=100)


class RuleCondition(BaseModel):
    """A single condition for matching emails in a rule.

    Attributes:
        field: Email field to match against (sender, subject, body).
        operator: Comparison operator (equals, contains, starts_with, ends_with).
        value: Value to compare against.
    """

    field: Literal["sender", "subject", "body"]
    operator: Literal["equals", "contains", "starts_with", "ends_with"]
    value: str


class RuleAction(BaseModel):
    """An action to apply when a rule matches.

    Attributes:
        action_type: Type of action (move, flag, mark_as_read, delete).
        destination: Target folder, required only for 'move' action.
    """

    action_type: Literal["move", "flag", "mark_as_read", "delete"]
    destination: str | None = None


class Rule(BaseModel):
    """An email filtering rule with conditions (AND) and actions.

    Attributes:
        name: Unique name for this rule.
        enabled: Whether this rule is active.
        conditions: List of conditions that must all match (AND logic).
        actions: List of actions to apply when all conditions match.
        created_at: ISO 8601 timestamp of when the rule was created.
    """

    name: str
    enabled: bool = True
    conditions: list[RuleCondition] = Field(default_factory=list)
    actions: list[RuleAction] = Field(default_factory=list)
    created_at: str | None = None


class Calendar(BaseModel):
    """Represents an iCloud CalDAV calendar collection.

    Attributes:
        name: Human-readable display name (``displayname`` property).
        url: Absolute URL of the calendar collection on the partition host.
        color: Calendar color as a hex string (``#RRGGBB``), if advertised.
        read_only: ``True`` when the current user cannot write to the calendar.
    """

    name: str
    url: str
    color: str | None = None
    read_only: bool = False


class CalendarEvent(BaseModel):
    """Represents a single calendar event (a ``VEVENT`` component).

    Datetimes are timezone-aware. For all-day events ``all_day`` is ``True``
    and the time component of ``start``/``end`` is not significant.

    Attributes:
        uid: iCalendar ``UID`` — stable identifier of the event.
        calendar: Name of the calendar the event belongs to.
        summary: Event title (``SUMMARY``).
        start: Event start (``DTSTART``).
        end: Event end (``DTEND``).
        all_day: Whether this is an all-day event (date-valued DTSTART/DTEND).
        location: Free-form location (``LOCATION``).
        description: Long-form notes (``DESCRIPTION``).
        href: CalDAV resource path of the event (``calendar.url`` + ``UID.ics``).
        etag: Server ETag, used for optimistic concurrency on update/delete.
        rrule: Raw recurrence rule of the master event (``RRULE``), e.g.
            ``"FREQ=WEEKLY;BYDAY=MO"``. ``None`` for non-recurring events.
        is_recurring: ``True`` when the underlying resource carries an ``RRULE``
            (or ``RDATE``) — i.e. this event belongs to a recurring series.
        recurrence_id: For an expanded occurrence, the ``RECURRENCE-ID``
            identifying which instance of the series this is. ``None`` for
            non-recurring events and for the unexpanded master.
    """

    uid: str
    calendar: str
    summary: str = ""
    start: datetime
    end: datetime
    all_day: bool = False
    location: str | None = None
    description: str | None = None
    href: str | None = None
    etag: str | None = None
    rrule: str | None = None
    is_recurring: bool = False
    recurrence_id: datetime | None = None


class ReminderList(BaseModel):
    """Represents an iCloud Reminders list (a native EventKit ``EKCalendar``).

    Reminders are served by the local macOS Reminders app via EventKit, so a
    list maps to an ``EKCalendar`` of the reminders entity type.

    Attributes:
        name: Human-readable display name (``EKCalendar.title``).
        identifier: Stable EventKit identifier (``EKCalendar.calendarIdentifier``).
        color: List color as a hex string (``#RRGGBB``), if available.
        read_only: ``True`` when the list does not allow content modifications.
    """

    name: str
    identifier: str
    color: str | None = None
    read_only: bool = False


class ReminderAlarm(BaseModel):
    """A display alarm on a reminder (a native EventKit ``EKAlarm``).

    Exactly one of the two fields is set:

    Attributes:
        minutes_before: Minutes before the reminder's ``due`` (or ``start`` if
            there is no due) to fire — a relative alarm (``relativeOffset``).
        trigger: Absolute date/time to fire — an absolute alarm (``absoluteDate``).
    """

    minutes_before: int | None = None
    trigger: datetime | None = None


class Reminder(BaseModel):
    """Represents a single reminder/task (a native EventKit ``EKReminder``).

    Datetimes are timezone-aware. ``due`` is the deadline that makes a reminder
    show up in the calendar timeline; a reminder without ``due`` is a plain
    task. For all-day reminders ``all_day`` is ``True`` and the time component
    of ``due``/``start`` is not significant.

    Attributes:
        uid: Stable identifier of the reminder (``calendarItemExternalIdentifier``).
        list: Name of the reminders list the task belongs to.
        summary: Task title (``EKReminder.title``).
        completed: Whether the task is done (``EKReminder.isCompleted``).
        completed_at: Completion timestamp (``completionDate``), if any.
        due: Deadline (``dueDateComponents``). ``None`` for a task without one.
        start: Start date/time (``startDateComponents``), if any.
        all_day: Whether ``due``/``start`` are date-valued (no time component).
        priority: iCalendar ``PRIORITY`` (0 none, 1-4 high, 5 medium, 6-9 low) —
            EventKit uses the same scale.
        description: Long-form notes (``EKReminder.notes``).
        url: Associated URL (``EKReminder.URL``).
        rrule: Raw recurrence rule (``RRULE``), e.g. ``"FREQ=WEEKLY;BYDAY=MO"``.
            ``None`` for a one-off task. Derived from ``EKReminder.recurrenceRules``.
        is_recurring: ``True`` when the task carries a recurrence rule.
        alarms: Display alarms on the task (``EKReminder.alarms``).
        created: Creation timestamp (``EKReminder.creationDate``), if any.
        modified: Last-modification timestamp (``lastModifiedDate``), if any.
    """

    uid: str
    list: str
    summary: str = ""
    completed: bool = False
    completed_at: datetime | None = None
    due: datetime | None = None
    start: datetime | None = None
    all_day: bool = False
    priority: int | None = None
    description: str | None = None
    url: str | None = None
    rrule: str | None = None
    is_recurring: bool = False
    alarms: _list[ReminderAlarm] = Field(default_factory=_list)
    created: datetime | None = None
    modified: datetime | None = None
