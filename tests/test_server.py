"""Tests for server.py — MCP tool handlers and lifespan."""

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from icloud_mail_mcp.imap_client import IMAPClient
from icloud_mail_mcp.models import Email, Folder
from icloud_mail_mcp.server import (
    AppContext,
    app_lifespan,
    create_folder,
    delete_email,
    get_email,
    list_emails,
    list_folders,
    mcp,
    move_email,
    search_emails,
    send_email,
)


@pytest.fixture
def mock_ctx() -> tuple[MagicMock, AsyncMock, AsyncMock]:
    """MagicMock Context with AppContext carrying AsyncMock IMAP and SMTP clients."""
    imap_client = AsyncMock()
    smtp_client = AsyncMock()
    ctx = MagicMock()
    ctx.request_context.lifespan_context = AppContext(
        imap_client=imap_client,
        smtp_client=smtp_client,
    )
    return ctx, imap_client, smtp_client


async def test_list_folders_tool(mock_ctx: tuple[MagicMock, AsyncMock, AsyncMock]) -> None:
    """list_folders tool calls imap_client.list_folders and serializes Folder models."""
    ctx, imap_client, _ = mock_ctx
    imap_client.list_folders.return_value = [Folder(name="INBOX"), Folder(name="Sent")]

    result = await list_folders(ctx)

    assert result == [
        {"name": "INBOX", "delimiter": "/", "flags": []},
        {"name": "Sent", "delimiter": "/", "flags": []},
    ]
    imap_client.list_folders.assert_called_once()


async def test_list_emails_tool(mock_ctx: tuple[MagicMock, AsyncMock, AsyncMock]) -> None:
    """list_emails tool delegates folder/limit/offset and serializes Email models."""
    ctx, imap_client, _ = mock_ctx
    imap_client.list_emails.return_value = [Email(uid="1", folder="INBOX")]

    result = await list_emails(ctx, folder="INBOX", limit=10, offset=5)

    assert len(result) == 1
    imap_client.list_emails.assert_called_once_with(folder="INBOX", limit=10, offset=5)


async def test_get_email_tool(mock_ctx: tuple[MagicMock, AsyncMock, AsyncMock]) -> None:
    """get_email tool delegates folder/uid and serializes the Email model."""
    ctx, imap_client, _ = mock_ctx
    imap_client.get_email.return_value = Email(uid="42", folder="INBOX", subject="Hello")

    result = await get_email(ctx, folder="INBOX", uid="42")

    assert result["uid"] == "42"
    assert result["subject"] == "Hello"
    imap_client.get_email.assert_called_once_with(folder="INBOX", uid="42")


async def test_search_emails_tool(mock_ctx: tuple[MagicMock, AsyncMock, AsyncMock]) -> None:
    """search_emails tool parses ISO date strings into date objects for SearchQuery."""
    ctx, imap_client, _ = mock_ctx
    imap_client.search_emails.return_value = []

    await search_emails(ctx, folder="INBOX", since="2024-01-01", before="2024-12-31", limit=5)

    call_kwargs = imap_client.search_emails.call_args.kwargs
    query = call_kwargs["query"]
    assert query.since == date(2024, 1, 1)
    assert query.before == date(2024, 12, 31)
    assert query.folder == "INBOX"
    assert query.limit == 5


async def test_send_email_tool(mock_ctx: tuple[MagicMock, AsyncMock, AsyncMock]) -> None:
    """send_email tool delegates all arguments to smtp_client.send_email."""
    ctx, _, smtp_client = mock_ctx
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


async def test_move_email_tool(mock_ctx: tuple[MagicMock, AsyncMock, AsyncMock]) -> None:
    """move_email tool delegates folder/uid/destination to imap_client."""
    ctx, imap_client, _ = mock_ctx
    imap_client.move_email.return_value = {
        "status": "moved",
        "uid": "42",
        "destination": "Archive",
    }

    result = await move_email(ctx, folder="INBOX", uid="42", destination="Archive")

    assert result["status"] == "moved"
    imap_client.move_email.assert_called_once_with(folder="INBOX", uid="42", destination="Archive")


async def test_delete_email_tool(mock_ctx: tuple[MagicMock, AsyncMock, AsyncMock]) -> None:
    """delete_email tool delegates folder/uid to imap_client."""
    ctx, imap_client, _ = mock_ctx
    imap_client.delete_email.return_value = {
        "status": "moved",
        "uid": "7",
        "destination": "Deleted Messages",
    }

    result = await delete_email(ctx, folder="INBOX", uid="7")

    assert result["destination"] == "Deleted Messages"
    imap_client.delete_email.assert_called_once_with(folder="INBOX", uid="7")


async def test_create_folder_tool(mock_ctx: tuple[MagicMock, AsyncMock, AsyncMock]) -> None:
    """create_folder tool delegates name and serializes the returned Folder."""
    ctx, imap_client, _ = mock_ctx
    imap_client.create_folder.return_value = Folder(name="MyFolder")

    result = await create_folder(ctx, name="MyFolder")

    assert result["name"] == "MyFolder"
    imap_client.create_folder.assert_called_once_with(name="MyFolder")


async def test_lifespan_init_and_close() -> None:
    """app_lifespan initializes the IMAP pool on enter and closes it on exit."""
    mock_settings = MagicMock()
    mock_settings.imap_pool_size = 1
    mock_pool = AsyncMock()
    mock_smtp_client: Any = MagicMock()

    with patch("icloud_mail_mcp.server.get_settings", return_value=mock_settings):
        with patch("icloud_mail_mcp.server.IMAPConnectionPool", return_value=mock_pool):
            with patch("icloud_mail_mcp.server.SMTPClient", return_value=mock_smtp_client):
                async with app_lifespan(mcp) as ctx:
                    mock_pool.initialize.assert_called_once()
                    assert isinstance(ctx.imap_client, IMAPClient)
                    assert ctx.smtp_client is mock_smtp_client

                mock_pool.close.assert_called_once()
