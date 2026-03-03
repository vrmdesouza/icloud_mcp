"""MCP server wiring: tool registration, lifespan, and client orchestration."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from icloud_mail_mcp.config import get_settings
from icloud_mail_mcp.imap_client import IMAPClient, IMAPConnectionPool
from icloud_mail_mcp.models import SearchQuery
from icloud_mail_mcp.rules import RulesEngine
from icloud_mail_mcp.smtp_client import SMTPClient

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Holds live client instances for the duration of the server process."""

    imap_client: IMAPClient
    smtp_client: SMTPClient
    rules_engine: RulesEngine


@asynccontextmanager
async def app_lifespan(app: FastMCP[AppContext]) -> AsyncIterator[AppContext]:
    """Initialize the IMAP pool and wire up clients on server start."""
    settings = get_settings()
    pool = IMAPConnectionPool(settings)
    log.info("Inicializando pool de conexões IMAP...")
    await pool.initialize()
    log.info("Pool IMAP inicializado com %d conexões.", settings.imap_pool_size)
    try:
        yield AppContext(
            imap_client=IMAPClient(pool),
            smtp_client=SMTPClient(settings),
            rules_engine=RulesEngine(),
        )
    finally:
        log.info("Encerrando pool de conexões IMAP...")
        await pool.close()


mcp: FastMCP[AppContext] = FastMCP("icloud-mail-mcp", lifespan=app_lifespan)


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
