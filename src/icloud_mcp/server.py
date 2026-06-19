"""MCP server wiring: tool registration, lifespan, and client orchestration."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from icloud_mcp.caldav_client import CalDAVClient
from icloud_mcp.config import get_settings
from icloud_mcp.eventkit_client import EventKitClient
from icloud_mcp.exceptions import EventKitError
from icloud_mcp.imap_client import IMAPClient, IMAPConnectionPool
from icloud_mcp.models import ReminderAlarm, SearchQuery
from icloud_mcp.rules import RulesEngine
from icloud_mcp.smtp_client import SMTPClient

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Holds live client instances for the duration of the server process."""

    imap_client: IMAPClient
    smtp_client: SMTPClient
    rules_engine: RulesEngine
    caldav_client: CalDAVClient
    eventkit_client: EventKitClient


@asynccontextmanager
async def app_lifespan(app: FastMCP[AppContext]) -> AsyncIterator[AppContext]:
    """Initialize the IMAP pool, CalDAV client, and EventKit Reminders client."""
    settings = get_settings()
    pool = IMAPConnectionPool(settings)
    log.info("Inicializando pool de conexões IMAP...")
    await pool.initialize()
    log.info("Pool IMAP inicializado com %d conexões.", settings.imap_pool_size)
    caldav_client = CalDAVClient(settings)
    eventkit_client = EventKitClient(settings)
    try:
        log.info("Descobrindo serviço CalDAV do iCloud...")
        await caldav_client.connect()
        log.info("Solicitando acesso aos Lembretes via EventKit...")
        try:
            await eventkit_client.connect()
            log.info("Acesso aos Lembretes concedido.")
        except EventKitError as exc:
            # Mail and Calendar still work without Reminders; degrade gracefully.
            log.warning(
                "Lembretes indisponíveis via EventKit (as demais ferramentas seguem ativas): %s",
                exc,
            )
        yield AppContext(
            imap_client=IMAPClient(pool),
            smtp_client=SMTPClient(settings),
            rules_engine=RulesEngine(),
            caldav_client=caldav_client,
            eventkit_client=eventkit_client,
        )
    finally:
        log.info("Encerrando pool de conexões IMAP e clientes CalDAV/EventKit...")
        await pool.close()
        await caldav_client.close()
        await eventkit_client.close()


mcp: FastMCP[AppContext] = FastMCP("icloud-mcp", lifespan=app_lifespan)


def _get_ctx(ctx: Context) -> AppContext:  # type: ignore[type-arg]
    """Extract the AppContext from the MCP request context."""
    lc: AppContext = ctx.request_context.lifespan_context
    return lc


@mcp.tool()
async def list_folders(ctx: Context) -> list[dict[str, Any]]:  # type: ignore[type-arg]
    """List all available iCloud Mail folders."""
    app = _get_ctx(ctx)
    folders = await app.imap_client.list_folders()
    return [f.model_dump() for f in folders]


@mcp.tool()
async def list_emails(
    ctx: Context,  # type: ignore[type-arg]
    folder: str = "INBOX",
    limit: int = 20,
    offset: int = 0,
    sort_order: str = "desc",
) -> dict[str, Any]:
    """List emails in a folder with pagination.

    Use sort_order 'desc' (newest first, default) or 'asc' (oldest first).
    Returns a dict with 'emails' (current page) and 'total_count' (full folder size).
    """
    app = _get_ctx(ctx)
    result = await app.imap_client.list_emails(
        folder=folder, limit=limit, offset=offset, sort_order=sort_order
    )
    return {
        "emails": [e.model_dump(mode="json") for e in result.emails],
        "total_count": result.total_count,
    }


@mcp.tool()
async def get_email(ctx: Context, folder: str, uid: str) -> dict[str, Any]:  # type: ignore[type-arg]
    """Fetch a complete email by UID including full body and attachment metadata."""
    app = _get_ctx(ctx)
    email_obj = await app.imap_client.get_email(folder=folder, uid=uid)
    return email_obj.model_dump(mode="json")


@mcp.tool()
async def search_emails(
    ctx: Context,  # type: ignore[type-arg]
    folder: str = "INBOX",
    sender: str | None = None,
    subject: str | None = None,
    since: str | None = None,
    before: str | None = None,
    body: str | None = None,
    is_read: bool | None = None,
    is_flagged: bool | None = None,
    min_size: int | None = None,
    has_attachments: bool | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search emails using IMAP SEARCH criteria. All filters are combined with AND.

    Args:
        folder: Folder to search in.
        sender: Filter by sender email address.
        subject: Filter by subject text.
        since: Include emails on or after this date (YYYY-MM-DD, inclusive).
        before: Include emails before this date (YYYY-MM-DD, exclusive).
        body: Filter by text in the email body.
        is_read: True → only read (SEEN), False → only unread (UNSEEN).
        is_flagged: True → only flagged, False → only unflagged.
        min_size: Minimum message size in bytes (LARGER criterion).
        has_attachments: True → only emails with attachments (multipart/mixed heuristic).
        limit: Maximum number of results (1–100).
    """
    app = _get_ctx(ctx)
    since_date: date | None = None
    before_date: date | None = None
    if since:
        try:
            since_date = date.fromisoformat(since)
        except ValueError as exc:
            raise ValueError(
                f"Formato de data inválido para 'since': '{since}'. Use YYYY-MM-DD."
            ) from exc
    if before:
        try:
            before_date = date.fromisoformat(before)
        except ValueError as exc:
            raise ValueError(
                f"Formato de data inválido para 'before': '{before}'. Use YYYY-MM-DD."
            ) from exc
    query = SearchQuery(
        folder=folder,
        sender=sender,
        subject=subject,
        since=since_date,
        before=before_date,
        body=body,
        is_read=is_read,
        is_flagged=is_flagged,
        min_size=min_size,
        has_attachments=has_attachments,
        limit=limit,
    )
    emails = await app.imap_client.search_emails(query=query)
    return [e.model_dump(mode="json") for e in emails]


@mcp.tool()
async def send_email(
    ctx: Context,  # type: ignore[type-arg]
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> dict[str, str]:
    """Send an email via iCloud Mail SMTP."""
    app = _get_ctx(ctx)
    return await app.smtp_client.send_email(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
    )


@mcp.tool()
async def move_email(
    ctx: Context,  # type: ignore[type-arg]
    folder: str,
    uid: str,
    destination: str,
) -> dict[str, str]:
    """Move an email from one folder to another."""
    app = _get_ctx(ctx)
    return await app.imap_client.move_email(folder=folder, uid=uid, destination=destination)


@mcp.tool()
async def delete_email(
    ctx: Context,  # type: ignore[type-arg]
    folder: str,
    uid: str,
) -> dict[str, str]:
    """Move an email to the iCloud Trash folder (Deleted Messages)."""
    app = _get_ctx(ctx)
    return await app.imap_client.delete_email(folder=folder, uid=uid)


@mcp.tool()
async def mark_as_read(ctx: Context, folder: str, uid: str) -> dict[str, str]:  # type: ignore[type-arg]
    """Mark an email as read (adds the \\Seen flag)."""
    app = _get_ctx(ctx)
    return await app.imap_client.mark_as_read(folder=folder, uid=uid)


@mcp.tool()
async def mark_as_unread(ctx: Context, folder: str, uid: str) -> dict[str, str]:  # type: ignore[type-arg]
    """Mark an email as unread (removes the \\Seen flag)."""
    app = _get_ctx(ctx)
    return await app.imap_client.mark_as_unread(folder=folder, uid=uid)


@mcp.tool()
async def flag_email(ctx: Context, folder: str, uid: str) -> dict[str, str]:  # type: ignore[type-arg]
    """Flag an email (adds the \\Flagged flag)."""
    app = _get_ctx(ctx)
    return await app.imap_client.flag_email(folder=folder, uid=uid)


@mcp.tool()
async def unflag_email(ctx: Context, folder: str, uid: str) -> dict[str, str]:  # type: ignore[type-arg]
    """Unflag an email (removes the \\Flagged flag)."""
    app = _get_ctx(ctx)
    return await app.imap_client.unflag_email(folder=folder, uid=uid)


@mcp.tool()
async def bulk_action(
    ctx: Context,  # type: ignore[type-arg]
    folder: str,
    uids: list[str],
    action: str,
    destination: str | None = None,
) -> dict[str, Any]:
    """Apply a bulk action to multiple emails by UID.

    Supported actions: mark_as_read, mark_as_unread, flag, unflag, move, delete.
    The 'destination' parameter is required only for the 'move' action.
    """
    app = _get_ctx(ctx)
    return await app.imap_client.bulk_action(
        folder=folder, uids=uids, action=action, destination=destination
    )


@mcp.tool()
async def create_folder(ctx: Context, name: str) -> dict[str, Any]:  # type: ignore[type-arg]
    """Create a new iCloud Mail folder."""
    app = _get_ctx(ctx)
    folder = await app.imap_client.create_folder(name=name)
    return folder.model_dump()


@mcp.tool()
async def rename_folder(ctx: Context, old_name: str, new_name: str) -> dict[str, Any]:  # type: ignore[type-arg]
    """Rename an existing iCloud Mail folder."""
    app = _get_ctx(ctx)
    folder = await app.imap_client.rename_folder(old_name=old_name, new_name=new_name)
    return folder.model_dump()


@mcp.tool()
async def delete_folder(ctx: Context, name: str) -> dict[str, str]:  # type: ignore[type-arg]
    """Delete an empty iCloud Mail folder."""
    app = _get_ctx(ctx)
    return await app.imap_client.delete_folder(name=name)


@mcp.tool()
async def list_attachments(
    ctx: Context,  # type: ignore[type-arg]
    folder: str,
    uid: str,
) -> list[dict[str, Any]]:
    """List attachments of an email without downloading the full message.

    Uses IMAP BODYSTRUCTURE for efficient metadata retrieval.
    Returns a list of dicts with 'filename', 'content_type', and 'size' keys.
    """
    app = _get_ctx(ctx)
    attachments = await app.imap_client.list_attachments(folder=folder, uid=uid)
    return [a.model_dump() for a in attachments]


@mcp.tool()
async def get_folder_stats(ctx: Context, folder: str = "INBOX") -> dict[str, Any]:  # type: ignore[type-arg]
    """Get statistics for an iCloud Mail folder (total and unread message counts)."""
    app = _get_ctx(ctx)
    stats = await app.imap_client.get_folder_stats(folder=folder)
    return stats.model_dump()


@mcp.tool()
async def download_attachment(
    ctx: Context,  # type: ignore[type-arg]
    folder: str,
    uid: str,
    filename: str,
) -> dict[str, str]:
    """Download the binary content of a specific email attachment as base64.

    Use get_email first to see available attachment filenames.
    Returns a dict with 'filename', 'content_type', and 'data' (base64-encoded).
    """
    app = _get_ctx(ctx)
    return await app.imap_client.download_attachment(folder=folder, uid=uid, filename=filename)


@mcp.tool()
async def save_draft(
    ctx: Context,  # type: ignore[type-arg]
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
) -> dict[str, str]:
    """Save a draft email without sending it."""
    app = _get_ctx(ctx)
    return await app.imap_client.save_draft(to=to, subject=subject, body=body, cc=cc)


@mcp.tool()
async def reply_email(
    ctx: Context,  # type: ignore[type-arg]
    folder: str,
    uid: str,
    body: str,
    reply_all: bool = False,
) -> dict[str, str]:
    """Reply to an existing email. Set reply_all=True to reply to all recipients."""
    app = _get_ctx(ctx)
    original = await app.imap_client.get_email(folder=folder, uid=uid)
    return await app.smtp_client.reply_email(
        original=original,
        body=body,
        reply_all=reply_all,
    )


@mcp.tool()
async def forward_email(
    ctx: Context,  # type: ignore[type-arg]
    folder: str,
    uid: str,
    to: list[str],
    body: str | None = None,
) -> dict[str, str]:
    """Forward an existing email to new recipients."""
    app = _get_ctx(ctx)
    original = await app.imap_client.get_email(folder=folder, uid=uid)
    return await app.smtp_client.forward_email(
        original=original,
        to=to,
        body=body,
    )


@mcp.tool()
async def list_rules(ctx: Context) -> list[dict[str, Any]]:  # type: ignore[type-arg]
    """List all email filtering rules."""
    app = _get_ctx(ctx)
    rules = app.rules_engine.list_rules()
    return [r.model_dump() for r in rules]


@mcp.tool()
async def create_rule(
    ctx: Context,  # type: ignore[type-arg]
    name: str,
    conditions: list[dict[str, str]],
    actions: list[dict[str, str | None]],
) -> dict[str, Any]:
    """Create a new email filtering rule.

    conditions: list of dicts with 'field', 'operator', 'value'.
    actions: list of dicts with 'action_type' and optional 'destination'.
    """
    app = _get_ctx(ctx)
    rule = app.rules_engine.create_rule(name=name, conditions=conditions, actions=actions)
    return rule.model_dump()


@mcp.tool()
async def delete_rule(ctx: Context, name: str) -> dict[str, str]:  # type: ignore[type-arg]
    """Delete an email filtering rule by name."""
    app = _get_ctx(ctx)
    return app.rules_engine.delete_rule(name=name)


@mcp.tool()
async def apply_rules(ctx: Context, folder: str = "INBOX") -> dict[str, Any]:  # type: ignore[type-arg]
    """Apply all enabled rules to emails in a folder. Returns processing stats."""
    app = _get_ctx(ctx)
    return await app.rules_engine.apply_rules(folder=folder, imap_client=app.imap_client)


def _parse_datetime(value: str, field: str) -> datetime:
    """Parse an ISO 8601 datetime (or date) string into a datetime."""
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"Formato de data/hora inválido para '{field}': '{value}'. "
            "Use ISO 8601 (YYYY-MM-DD ou YYYY-MM-DDTHH:MM:SS)."
        ) from exc


def _parse_alarms_arg(
    alarms: list[dict[str, Any]] | None,
) -> list[ReminderAlarm] | None:
    """Convert raw alarm dicts (minutes_before / trigger ISO string) into models."""
    if alarms is None:
        return None
    parsed: list[ReminderAlarm] = []
    for alarm in alarms:
        trigger = alarm.get("trigger")
        parsed.append(
            ReminderAlarm(
                minutes_before=alarm.get("minutes_before"),
                trigger=_parse_datetime(trigger, "trigger") if trigger else None,
            )
        )
    return parsed


@mcp.tool()
async def list_calendars(ctx: Context) -> list[dict[str, Any]]:  # type: ignore[type-arg]
    """List all iCloud calendars that support events."""
    app = _get_ctx(ctx)
    calendars = await app.caldav_client.list_calendars()
    return [c.model_dump() for c in calendars]


@mcp.tool()
async def list_events(
    ctx: Context,  # type: ignore[type-arg]
    calendar: str,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """List events in a calendar within a time range.

    Recurring events are expanded into one entry per occurrence inside the
    range; each occurrence carries ``recurrence_id`` and ``is_recurring=True``.

    Args:
        calendar: Calendar display name (see list_calendars).
        start: Range start, inclusive (ISO 8601, e.g. 2026-06-01 or 2026-06-01T09:00:00).
        end: Range end, exclusive (ISO 8601).
    """
    app = _get_ctx(ctx)
    start_dt = _parse_datetime(start, "start")
    end_dt = _parse_datetime(end, "end")
    events = await app.caldav_client.list_events(calendar=calendar, start=start_dt, end=end_dt)
    return [e.model_dump(mode="json") for e in events]


@mcp.tool()
async def get_event(ctx: Context, calendar: str, uid: str) -> dict[str, Any]:  # type: ignore[type-arg]
    """Fetch a single calendar event by its iCalendar UID."""
    app = _get_ctx(ctx)
    event = await app.caldav_client.get_event(calendar=calendar, uid=uid)
    return event.model_dump(mode="json")


@mcp.tool()
async def create_event(
    ctx: Context,  # type: ignore[type-arg]
    calendar: str,
    summary: str,
    start: str,
    end: str,
    all_day: bool = False,
    location: str | None = None,
    description: str | None = None,
    rrule: str | None = None,
) -> dict[str, Any]:
    """Create a new event in a calendar.

    Args:
        calendar: Target calendar display name.
        summary: Event title.
        start: Start datetime (ISO 8601). For all_day events a date is enough.
        end: End datetime (ISO 8601, exclusive).
        all_day: True for an all-day event (time component ignored).
        location: Optional location text.
        description: Optional notes.
        rrule: Optional iCalendar recurrence rule to create a recurring series,
            e.g. "FREQ=WEEKLY;BYDAY=MO" or "FREQ=DAILY;COUNT=10".
    """
    app = _get_ctx(ctx)
    event = await app.caldav_client.create_event(
        calendar=calendar,
        summary=summary,
        start=_parse_datetime(start, "start"),
        end=_parse_datetime(end, "end"),
        all_day=all_day,
        location=location,
        description=description,
        rrule=rrule,
    )
    return event.model_dump(mode="json")


@mcp.tool()
async def update_event(
    ctx: Context,  # type: ignore[type-arg]
    calendar: str,
    uid: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    all_day: bool | None = None,
    location: str | None = None,
    description: str | None = None,
    rrule: str | None = None,
) -> dict[str, Any]:
    """Update fields of an existing event (whole series). Only provided fields change.

    For recurring events this updates the entire series. Pass ``rrule=""`` to
    remove recurrence (turn it into a one-off); a non-empty ``rrule`` replaces
    the recurrence rule; omitting it keeps the current recurrence.
    """
    app = _get_ctx(ctx)
    event = await app.caldav_client.update_event(
        calendar=calendar,
        uid=uid,
        summary=summary,
        start=_parse_datetime(start, "start") if start is not None else None,
        end=_parse_datetime(end, "end") if end is not None else None,
        all_day=all_day,
        location=location,
        description=description,
        rrule=rrule,
    )
    return event.model_dump(mode="json")


@mcp.tool()
async def delete_event(ctx: Context, calendar: str, uid: str) -> dict[str, str]:  # type: ignore[type-arg]
    """Delete a calendar event by its iCalendar UID (the whole series if recurring)."""
    app = _get_ctx(ctx)
    return await app.caldav_client.delete_event(calendar=calendar, uid=uid)


@mcp.tool()
async def update_occurrence(
    ctx: Context,  # type: ignore[type-arg]
    calendar: str,
    uid: str,
    recurrence_id: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    location: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Edit a single occurrence of a recurring series, leaving the rest intact.

    Args:
        calendar: Calendar display name.
        uid: UID of the recurring series.
        recurrence_id: The occurrence's original slot (ISO 8601), as returned in
            each occurrence's `recurrence_id` by `list_events`.
        summary: New title for this occurrence only.
        start: New start (ISO 8601) for this occurrence only.
        end: New end (ISO 8601) for this occurrence only.
        location: New location for this occurrence only.
        description: New notes for this occurrence only.
    """
    app = _get_ctx(ctx)
    event = await app.caldav_client.update_occurrence(
        calendar=calendar,
        uid=uid,
        recurrence_id=_parse_datetime(recurrence_id, "recurrence_id"),
        summary=summary,
        start=_parse_datetime(start, "start") if start is not None else None,
        end=_parse_datetime(end, "end") if end is not None else None,
        location=location,
        description=description,
    )
    return event.model_dump(mode="json")


@mcp.tool()
async def delete_occurrence(
    ctx: Context,  # type: ignore[type-arg]
    calendar: str,
    uid: str,
    recurrence_id: str,
) -> dict[str, str]:
    """Delete a single occurrence of a recurring series (keeps the rest).

    Args:
        calendar: Calendar display name.
        uid: UID of the recurring series.
        recurrence_id: The occurrence's original slot (ISO 8601), as returned in
            each occurrence's `recurrence_id` by `list_events`.
    """
    app = _get_ctx(ctx)
    return await app.caldav_client.delete_occurrence(
        calendar=calendar,
        uid=uid,
        recurrence_id=_parse_datetime(recurrence_id, "recurrence_id"),
    )


# -- Reminders (native macOS EventKit) -------------------------------------


@mcp.tool()
async def list_reminder_lists(ctx: Context) -> list[dict[str, Any]]:  # type: ignore[type-arg]
    """List all iCloud Reminders lists (native macOS Reminders lists)."""
    app = _get_ctx(ctx)
    lists = await app.eventkit_client.list_reminder_lists()
    return [r.model_dump() for r in lists]


@mcp.tool()
async def list_reminders(
    ctx: Context,  # type: ignore[type-arg]
    list: str,
    include_completed: bool = False,
) -> list[dict[str, Any]]:
    """List reminders (tasks) in a list, ordered by due date (undated last).

    Args:
        list: Reminders list display name (see list_reminder_lists).
        include_completed: When False (default), completed tasks are hidden.
    """
    app = _get_ctx(ctx)
    reminders = await app.eventkit_client.list_reminders(
        list=list, include_completed=include_completed
    )
    return [r.model_dump(mode="json") for r in reminders]


@mcp.tool()
async def search_reminders(
    ctx: Context,  # type: ignore[type-arg]
    query: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    include_completed: bool = False,
    undated: bool = True,
    lists: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search reminders across all lists (or a subset), ordered by due date.

    Presets: overdue → due_before=now & undated=False; due today → due_before
    set to end of today & undated=False; free-text → query.

    Args:
        query: Case-insensitive substring matched against title and notes.
        due_before: Keep dated tasks due strictly before this (ISO 8601).
        due_after: Keep dated tasks due at/after this (ISO 8601).
        include_completed: When False (default), completed tasks are hidden.
        undated: Include tasks without a due date (default True).
        lists: Restrict to these list names; None searches every list.
    """
    app = _get_ctx(ctx)
    reminders = await app.eventkit_client.search_reminders(
        query=query,
        due_before=_parse_datetime(due_before, "due_before") if due_before else None,
        due_after=_parse_datetime(due_after, "due_after") if due_after else None,
        include_completed=include_completed,
        undated=undated,
        lists=lists,
    )
    return [r.model_dump(mode="json") for r in reminders]


@mcp.tool()
async def get_reminder(ctx: Context, list: str, uid: str) -> dict[str, Any]:  # type: ignore[type-arg]
    """Fetch a single reminder by its iCalendar UID."""
    app = _get_ctx(ctx)
    reminder = await app.eventkit_client.get_reminder(list=list, uid=uid)
    return reminder.model_dump(mode="json")


@mcp.tool()
async def create_reminder(
    ctx: Context,  # type: ignore[type-arg]
    list: str,
    summary: str,
    due: str | None = None,
    start: str | None = None,
    all_day: bool = False,
    priority: int | None = None,
    description: str | None = None,
    url: str | None = None,
    rrule: str | None = None,
    alarms: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a reminder in a list, with or without a due date.

    Args:
        list: Target reminders list display name.
        summary: Task title.
        due: Optional deadline (ISO 8601). Omit for a task without a deadline.
        start: Optional start date/time (ISO 8601).
        all_day: True to store due/start as a date (no time component).
        priority: iCalendar PRIORITY (0 none, 1-4 high, 5 medium, 6-9 low).
        description: Optional notes.
        url: Optional associated URL.
        rrule: Optional iCalendar recurrence rule for a recurring task,
            e.g. "FREQ=WEEKLY;BYDAY=MO" or "FREQ=DAILY;COUNT=10".
        alarms: Optional display alarms. Each item is a dict with exactly one of
            "minutes_before" (int, before the due) or "trigger" (absolute ISO 8601).
    """
    app = _get_ctx(ctx)
    reminder = await app.eventkit_client.create_reminder(
        list=list,
        summary=summary,
        due=_parse_datetime(due, "due") if due is not None else None,
        start=_parse_datetime(start, "start") if start is not None else None,
        all_day=all_day,
        priority=priority,
        description=description,
        url=url,
        rrule=rrule,
        alarms=_parse_alarms_arg(alarms),
    )
    return reminder.model_dump(mode="json")


@mcp.tool()
async def update_reminder(
    ctx: Context,  # type: ignore[type-arg]
    list: str,
    uid: str,
    summary: str | None = None,
    due: str | None = None,
    start: str | None = None,
    all_day: bool | None = None,
    priority: int | None = None,
    description: str | None = None,
    url: str | None = None,
    rrule: str | None = None,
    alarms: list[dict[str, Any]] | None = None,
    clear: list[str] | None = None,
) -> dict[str, Any]:
    """Update fields of an existing reminder. Only provided fields change.

    Use complete_reminder/reopen_reminder to toggle completion.

    Args:
        rrule: None keeps the current recurrence; "" removes it; a non-empty
            value replaces the recurrence rule.
        alarms: None keeps the current alarms; any list (including []) replaces
            all alarms. Each item is a dict with exactly one of "minutes_before"
            (int) or "trigger" (absolute ISO 8601).
        clear: Field names to unset entirely (any of "due", "start",
            "description", "url", "priority") — e.g. to remove a deadline.
    """
    app = _get_ctx(ctx)
    reminder = await app.eventkit_client.update_reminder(
        list=list,
        uid=uid,
        summary=summary,
        due=_parse_datetime(due, "due") if due is not None else None,
        start=_parse_datetime(start, "start") if start is not None else None,
        all_day=all_day,
        priority=priority,
        description=description,
        url=url,
        rrule=rrule,
        alarms=_parse_alarms_arg(alarms),
        clear=clear,
    )
    return reminder.model_dump(mode="json")


@mcp.tool()
async def complete_reminder(ctx: Context, list: str, uid: str) -> dict[str, Any]:  # type: ignore[type-arg]
    """Mark a reminder as completed."""
    app = _get_ctx(ctx)
    reminder = await app.eventkit_client.complete_reminder(list=list, uid=uid)
    return reminder.model_dump(mode="json")


@mcp.tool()
async def reopen_reminder(ctx: Context, list: str, uid: str) -> dict[str, Any]:  # type: ignore[type-arg]
    """Reopen a completed reminder (back to needs-action)."""
    app = _get_ctx(ctx)
    reminder = await app.eventkit_client.reopen_reminder(list=list, uid=uid)
    return reminder.model_dump(mode="json")


@mcp.tool()
async def delete_reminder(ctx: Context, list: str, uid: str) -> dict[str, str]:  # type: ignore[type-arg]
    """Delete a reminder by its iCalendar UID."""
    app = _get_ctx(ctx)
    return await app.eventkit_client.delete_reminder(list=list, uid=uid)


@mcp.tool()
async def move_reminder(
    ctx: Context,  # type: ignore[type-arg]
    uid: str,
    from_list: str,
    to_list: str,
) -> dict[str, Any]:
    """Move a reminder from one list to another (preserves all its fields)."""
    app = _get_ctx(ctx)
    reminder = await app.eventkit_client.move_reminder(
        uid=uid, from_list=from_list, to_list=to_list
    )
    return reminder.model_dump(mode="json")


@mcp.tool()
async def create_reminder_list(
    ctx: Context,  # type: ignore[type-arg]
    name: str,
    color: str | None = None,
) -> dict[str, Any]:
    """Create a new Reminders list.

    Args:
        name: Display name for the new list.
        color: Optional hex color (e.g. "#FF0000").
    """
    app = _get_ctx(ctx)
    rlist = await app.eventkit_client.create_reminder_list(name=name, color=color)
    return rlist.model_dump()


@mcp.tool()
async def rename_reminder_list(ctx: Context, name: str, new_name: str) -> dict[str, Any]:  # type: ignore[type-arg]
    """Rename an existing Reminders list."""
    app = _get_ctx(ctx)
    rlist = await app.eventkit_client.rename_reminder_list(name=name, new_name=new_name)
    return rlist.model_dump()


@mcp.tool()
async def delete_reminder_list(
    ctx: Context,  # type: ignore[type-arg]
    name: str,
    confirm: bool = False,
) -> dict[str, str]:
    """Delete a Reminders list and ALL its tasks. Requires confirm=True.

    Args:
        name: Display name of the list to delete.
        confirm: Must be True to proceed — this is destructive and irreversible.
    """
    app = _get_ctx(ctx)
    return await app.eventkit_client.delete_reminder_list(name=name, confirm=confirm)
