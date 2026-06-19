"""Tests for server.py — MCP tool handlers and lifespan."""

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from icloud_mcp.imap_client import IMAPClient
from icloud_mcp.models import (
    Attachment,
    Calendar,
    CalendarEvent,
    Email,
    EmailListResult,
    Folder,
    FolderStats,
    Reminder,
    ReminderList,
)
from icloud_mcp.rules import RulesEngine
from icloud_mcp.server import (
    AppContext,
    app_lifespan,
    apply_rules,
    bulk_action,
    complete_reminder,
    create_event,
    create_folder,
    create_reminder,
    create_reminder_list,
    create_rule,
    delete_email,
    delete_event,
    delete_folder,
    delete_occurrence,
    delete_reminder,
    delete_reminder_list,
    delete_rule,
    download_attachment,
    flag_email,
    forward_email,
    get_email,
    get_event,
    get_folder_stats,
    get_reminder,
    list_attachments,
    list_calendars,
    list_emails,
    list_events,
    list_folders,
    list_reminder_lists,
    list_reminders,
    list_rules,
    mark_as_read,
    mark_as_unread,
    mcp,
    move_email,
    move_reminder,
    rename_folder,
    rename_reminder_list,
    reopen_reminder,
    reply_email,
    save_draft,
    search_emails,
    search_reminders,
    send_email,
    unflag_email,
    update_event,
    update_occurrence,
    update_reminder,
)

MockCtx = tuple[MagicMock, AsyncMock, AsyncMock, RulesEngine]


@pytest.fixture
def mock_ctx(tmp_path: Path) -> MockCtx:
    """MagicMock Context with AppContext carrying AsyncMock IMAP/SMTP + RulesEngine."""
    imap_client = AsyncMock()
    smtp_client = AsyncMock()
    rules_engine = RulesEngine(rules_dir=tmp_path)
    caldav_client = AsyncMock()
    eventkit_client = AsyncMock()
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        imap_client=imap_client,
        smtp_client=smtp_client,
        rules_engine=rules_engine,
        caldav_client=caldav_client,
        eventkit_client=eventkit_client,
    )
    return ctx, imap_client, smtp_client, rules_engine


async def test_list_folders_tool(mock_ctx: MockCtx) -> None:
    """list_folders tool calls imap_client.list_folders and serializes Folder models."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.list_folders.return_value = [Folder(name="INBOX"), Folder(name="Sent")]

    result = await list_folders(ctx)

    assert result == [
        {"name": "INBOX", "delimiter": "/", "flags": []},
        {"name": "Sent", "delimiter": "/", "flags": []},
    ]
    imap_client.list_folders.assert_called_once()


async def test_list_emails_tool(mock_ctx: MockCtx) -> None:
    """list_emails tool returns dict with 'emails' list and 'total_count'."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.list_emails.return_value = EmailListResult(
        emails=[Email(uid="1", folder="INBOX")], total_count=1
    )

    result = await list_emails(ctx, folder="INBOX", limit=10, offset=5)

    assert result["total_count"] == 1
    assert len(result["emails"]) == 1
    imap_client.list_emails.assert_called_once_with(
        folder="INBOX", limit=10, offset=5, sort_order="desc"
    )


async def test_list_emails_tool_sort_order(
    mock_ctx: MockCtx,
) -> None:
    """list_emails tool propagates sort_order='asc' to imap_client."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.list_emails.return_value = EmailListResult(
        emails=[Email(uid="1", folder="INBOX")], total_count=1
    )

    result = await list_emails(ctx, folder="INBOX", limit=10, offset=0, sort_order="asc")

    assert result["total_count"] == 1
    assert len(result["emails"]) == 1
    imap_client.list_emails.assert_called_once_with(
        folder="INBOX", limit=10, offset=0, sort_order="asc"
    )


async def test_get_email_tool(mock_ctx: MockCtx) -> None:
    """get_email tool delegates folder/uid and serializes the Email model."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.get_email.return_value = Email(uid="42", folder="INBOX", subject="Hello")

    result = await get_email(ctx, folder="INBOX", uid="42")

    assert result["uid"] == "42"
    assert result["subject"] == "Hello"
    imap_client.get_email.assert_called_once_with(folder="INBOX", uid="42")


async def test_search_emails_tool(mock_ctx: MockCtx) -> None:
    """search_emails tool parses ISO date strings into date objects for SearchQuery."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.search_emails.return_value = []

    await search_emails(ctx, folder="INBOX", since="2024-01-01", before="2024-12-31", limit=5)

    call_kwargs = imap_client.search_emails.call_args.kwargs
    query = call_kwargs["query"]
    assert query.since == date(2024, 1, 1)
    assert query.before == date(2024, 12, 31)
    assert query.folder == "INBOX"
    assert query.limit == 5


async def test_search_emails_with_new_filters_tool(
    mock_ctx: MockCtx,
) -> None:
    """search_emails tool propagates is_read, is_flagged, min_size, has_attachments."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.search_emails.return_value = []

    await search_emails(
        ctx,
        folder="INBOX",
        is_read=False,
        is_flagged=True,
        min_size=1024,
        has_attachments=True,
    )

    call_kwargs = imap_client.search_emails.call_args.kwargs
    query = call_kwargs["query"]
    assert query.is_read is False
    assert query.is_flagged is True
    assert query.min_size == 1024
    assert query.has_attachments is True


async def test_send_email_tool(mock_ctx: MockCtx) -> None:
    """send_email tool delegates all arguments to smtp_client.send_email."""
    ctx, _, smtp_client, _ = mock_ctx
    smtp_client.send_email.return_value = {"status": "sent", "message_id": "abc"}

    result = await send_email(
        ctx,
        to=["to@example.com"],
        subject="Hi",
        body="World",
        cc=["cc@example.com"],
        bcc=["bcc@example.com"],
    )

    assert result["status"] == "sent"
    smtp_client.send_email.assert_called_once_with(
        to=["to@example.com"],
        subject="Hi",
        body="World",
        cc=["cc@example.com"],
        bcc=["bcc@example.com"],
    )


async def test_move_email_tool(mock_ctx: MockCtx) -> None:
    """move_email tool delegates folder/uid/destination to imap_client."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.move_email.return_value = {
        "status": "moved",
        "uid": "42",
        "destination": "Archive",
    }

    result = await move_email(ctx, folder="INBOX", uid="42", destination="Archive")

    assert result["status"] == "moved"
    imap_client.move_email.assert_called_once_with(folder="INBOX", uid="42", destination="Archive")


async def test_delete_email_tool(mock_ctx: MockCtx) -> None:
    """delete_email tool delegates folder/uid to imap_client."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.delete_email.return_value = {
        "status": "moved",
        "uid": "7",
        "destination": "Deleted Messages",
    }

    result = await delete_email(ctx, folder="INBOX", uid="7")

    assert result["destination"] == "Deleted Messages"
    imap_client.delete_email.assert_called_once_with(folder="INBOX", uid="7")


async def test_create_folder_tool(mock_ctx: MockCtx) -> None:
    """create_folder tool delegates name and serializes the returned Folder."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.create_folder.return_value = Folder(name="MyFolder")

    result = await create_folder(ctx, name="MyFolder")

    assert result["name"] == "MyFolder"
    imap_client.create_folder.assert_called_once_with(name="MyFolder")


async def test_rename_folder_tool(mock_ctx: MockCtx) -> None:
    """rename_folder tool delegates old_name/new_name and serializes the returned Folder."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.rename_folder.return_value = Folder(name="NewName")

    result = await rename_folder(ctx, old_name="OldName", new_name="NewName")

    assert result["name"] == "NewName"
    imap_client.rename_folder.assert_called_once_with(old_name="OldName", new_name="NewName")


async def test_delete_folder_tool(mock_ctx: MockCtx) -> None:
    """delete_folder tool delegates name to imap_client.delete_folder and returns result."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.delete_folder.return_value = {"status": "deleted", "name": "MyFolder"}

    result = await delete_folder(ctx, name="MyFolder")

    assert result == {"status": "deleted", "name": "MyFolder"}
    imap_client.delete_folder.assert_called_once_with(name="MyFolder")


async def test_get_folder_stats_tool(mock_ctx: MockCtx) -> None:
    """get_folder_stats tool delegates to imap_client.get_folder_stats and serializes model."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.get_folder_stats.return_value = FolderStats(
        folder="INBOX", total_count=42, unread_count=3
    )

    result = await get_folder_stats(ctx, folder="INBOX")

    assert result == {"folder": "INBOX", "total_count": 42, "unread_count": 3}
    imap_client.get_folder_stats.assert_called_once_with(folder="INBOX")


async def test_mark_as_read_tool(mock_ctx: MockCtx) -> None:
    """mark_as_read tool delegates folder/uid to imap_client.mark_as_read."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.mark_as_read.return_value = {"status": "marked_as_read", "uid": "42"}

    result = await mark_as_read(ctx, folder="INBOX", uid="42")

    assert result == {"status": "marked_as_read", "uid": "42"}
    imap_client.mark_as_read.assert_called_once_with(folder="INBOX", uid="42")


async def test_mark_as_unread_tool(mock_ctx: MockCtx) -> None:
    """mark_as_unread tool delegates folder/uid to imap_client.mark_as_unread."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.mark_as_unread.return_value = {"status": "marked_as_unread", "uid": "42"}

    result = await mark_as_unread(ctx, folder="INBOX", uid="42")

    assert result == {"status": "marked_as_unread", "uid": "42"}
    imap_client.mark_as_unread.assert_called_once_with(folder="INBOX", uid="42")


async def test_flag_email_tool(mock_ctx: MockCtx) -> None:
    """flag_email tool delegates folder/uid to imap_client.flag_email."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.flag_email.return_value = {"status": "flagged", "uid": "42"}

    result = await flag_email(ctx, folder="INBOX", uid="42")

    assert result == {"status": "flagged", "uid": "42"}
    imap_client.flag_email.assert_called_once_with(folder="INBOX", uid="42")


async def test_unflag_email_tool(mock_ctx: MockCtx) -> None:
    """unflag_email tool delegates folder/uid to imap_client.unflag_email."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.unflag_email.return_value = {"status": "unflagged", "uid": "42"}

    result = await unflag_email(ctx, folder="INBOX", uid="42")

    assert result == {"status": "unflagged", "uid": "42"}
    imap_client.unflag_email.assert_called_once_with(folder="INBOX", uid="42")


async def test_bulk_action_tool(mock_ctx: MockCtx) -> None:
    """bulk_action tool delegates all params to imap_client.bulk_action."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.bulk_action.return_value = {"status": "bulk_mark_as_read", "uids": ["42", "43"]}

    result = await bulk_action(ctx, folder="INBOX", uids=["42", "43"], action="mark_as_read")

    assert result == {"status": "bulk_mark_as_read", "uids": ["42", "43"]}
    imap_client.bulk_action.assert_called_once_with(
        folder="INBOX", uids=["42", "43"], action="mark_as_read", destination=None
    )


async def test_list_attachments_tool(mock_ctx: MockCtx) -> None:
    """list_attachments tool delegates folder/uid to imap_client and serializes results."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.list_attachments.return_value = [
        Attachment(filename="report.pdf", content_type="application/pdf", size=38000)
    ]

    result = await list_attachments(ctx, folder="INBOX", uid="42")

    assert result == [{"filename": "report.pdf", "content_type": "application/pdf", "size": 38000}]
    imap_client.list_attachments.assert_called_once_with(folder="INBOX", uid="42")


async def test_download_attachment_tool(
    mock_ctx: MockCtx,
) -> None:
    """download_attachment tool delegates folder/uid/filename to imap_client."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.download_attachment.return_value = {
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "data": "ZmFrZQ==",
    }

    result = await download_attachment(ctx, folder="INBOX", uid="42", filename="report.pdf")

    assert result["filename"] == "report.pdf"
    assert result["content_type"] == "application/pdf"
    assert result["data"] == "ZmFrZQ=="
    imap_client.download_attachment.assert_called_once_with(
        folder="INBOX", uid="42", filename="report.pdf"
    )


async def test_lifespan_init_and_close() -> None:
    """app_lifespan initializes the IMAP pool on enter and closes it on exit."""
    mock_settings = MagicMock()
    mock_settings.imap_pool_size = 1
    mock_pool = AsyncMock()
    mock_smtp_client: Any = MagicMock()

    mock_rules: Any = MagicMock()
    mock_caldav = AsyncMock()
    mock_eventkit = AsyncMock()

    with patch("icloud_mcp.server.get_settings", return_value=mock_settings):
        with patch("icloud_mcp.server.IMAPConnectionPool", return_value=mock_pool):
            with patch("icloud_mcp.server.SMTPClient", return_value=mock_smtp_client):
                with patch("icloud_mcp.server.RulesEngine", return_value=mock_rules):
                    with patch("icloud_mcp.server.CalDAVClient", return_value=mock_caldav):
                        with patch("icloud_mcp.server.EventKitClient", return_value=mock_eventkit):
                            async with app_lifespan(mcp) as ctx:
                                mock_pool.initialize.assert_called_once()
                                mock_caldav.connect.assert_called_once()
                                mock_eventkit.connect.assert_called_once()
                                assert isinstance(ctx.imap_client, IMAPClient)
                                assert ctx.smtp_client is mock_smtp_client
                                assert ctx.rules_engine is mock_rules
                                assert ctx.caldav_client is mock_caldav
                                assert ctx.eventkit_client is mock_eventkit

                        mock_pool.close.assert_called_once()
                        mock_caldav.close.assert_called_once()
                        mock_eventkit.close.assert_called_once()


async def test_save_draft_tool(
    mock_ctx: MockCtx,
) -> None:
    """save_draft tool delegates to imap_client.save_draft with correct kwargs."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.save_draft.return_value = {"status": "saved", "folder": "Drafts", "uid": "456"}

    result = await save_draft(ctx, to=["to@example.com"], subject="Test", body="Body")

    assert result == {"status": "saved", "folder": "Drafts", "uid": "456"}
    imap_client.save_draft.assert_called_once_with(
        to=["to@example.com"], subject="Test", body="Body", cc=None
    )


async def test_reply_email_tool(mock_ctx: MockCtx) -> None:
    """reply_email tool fetches the original via imap_client and delegates to smtp_client."""
    ctx, imap_client, smtp_client, _ = mock_ctx
    original = Email(uid="10", folder="INBOX", subject="Hello", sender="alice@example.com")
    imap_client.get_email.return_value = original
    smtp_client.reply_email.return_value = {"status": "sent", "message_id": "<r@mail>"}

    result = await reply_email(ctx, folder="INBOX", uid="10", body="My reply.")

    assert result == {"status": "sent", "message_id": "<r@mail>"}
    imap_client.get_email.assert_called_once_with(folder="INBOX", uid="10")
    smtp_client.reply_email.assert_called_once_with(
        original=original, body="My reply.", reply_all=False
    )


async def test_forward_email_tool(mock_ctx: MockCtx) -> None:
    """forward_email tool fetches the original via imap_client and delegates to smtp_client."""
    ctx, imap_client, smtp_client, _ = mock_ctx
    original = Email(uid="10", folder="INBOX", subject="Hello", sender="alice@example.com")
    imap_client.get_email.return_value = original
    smtp_client.forward_email.return_value = {"status": "sent", "message_id": "<f@mail>"}

    result = await forward_email(ctx, folder="INBOX", uid="10", to=["dave@example.com"])

    assert result == {"status": "sent", "message_id": "<f@mail>"}
    imap_client.get_email.assert_called_once_with(folder="INBOX", uid="10")
    smtp_client.forward_email.assert_called_once_with(
        original=original, to=["dave@example.com"], body=None
    )


async def test_list_rules_tool(mock_ctx: MockCtx) -> None:
    """list_rules tool uses the shared RulesEngine from AppContext."""
    ctx, _, _, _ = mock_ctx
    result = await list_rules(ctx)
    assert result == []


async def test_create_rule_tool(mock_ctx: MockCtx) -> None:
    """create_rule tool delegates name/conditions/actions to shared RulesEngine."""
    ctx, _, _, _ = mock_ctx
    result = await create_rule(
        ctx,
        name="test",
        conditions=[{"field": "sender", "operator": "equals", "value": "x"}],
        actions=[{"action_type": "mark_as_read"}],
    )
    assert result["name"] == "test"


async def test_delete_rule_tool(mock_ctx: MockCtx) -> None:
    """delete_rule tool delegates name to shared RulesEngine.delete_rule."""
    ctx, _, _, _ = mock_ctx
    # First create a rule, then delete it
    await create_rule(
        ctx,
        name="old",
        conditions=[{"field": "sender", "operator": "equals", "value": "x"}],
        actions=[{"action_type": "mark_as_read"}],
    )
    result = await delete_rule(ctx, name="old")
    assert result == {"status": "deleted", "name": "old"}


async def test_apply_rules_tool(mock_ctx: MockCtx) -> None:
    """apply_rules tool delegates folder and imap_client to shared RulesEngine."""
    ctx, imap_client, _, _ = mock_ctx
    imap_client.list_emails.return_value = EmailListResult(emails=[], total_count=0)

    result = await apply_rules(ctx, folder="INBOX")

    assert result == {"processed": 0, "matched": 0, "actions_applied": 0}


async def test_rules_tool_uses_shared_engine(
    mock_ctx: MockCtx,
) -> None:
    """All rule tools use the same RulesEngine instance from AppContext."""
    ctx, _, _, rules_engine = mock_ctx
    # Create via tool
    await create_rule(
        ctx,
        name="shared",
        conditions=[{"field": "sender", "operator": "equals", "value": "x"}],
        actions=[{"action_type": "mark_as_read"}],
    )
    # Verify via direct access — same engine
    assert len(rules_engine.list_rules()) == 1
    # List via tool — should see the same rule
    result = await list_rules(ctx)
    assert len(result) == 1
    assert result[0]["name"] == "shared"


async def test_search_emails_invalid_since_date(
    mock_ctx: MockCtx,
) -> None:
    """search_emails raises ValueError for invalid 'since' date format."""
    ctx, _, _, _ = mock_ctx
    with pytest.raises(ValueError, match="Formato de data inválido para 'since'"):
        await search_emails(ctx, since="not-a-date")


async def test_search_emails_invalid_before_date(
    mock_ctx: MockCtx,
) -> None:
    """search_emails raises ValueError for invalid 'before' date format."""
    ctx, _, _, _ = mock_ctx
    with pytest.raises(ValueError, match="Formato de data inválido para 'before'"):
        await search_emails(ctx, before="31/12/2024")


# -- Calendar (CalDAV) tool handlers ---------------------------------------


def _caldav(ctx: MagicMock) -> AsyncMock:
    """Extract the AsyncMock CalDAVClient from a mock context."""
    client: AsyncMock = ctx.request_context.lifespan_context.caldav_client
    return client


async def test_list_calendars_tool(mock_ctx: MockCtx) -> None:
    """list_calendars delegates to caldav_client and serializes Calendar models."""
    ctx, *_ = mock_ctx
    caldav = _caldav(ctx)
    caldav.list_calendars.return_value = [
        Calendar(name="Work", url="https://p1/cal/work/", color="#FF0000"),
    ]

    result = await list_calendars(ctx)

    assert result == [
        {
            "name": "Work",
            "url": "https://p1/cal/work/",
            "color": "#FF0000",
            "read_only": False,
        }
    ]
    caldav.list_calendars.assert_called_once()


async def test_list_events_tool_parses_dates(mock_ctx: MockCtx) -> None:
    """list_events parses ISO date strings and forwards datetimes to the client."""
    ctx, *_ = mock_ctx
    caldav = _caldav(ctx)
    caldav.list_events.return_value = [
        CalendarEvent(
            uid="e1",
            calendar="Work",
            summary="Standup",
            start=datetime(2026, 6, 1, 9, tzinfo=UTC),
            end=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
        )
    ]

    result = await list_events(ctx, calendar="Work", start="2026-06-01", end="2026-06-02")

    assert result[0]["uid"] == "e1"
    caldav.list_events.assert_called_once_with(
        calendar="Work",
        start=datetime(2026, 6, 1),
        end=datetime(2026, 6, 2),
    )


async def test_list_events_tool_invalid_date(mock_ctx: MockCtx) -> None:
    """list_events raises ValueError on a malformed date string."""
    ctx, *_ = mock_ctx
    with pytest.raises(ValueError, match="Formato de data/hora inválido para 'start'"):
        await list_events(ctx, calendar="Work", start="nope", end="2026-06-02")


async def test_get_event_tool(mock_ctx: MockCtx) -> None:
    """get_event delegates to caldav_client and serializes the event."""
    ctx, *_ = mock_ctx
    caldav = _caldav(ctx)
    caldav.get_event.return_value = CalendarEvent(
        uid="e1",
        calendar="Work",
        summary="Standup",
        start=datetime(2026, 6, 1, 9, tzinfo=UTC),
        end=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
    )

    result = await get_event(ctx, calendar="Work", uid="e1")

    assert result["uid"] == "e1"
    caldav.get_event.assert_called_once_with(calendar="Work", uid="e1")


async def test_create_event_tool(mock_ctx: MockCtx) -> None:
    """create_event parses dates and forwards all fields to the client."""
    ctx, *_ = mock_ctx
    caldav = _caldav(ctx)
    caldav.create_event.return_value = CalendarEvent(
        uid="new",
        calendar="Work",
        summary="Lunch",
        start=datetime(2026, 6, 1, 12, tzinfo=UTC),
        end=datetime(2026, 6, 1, 13, tzinfo=UTC),
    )

    result = await create_event(
        ctx,
        calendar="Work",
        summary="Lunch",
        start="2026-06-01T12:00:00",
        end="2026-06-01T13:00:00",
        location="Cafe",
    )

    assert result["uid"] == "new"
    caldav.create_event.assert_called_once_with(
        calendar="Work",
        summary="Lunch",
        start=datetime(2026, 6, 1, 12),
        end=datetime(2026, 6, 1, 13),
        all_day=False,
        location="Cafe",
        description=None,
        rrule=None,
    )


async def test_create_event_tool_forwards_rrule(mock_ctx: MockCtx) -> None:
    """create_event forwards the rrule argument to the client."""
    ctx, *_ = mock_ctx
    caldav = _caldav(ctx)
    caldav.create_event.return_value = CalendarEvent(
        uid="rec",
        calendar="Work",
        summary="Standup",
        start=datetime(2026, 6, 1, 9, tzinfo=UTC),
        end=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
        rrule="FREQ=WEEKLY;BYDAY=MO",
        is_recurring=True,
    )

    result = await create_event(
        ctx,
        calendar="Work",
        summary="Standup",
        start="2026-06-01T09:00:00",
        end="2026-06-01T09:30:00",
        rrule="FREQ=WEEKLY;BYDAY=MO",
    )

    assert result["is_recurring"] is True
    assert caldav.create_event.call_args.kwargs["rrule"] == "FREQ=WEEKLY;BYDAY=MO"


async def test_update_event_tool_partial(mock_ctx: MockCtx) -> None:
    """update_event passes only the provided fields, leaving dates as None."""
    ctx, *_ = mock_ctx
    caldav = _caldav(ctx)
    caldav.update_event.return_value = CalendarEvent(
        uid="e1",
        calendar="Work",
        summary="Renamed",
        start=datetime(2026, 6, 1, 9, tzinfo=UTC),
        end=datetime(2026, 6, 1, 10, tzinfo=UTC),
    )

    result = await update_event(ctx, calendar="Work", uid="e1", summary="Renamed")

    assert result["summary"] == "Renamed"
    caldav.update_event.assert_called_once_with(
        calendar="Work",
        uid="e1",
        summary="Renamed",
        start=None,
        end=None,
        all_day=None,
        location=None,
        description=None,
        rrule=None,
    )


async def test_delete_event_tool(mock_ctx: MockCtx) -> None:
    """delete_event delegates to the client and returns its status dict."""
    ctx, *_ = mock_ctx
    caldav = _caldav(ctx)
    caldav.delete_event.return_value = {"status": "deleted", "uid": "e1"}

    result = await delete_event(ctx, calendar="Work", uid="e1")

    assert result == {"status": "deleted", "uid": "e1"}
    caldav.delete_event.assert_called_once_with(calendar="Work", uid="e1")


async def test_update_occurrence_tool(mock_ctx: MockCtx) -> None:
    """update_occurrence parses recurrence_id/dates and forwards them."""
    ctx, *_ = mock_ctx
    caldav = _caldav(ctx)
    caldav.update_occurrence.return_value = CalendarEvent(
        uid="weekly-1",
        calendar="Work",
        summary="Moved",
        start=datetime(2026, 6, 15, 11, tzinfo=UTC),
        end=datetime(2026, 6, 15, 11, 30, tzinfo=UTC),
        is_recurring=True,
        recurrence_id=datetime(2026, 6, 15, 9, tzinfo=UTC),
    )

    result = await update_occurrence(
        ctx,
        calendar="Work",
        uid="weekly-1",
        recurrence_id="2026-06-15T09:00:00",
        summary="Moved",
        start="2026-06-15T11:00:00",
    )

    assert result["summary"] == "Moved"
    caldav.update_occurrence.assert_called_once_with(
        calendar="Work",
        uid="weekly-1",
        recurrence_id=datetime(2026, 6, 15, 9),
        summary="Moved",
        start=datetime(2026, 6, 15, 11),
        end=None,
        location=None,
        description=None,
    )


async def test_delete_occurrence_tool(mock_ctx: MockCtx) -> None:
    """delete_occurrence parses recurrence_id and forwards it."""
    ctx, *_ = mock_ctx
    caldav = _caldav(ctx)
    caldav.delete_occurrence.return_value = {
        "status": "deleted_occurrence",
        "uid": "weekly-1",
        "recurrence_id": "2026-06-08T09:00:00",
    }

    result = await delete_occurrence(
        ctx, calendar="Work", uid="weekly-1", recurrence_id="2026-06-08T09:00:00"
    )

    assert result["status"] == "deleted_occurrence"
    caldav.delete_occurrence.assert_called_once_with(
        calendar="Work",
        uid="weekly-1",
        recurrence_id=datetime(2026, 6, 8, 9),
    )


# -- Reminders (native macOS EventKit) tool handlers -----------------------


def _eventkit(ctx: MagicMock) -> AsyncMock:
    """Extract the AsyncMock EventKitClient from a mock context."""
    client: AsyncMock = ctx.request_context.lifespan_context.eventkit_client
    return client


async def test_list_reminder_lists_tool(mock_ctx: MockCtx) -> None:
    """list_reminder_lists delegates to eventkit_client and serializes models."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.list_reminder_lists.return_value = [
        ReminderList(name="Tasks", identifier="r-tasks"),
    ]

    result = await list_reminder_lists(ctx)

    assert result == [{"name": "Tasks", "identifier": "r-tasks", "color": None, "read_only": False}]
    eventkit.list_reminder_lists.assert_called_once()


async def test_list_reminders_tool(mock_ctx: MockCtx) -> None:
    """list_reminders forwards include_completed and serializes reminders."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.list_reminders.return_value = [
        Reminder(uid="t1", list="Tasks", summary="Buy milk"),
    ]

    result = await list_reminders(ctx, list="Tasks", include_completed=True)

    assert result[0]["uid"] == "t1"
    eventkit.list_reminders.assert_called_once_with(list="Tasks", include_completed=True)


async def test_get_reminder_tool(mock_ctx: MockCtx) -> None:
    """get_reminder delegates to eventkit_client and serializes the reminder."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.get_reminder.return_value = Reminder(uid="t1", list="Tasks", summary="Buy milk")

    result = await get_reminder(ctx, list="Tasks", uid="t1")

    assert result["uid"] == "t1"
    eventkit.get_reminder.assert_called_once_with(list="Tasks", uid="t1")


async def test_create_reminder_tool(mock_ctx: MockCtx) -> None:
    """create_reminder parses due/start and forwards all fields."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.create_reminder.return_value = Reminder(
        uid="new", list="Tasks", summary="Pay rent", due=datetime(2026, 7, 1, 9, tzinfo=UTC)
    )

    result = await create_reminder(
        ctx,
        list="Tasks",
        summary="Pay rent",
        due="2026-07-01T09:00:00",
        priority=1,
    )

    assert result["uid"] == "new"
    eventkit.create_reminder.assert_called_once_with(
        list="Tasks",
        summary="Pay rent",
        due=datetime(2026, 7, 1, 9),
        start=None,
        all_day=False,
        priority=1,
        description=None,
        url=None,
        rrule=None,
        alarms=None,
    )


async def test_update_reminder_tool(mock_ctx: MockCtx) -> None:
    """update_reminder forwards only the provided fields (None for the rest)."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.update_reminder.return_value = Reminder(uid="t1", list="Tasks", summary="Renamed")

    result = await update_reminder(ctx, list="Tasks", uid="t1", summary="Renamed")

    assert result["summary"] == "Renamed"
    eventkit.update_reminder.assert_called_once_with(
        list="Tasks",
        uid="t1",
        summary="Renamed",
        due=None,
        start=None,
        all_day=None,
        priority=None,
        description=None,
        url=None,
        rrule=None,
        alarms=None,
        clear=None,
    )


async def test_complete_reminder_tool(mock_ctx: MockCtx) -> None:
    """complete_reminder delegates and serializes the completed reminder."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.complete_reminder.return_value = Reminder(
        uid="t1", list="Tasks", summary="Buy milk", completed=True
    )

    result = await complete_reminder(ctx, list="Tasks", uid="t1")

    assert result["completed"] is True
    eventkit.complete_reminder.assert_called_once_with(list="Tasks", uid="t1")


async def test_reopen_reminder_tool(mock_ctx: MockCtx) -> None:
    """reopen_reminder delegates and serializes the reopened reminder."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.reopen_reminder.return_value = Reminder(
        uid="t1", list="Tasks", summary="Buy milk", completed=False
    )

    result = await reopen_reminder(ctx, list="Tasks", uid="t1")

    assert result["completed"] is False
    eventkit.reopen_reminder.assert_called_once_with(list="Tasks", uid="t1")


async def test_delete_reminder_tool(mock_ctx: MockCtx) -> None:
    """delete_reminder delegates to eventkit_client and returns its status dict."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.delete_reminder.return_value = {"status": "deleted", "uid": "t1"}

    result = await delete_reminder(ctx, list="Tasks", uid="t1")

    assert result == {"status": "deleted", "uid": "t1"}
    eventkit.delete_reminder.assert_called_once_with(list="Tasks", uid="t1")


async def test_update_reminder_tool_forwards_clear(mock_ctx: MockCtx) -> None:
    """update_reminder forwards the clear list to the client."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.update_reminder.return_value = Reminder(uid="t1", list="Tasks", summary="x")

    await update_reminder(ctx, list="Tasks", uid="t1", clear=["due", "priority"])

    eventkit.update_reminder.assert_called_once_with(
        list="Tasks",
        uid="t1",
        summary=None,
        due=None,
        start=None,
        all_day=None,
        priority=None,
        description=None,
        url=None,
        rrule=None,
        alarms=None,
        clear=["due", "priority"],
    )


async def test_move_reminder_tool(mock_ctx: MockCtx) -> None:
    """move_reminder delegates uid/from_list/to_list and serializes the result."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.move_reminder.return_value = Reminder(uid="t1", list="Personal", summary="x")

    result = await move_reminder(ctx, uid="t1", from_list="Tasks", to_list="Personal")

    assert result["list"] == "Personal"
    eventkit.move_reminder.assert_called_once_with(uid="t1", from_list="Tasks", to_list="Personal")


async def test_create_reminder_list_tool(mock_ctx: MockCtx) -> None:
    """create_reminder_list delegates name/color and serializes the list."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.create_reminder_list.return_value = ReminderList(
        name="Groceries", identifier="r-groc", color="#00FF00"
    )

    result = await create_reminder_list(ctx, name="Groceries", color="#00FF00")

    assert result["name"] == "Groceries"
    eventkit.create_reminder_list.assert_called_once_with(name="Groceries", color="#00FF00")


async def test_rename_reminder_list_tool(mock_ctx: MockCtx) -> None:
    """rename_reminder_list delegates name/new_name and serializes the list."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.rename_reminder_list.return_value = ReminderList(name="To Do", identifier="r-tasks")

    result = await rename_reminder_list(ctx, name="Tasks", new_name="To Do")

    assert result["name"] == "To Do"
    eventkit.rename_reminder_list.assert_called_once_with(name="Tasks", new_name="To Do")


async def test_delete_reminder_list_tool(mock_ctx: MockCtx) -> None:
    """delete_reminder_list forwards confirm and returns its status dict."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.delete_reminder_list.return_value = {"status": "deleted_list", "list": "Tasks"}

    result = await delete_reminder_list(ctx, name="Tasks", confirm=True)

    assert result == {"status": "deleted_list", "list": "Tasks"}
    eventkit.delete_reminder_list.assert_called_once_with(name="Tasks", confirm=True)


async def test_search_reminders_tool(mock_ctx: MockCtx) -> None:
    """search_reminders parses date bounds and forwards all filters."""
    ctx, *_ = mock_ctx
    eventkit = _eventkit(ctx)
    eventkit.search_reminders.return_value = [
        Reminder(uid="t1", list="Tasks", summary="Overdue", due=datetime(2026, 6, 10, tzinfo=UTC)),
    ]

    result = await search_reminders(
        ctx, due_before="2026-06-18", undated=False, lists=["Tasks", "Personal"]
    )

    assert result[0]["uid"] == "t1"
    eventkit.search_reminders.assert_called_once_with(
        query=None,
        due_before=datetime(2026, 6, 18),
        due_after=None,
        include_completed=False,
        undated=False,
        lists=["Tasks", "Personal"],
    )
