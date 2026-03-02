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
from icloud_mail_mcp.smtp_client import SMTPClient

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Holds live client instances for the duration of the server process."""

    imap_client: IMAPClient
    smtp_client: SMTPClient


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
) -> list[dict[str, Any]]:
    """List emails in a folder with pagination, ordered newest first."""
    app = _get_ctx(ctx)
    emails = await app.imap_client.list_emails(folder=folder, limit=limit, offset=offset)
    return [e.model_dump(mode="json") for e in emails]


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
        limit: Maximum number of results (1–100).
    """
    app = _get_ctx(ctx)
    query = SearchQuery(
        folder=folder,
        sender=sender,
        subject=subject,
        since=date.fromisoformat(since) if since else None,
        before=date.fromisoformat(before) if before else None,
        body=body,
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
async def create_folder(ctx: Context, name: str) -> dict[str, Any]:  # type: ignore[type-arg]
    """Create a new iCloud Mail folder."""
    app = _get_ctx(ctx)
    folder = await app.imap_client.create_folder(name=name)
    return folder.model_dump()
