"""Persistent IMAP connection pool and all email read/search/management operations."""

import asyncio
import email
import email.header
import email.message
import email.utils
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import TypeVar, cast

import aioimaplib
from aioimaplib import IMAP4_SSL

from icloud_mail_mcp.config import ICloudMailSettings
from icloud_mail_mcp.exceptions import IMAPAuthenticationError, IMAPConnectionError
from icloud_mail_mcp.models import Attachment, Email, Folder, SearchQuery

T = TypeVar("T")
log = logging.getLogger(__name__)

ICLOUD_TRASH_FOLDER = "Deleted Messages"
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)

_MONTHS: dict[int, str] = {
    1: "Jan",
    2: "Feb",
    3: "Mar",
    4: "Apr",
    5: "May",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dec",
}

_LIST_PATTERN = re.compile(rb'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)')
_FETCH_PATTERN = re.compile(rb"\d+ FETCH \(")
_UID_PATTERN = re.compile(rb"UID (\d+)")
_FLAGS_PATTERN = re.compile(rb"FLAGS \(([^)]*)\)")


def _decode_header(value: str | None) -> str:
    """Decode an RFC 2047-encoded email header value to a plain string.

    Args:
        value: Raw header string, possibly RFC 2047 encoded, or None.

    Returns:
        Decoded string, or an empty string if decoding fails or value is None.
    """
    if value is None:
        return ""
    try:
        parts = email.header.decode_header(value)
        decoded: list[str] = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(part)
        return "".join(decoded)
    except Exception:
        return ""


def _date_to_imap(d: date) -> str:
    """Format a date as DD-Mon-YYYY for IMAP SEARCH commands.

    Avoids locale-dependent strftime("%b") by using a fixed month map.

    Args:
        d: The date to format.

    Returns:
        IMAP-formatted date string (e.g. ``"01-Jan-2024"``).
    """
    return f"{d.day:02d}-{_MONTHS[d.month]}-{d.year}"


def _parse_list_line(line: bytes) -> Folder | None:
    """Parse a single IMAP LIST response line into a Folder model.

    Args:
        line: A bytes line from the LIST response (without the ``* LIST`` prefix).

    Returns:
        A Folder instance, or None if the line does not match the expected format.
    """
    m = _LIST_PATTERN.match(line)
    if not m:
        return None
    flags_raw = m.group("flags").decode("utf-8", errors="replace")
    flags = [f for f in flags_raw.split() if f]
    delim = m.group("delim").decode("utf-8", errors="replace")
    name_bytes: bytes = m.group("name").strip()
    name = name_bytes.decode("utf-8", errors="replace").strip().strip('"')
    return Folder(name=name, delimiter=delim, flags=flags)


def _parse_fetch_response(
    lines: list[bytes | bytearray],
) -> dict[str, tuple[list[str], bytes]]:
    """Parse a FETCH response into a mapping of UID to (flags, raw_bytes).

    The aioimaplib fetch response interleaves metadata bytes lines with
    literal bytearray objects containing the actual message data.

    Args:
        lines: Raw response lines from a UID FETCH command.

    Returns:
        Dict mapping UID strings to ``(flags_list, message_bytes)`` tuples.
    """
    result: dict[str, tuple[list[str], bytes]] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if isinstance(line, bytes) and _FETCH_PATTERN.search(line):
            uid_m = _UID_PATTERN.search(line)
            if uid_m:
                uid = uid_m.group(1).decode()
                flags: list[str] = []
                flags_m = _FLAGS_PATTERN.search(line)
                if flags_m:
                    flags_raw = flags_m.group(1).decode("utf-8", errors="replace")
                    flags = [f for f in flags_raw.split() if f]
                if i + 1 < len(lines) and isinstance(lines[i + 1], bytearray):
                    result[uid] = (flags, bytes(lines[i + 1]))
                    i += 2
                    continue
        i += 1
    return result


def _parse_email(
    raw_bytes: bytes,
    uid: str,
    folder: str,
    *,
    headers_only: bool = False,
    flags: list[str] | None = None,
) -> Email:
    """Parse raw RFC 822 bytes into an Email model.

    Tolerant to malformed headers and missing fields — falls back to
    empty strings / None rather than raising.

    Args:
        raw_bytes: Raw email bytes (headers only or complete message).
        uid: IMAP UID of the message.
        folder: Folder where the message lives.
        headers_only: If True, skip body and attachment parsing.
        flags: IMAP flags list (used to determine ``is_read``).

    Returns:
        An Email model populated from the raw bytes.
    """
    try:
        msg = email.message_from_bytes(raw_bytes)
    except Exception:
        return Email(uid=uid, folder=folder)

    subject = _decode_header(msg.get("Subject"))
    sender = _decode_header(msg.get("From", ""))
    to_raw = _decode_header(msg.get("To", ""))
    to = [a.strip() for a in to_raw.split(",") if a.strip()]
    cc_raw = _decode_header(msg.get("Cc", ""))
    cc = [a.strip() for a in cc_raw.split(",") if a.strip()]

    date_parsed: datetime | None = None
    try:
        date_str = msg.get("Date", "")
        if date_str:
            date_parsed = email.utils.parsedate_to_datetime(str(date_str))
    except Exception:
        pass

    is_read = "\\Seen" in (flags or [])

    if headers_only:
        return Email(
            uid=uid,
            folder=folder,
            subject=subject,
            sender=sender,
            to=to,
            cc=cc,
            date=date_parsed,
            is_read=is_read,
        )

    body_text = ""
    body_html = ""
    attachments: list[Attachment] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_disp = str(part.get("Content-Disposition", ""))
            filename = part.get_filename()
            content_type = part.get_content_type()

            if filename or content_disp.startswith("attachment"):
                try:
                    payload = cast(bytes | None, part.get_payload(decode=True))
                    size = len(payload) if isinstance(payload, bytes) else None
                    attachments.append(
                        Attachment(
                            filename=str(filename) if filename else "unnamed",
                            content_type=content_type,
                            size=size,
                        )
                    )
                except Exception:
                    pass
            elif content_type == "text/plain" and not body_text:
                try:
                    payload = cast(bytes | None, part.get_payload(decode=True))
                    if isinstance(payload, bytes):
                        charset = part.get_content_charset() or "utf-8"
                        body_text = payload.decode(charset, errors="replace")
                except Exception:
                    pass
            elif content_type == "text/html" and not body_html:
                try:
                    payload = cast(bytes | None, part.get_payload(decode=True))
                    if isinstance(payload, bytes):
                        charset = part.get_content_charset() or "utf-8"
                        body_html = payload.decode(charset, errors="replace")
                except Exception:
                    pass
    else:
        content_type = msg.get_content_type()
        try:
            payload = cast(bytes | None, msg.get_payload(decode=True))
            if isinstance(payload, bytes):
                charset = msg.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                if content_type == "text/html":
                    body_html = text
                else:
                    body_text = text
        except Exception:
            pass

    return Email(
        uid=uid,
        folder=folder,
        subject=subject,
        sender=sender,
        to=to,
        cc=cc,
        date=date_parsed,
        is_read=is_read,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
    )


def _build_search_criteria(query: SearchQuery) -> list[str]:
    """Build IMAP SEARCH criteria tokens from a SearchQuery.

    All non-None fields are combined with implicit AND logic.

    Args:
        query: The search parameters.

    Returns:
        List of IMAP SEARCH tokens
        (e.g. ``["FROM", '"alice@example.com"', "SINCE", "01-Jan-2024"]``).
    """
    criteria: list[str] = []
    if query.sender:
        criteria.extend(["FROM", f'"{query.sender}"'])
    if query.subject:
        criteria.extend(["SUBJECT", f'"{query.subject}"'])
    if query.since:
        criteria.extend(["SINCE", _date_to_imap(query.since)])
    if query.before:
        criteria.extend(["BEFORE", _date_to_imap(query.before)])
    if query.body:
        criteria.extend(["BODY", f'"{query.body}"'])
    if not criteria:
        criteria = ["ALL"]
    return criteria


class IMAPConnectionPool:
    """Pool of persistent authenticated IMAP connections.

    Maintains a fixed number of open IMAP connections and reuses them
    across operations. Automatically reconnects stale connections
    (iCloud disconnects after ~30 minutes of inactivity).

    Attributes:
        _settings: Application settings with credentials and pool configuration.
        _queue: Queue of available IMAP connections.

    Example:
        pool = IMAPConnectionPool(settings)
        await pool.initialize()
        async with pool.acquire() as conn:
            await conn.select("INBOX")
        await pool.close()
    """

    def __init__(self, settings: ICloudMailSettings) -> None:
        self._settings = settings
        self._queue: asyncio.Queue[IMAP4_SSL] = asyncio.Queue(maxsize=settings.imap_pool_size)

    async def _create_connection(self) -> IMAP4_SSL:
        """Create and authenticate a new IMAP connection.

        Returns:
            An authenticated IMAP4_SSL connection ready for use.

        Raises:
            IMAPAuthenticationError: If the server rejects the credentials.
            IMAPConnectionError: If the connection times out or a transport error occurs.
        """
        try:
            conn = IMAP4_SSL(
                host=self._settings.imap_host,
                port=self._settings.imap_port,
                timeout=float(self._settings.imap_timeout),
            )
            await conn.wait_hello_from_server()
            response = await conn.login(
                self._settings.icloud_email,
                self._settings.icloud_app_password,
            )
            if response.result != "OK":
                raise IMAPAuthenticationError("Credenciais inválidas para o servidor IMAP.")
            return conn
        except IMAPAuthenticationError:
            raise
        except TimeoutError as exc:
            raise IMAPConnectionError("Timeout ao conectar ao servidor IMAP.") from exc
        except aioimaplib.AioImapException as exc:
            raise IMAPConnectionError(f"Erro IMAP ao conectar: {exc}") from exc

    async def _health_check(self, conn: IMAP4_SSL) -> bool:
        """Check if a connection is still alive via NOOP.

        Uses the internal timeout of aioimaplib — no additional wrapping needed.

        Args:
            conn: The connection to check.

        Returns:
            True if the connection responded with OK, False if stale or errored.
        """
        try:
            response = await conn.noop()
            return bool(response.result == "OK")
        except Exception:
            return False

    async def _retry_operation(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Execute an async callable with exponential backoff retry.

        Attempts the operation up to 3 times with delays of 1s, 2s, 4s.
        Authentication errors are propagated immediately without retry.

        Args:
            fn: Zero-argument async callable to execute.

        Returns:
            The return value of the callable on success.

        Raises:
            IMAPAuthenticationError: Propagated immediately without retry.
            IMAPConnectionError: After all retry attempts are exhausted.
        """
        last_exc: Exception = RuntimeError("unreachable")
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            try:
                return await fn()
            except IMAPAuthenticationError:
                raise
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "Tentativa %d/%d falhou: %s. Aguardando %.1fs antes de tentar novamente.",
                    attempt,
                    len(RETRY_DELAYS),
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
        raise IMAPConnectionError(f"Falha após {len(RETRY_DELAYS)} tentativas.") from last_exc

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[IMAP4_SSL]:
        """Async context manager that yields a healthy IMAP connection from the pool.

        Retrieves a connection from the queue, health-checks it (reconnecting
        if stale), yields it for use, then returns it to the pool. On error,
        discards the connection and inserts a fresh replacement.

        Yields:
            A healthy, authenticated IMAP4_SSL connection.

        Raises:
            IMAPConnectionError: If a healthy connection cannot be obtained or replaced.
        """
        conn = await self._queue.get()
        try:
            if not await self._health_check(conn):
                log.info("Conexão IMAP obsoleta — reconectando.")
                try:
                    await conn.logout()
                except Exception:
                    pass
                conn = await self._retry_operation(self._create_connection)
            yield conn
        except Exception:
            try:
                await conn.logout()
            except Exception:
                pass
            try:
                replacement = await self._retry_operation(self._create_connection)
                await self._queue.put(replacement)
            except Exception:
                log.error("Falha ao substituir conexão IMAP após erro.")
            raise
        else:
            await self._queue.put(conn)

    async def initialize(self) -> None:
        """Create the initial pool of authenticated IMAP connections.

        Must be called once before any calls to ``acquire()``.

        Raises:
            IMAPAuthenticationError: If credentials are invalid.
            IMAPConnectionError: If connections cannot be established after retries.
        """
        for _ in range(self._settings.imap_pool_size):
            conn = await self._retry_operation(self._create_connection)
            await self._queue.put(conn)

    async def close(self) -> None:
        """Gracefully logout and close all connections in the pool."""
        while True:
            try:
                conn = self._queue.get_nowait()
                try:
                    await conn.logout()
                except Exception:
                    pass
            except asyncio.QueueEmpty:
                break


class IMAPClient:
    """High-level IMAP client for email read, search, and management operations.

    Delegates all connection handling to an ``IMAPConnectionPool``. Each public
    method acquires a connection, performs its operation, and releases it back
    to the pool. No IMAP state persists between calls.

    Attributes:
        _pool: The underlying connection pool.

    Example:
        client = IMAPClient(pool)
        folders = await client.list_folders()
    """

    def __init__(self, pool: IMAPConnectionPool) -> None:
        self._pool = pool

    async def list_folders(self) -> list[Folder]:
        """List all available IMAP mailbox folders.

        Returns:
            List of Folder models.

        Raises:
            IMAPConnectionError: If the LIST command fails.
        """
        async with self._pool.acquire() as conn:
            response = await conn.list("", "*")
        if response.result != "OK":
            raise IMAPConnectionError("Falha ao listar pastas IMAP.")
        return [
            f
            for line in response.lines
            if isinstance(line, bytes)
            if (f := _parse_list_line(line)) is not None
        ]

    async def list_emails(self, folder: str, limit: int = 20, offset: int = 0) -> list[Email]:
        """List emails in a folder with offset-based pagination (newest first).

        Args:
            folder: Folder name (e.g. ``"INBOX"``).
            limit: Maximum number of emails to return.
            offset: Number of emails to skip, counted from the newest.

        Returns:
            List of Email models (headers only), ordered newest-first.

        Raises:
            IMAPConnectionError: If SELECT or FETCH operations fail.
        """
        async with self._pool.acquire() as conn:
            await conn.select(aioimaplib.quoted(folder))
            search_resp = await conn.uid_search("ALL", charset=None)
            all_uids = [
                p
                for line in search_resp.lines
                if isinstance(line, bytes)
                for p in line.decode().split()
                if p.isdigit()
            ]
            all_uids.reverse()  # UIDs are monotonically increasing; highest UID = newest
            page_uids = all_uids[offset : offset + limit]
            if not page_uids:
                return []
            uid_set = ",".join(page_uids)
            fetch_resp = await conn.uid(
                "FETCH",
                uid_set,
                "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE)])",
            )
        parsed = _parse_fetch_response(fetch_resp.lines)
        return [
            _parse_email(hdr_bytes, uid, folder, headers_only=True, flags=flags)
            for uid, (flags, hdr_bytes) in parsed.items()
        ]

    async def get_email(self, folder: str, uid: str) -> Email:
        """Fetch a complete email by UID including body and attachment metadata.

        Args:
            folder: Folder containing the email.
            uid: IMAP UID of the message.

        Returns:
            Full Email model with body text, HTML, and attachment metadata.

        Raises:
            IMAPConnectionError: If the email is not found or the FETCH fails.
        """
        async with self._pool.acquire() as conn:
            await conn.select(aioimaplib.quoted(folder))
            response = await conn.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
        parsed = _parse_fetch_response(response.lines)
        if uid not in parsed:
            raise IMAPConnectionError(f"Email UID {uid} não encontrado em '{folder}'.")
        flags, raw_bytes = parsed[uid]
        return _parse_email(raw_bytes, uid, folder, flags=flags)

    async def search_emails(self, query: SearchQuery) -> list[Email]:
        """Search emails using IMAP SEARCH criteria.

        All non-None query fields are combined with implicit AND logic.

        Args:
            query: SearchQuery specifying folder, optional filters, and result limit.

        Returns:
            List of matching Email models (headers only), newest-first.

        Raises:
            IMAPConnectionError: If SELECT or SEARCH operations fail.
        """
        async with self._pool.acquire() as conn:
            await conn.select(aioimaplib.quoted(query.folder))
            criteria = _build_search_criteria(query)
            search_resp = await conn.uid_search(*criteria, charset=None)
            all_uids = [
                p
                for line in search_resp.lines
                if isinstance(line, bytes)
                for p in line.decode().split()
                if p.isdigit()
            ]
            all_uids.reverse()
            page_uids = all_uids[: query.limit]
            if not page_uids:
                return []
            uid_set = ",".join(page_uids)
            fetch_resp = await conn.uid(
                "FETCH",
                uid_set,
                "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE)])",
            )
        parsed = _parse_fetch_response(fetch_resp.lines)
        return [
            _parse_email(hdr_bytes, uid, query.folder, headers_only=True, flags=flags)
            for uid, (flags, hdr_bytes) in parsed.items()
        ]

    async def move_email(self, folder: str, uid: str, destination: str) -> dict[str, str]:
        """Move an email to another folder using COPY then delete original.

        Args:
            folder: Source folder containing the email.
            uid: IMAP UID of the message.
            destination: Target folder name.

        Returns:
            Dict with ``status``, ``uid``, and ``destination`` keys.

        Raises:
            IMAPConnectionError: If the COPY or delete operation fails.
        """
        async with self._pool.acquire() as conn:
            await conn.select(aioimaplib.quoted(folder))
            copy_resp = await conn.uid("COPY", uid, aioimaplib.quoted(destination))
            if copy_resp.result != "OK":
                raise IMAPConnectionError(f"Falha ao copiar email {uid} para '{destination}'.")
            store_resp = await conn.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")
            if store_resp.result != "OK":
                raise IMAPConnectionError(f"Falha ao marcar email {uid} como deletado.")
            await conn.expunge()
        return {"status": "moved", "uid": uid, "destination": destination}

    async def delete_email(self, folder: str, uid: str) -> dict[str, str]:
        """Move an email to the iCloud Trash folder (``"Deleted Messages"``).

        Args:
            folder: Source folder containing the email.
            uid: IMAP UID of the message.

        Returns:
            Dict with ``status``, ``uid``, and ``destination`` keys.

        Raises:
            IMAPConnectionError: If the move operation fails.
        """
        return await self.move_email(folder, uid, ICLOUD_TRASH_FOLDER)

    async def create_folder(self, name: str) -> Folder:
        """Create a new IMAP mailbox folder.

        Args:
            name: Name for the new folder.

        Returns:
            Folder model for the newly created folder.

        Raises:
            IMAPConnectionError: If the CREATE command fails.
        """
        async with self._pool.acquire() as conn:
            response = await conn.create(name)
        if response.result != "OK":
            raise IMAPConnectionError(f"Falha ao criar a pasta '{name}'.")
        return Folder(name=name)
