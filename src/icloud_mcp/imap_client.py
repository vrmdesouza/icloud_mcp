"""Persistent IMAP connection pool and all email read/search/management operations."""

import asyncio
import base64
import email
import email.header
import email.message
import email.utils
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any, TypeVar, cast

import aioimaplib
from aioimaplib import IMAP4_SSL

from icloud_mcp.config import ICloudMailSettings
from icloud_mcp.exceptions import IMAPAuthenticationError, IMAPConnectionError
from icloud_mcp.models import (
    Attachment,
    Email,
    EmailListResult,
    Folder,
    FolderStats,
    SearchQuery,
)

T = TypeVar("T")
log = logging.getLogger(__name__)

ICLOUD_TRASH_FOLDER = "Deleted Messages"
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)

_BULK_STORE_ACTIONS: dict[str, tuple[str, str]] = {
    "mark_as_read": ("+FLAGS.SILENT", "(\\Seen)"),
    "mark_as_unread": ("-FLAGS.SILENT", "(\\Seen)"),
    "flag": ("+FLAGS.SILENT", "(\\Flagged)"),
    "unflag": ("-FLAGS.SILENT", "(\\Flagged)"),
}

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
_BODYSTRUCTURE_RE = re.compile(rb"BODYSTRUCTURE\s*\(", re.IGNORECASE)

_HEADER_FIELDS = (
    "(FLAGS BODY.PEEK[HEADER.FIELDS "
    "(FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES REPLY-TO)])"
)


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

    message_id = msg.get("Message-ID", "") or None
    in_reply_to = msg.get("In-Reply-To", "") or None
    references = msg.get("References", "") or None
    reply_to = _decode_header(msg.get("Reply-To", "")) or None

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
            message_id=message_id,
            in_reply_to=in_reply_to,
            references=references,
            reply_to=reply_to,
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
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        reply_to=reply_to,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
    )


def _extract_attachment(raw_bytes: bytes, filename: str) -> dict[str, str]:
    """Locate a named attachment in raw email bytes and return its base64-encoded content.

    Args:
        raw_bytes: Raw RFC 822 email bytes containing the message.
        filename: The filename of the attachment to extract.

    Returns:
        Dict with ``filename``, ``content_type``, and ``data`` (base64-encoded ASCII) keys.

    Raises:
        IMAPConnectionError: If the attachment is not found or message parsing fails.
    """
    try:
        msg = email.message_from_bytes(raw_bytes)
    except Exception as exc:
        raise IMAPConnectionError(
            f"Falha ao processar a mensagem para extrair anexo '{filename}'."
        ) from exc
    for part in msg.walk():
        raw_filename = part.get_filename()
        if raw_filename is None:
            continue
        decoded_filename = _decode_header(str(raw_filename))
        if decoded_filename == filename:
            payload = cast(bytes | None, part.get_payload(decode=True))
            data = base64.b64encode(payload if payload is not None else b"").decode("ascii")
            return {
                "filename": decoded_filename,
                "content_type": part.get_content_type(),
                "data": data,
            }
    raise IMAPConnectionError(f"Anexo '{filename}' não encontrado na mensagem.")


def _sanitize_imap_string(value: str) -> str:
    """Remove characters that could break IMAP quoted-string syntax.

    RFC 3501 quoted-strings do not support escaping ``"`` or ``\\`` inside
    the quotes.  Stripping them prevents IMAP search injection.

    Args:
        value: Raw user input string.

    Returns:
        Sanitized string safe for use in IMAP quoted-string context.
    """
    return value.replace("\\", "").replace('"', "")


def _validate_uid(uid: str) -> None:
    """Validate that a UID string is purely numeric.

    Args:
        uid: The UID string to validate.

    Raises:
        ValueError: If ``uid`` contains non-digit characters.
    """
    if not uid.isdigit():
        raise ValueError(f"UID inválido: '{uid}'. O UID deve ser numérico.")


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
        criteria.extend(["FROM", f'"{_sanitize_imap_string(query.sender)}"'])
    if query.subject:
        criteria.extend(["SUBJECT", f'"{_sanitize_imap_string(query.subject)}"'])
    if query.since:
        criteria.extend(["SINCE", _date_to_imap(query.since)])
    if query.before:
        criteria.extend(["BEFORE", _date_to_imap(query.before)])
    if query.body:
        criteria.extend(["BODY", f'"{_sanitize_imap_string(query.body)}"'])
    if query.is_read is True:
        criteria.append("SEEN")
    elif query.is_read is False:
        criteria.append("UNSEEN")
    if query.is_flagged is True:
        criteria.append("FLAGGED")
    elif query.is_flagged is False:
        criteria.append("UNFLAGGED")
    if query.min_size is not None:
        criteria.extend(["LARGER", str(query.min_size)])
    if query.has_attachments is True:
        criteria.extend(["HEADER", "Content-Type", '"multipart/mixed"'])
    elif query.has_attachments is False:
        criteria.extend(["NOT", "HEADER", "Content-Type", '"multipart/mixed"'])
    if not criteria:
        criteria = ["ALL"]
    return criteria


def _tokenize_bodystructure(data: str) -> list[str]:
    """Tokenize an IMAP BODYSTRUCTURE S-expression string into a flat token list.

    Splits on parentheses, quoted strings, NIL, atoms and numbers using
    a positional state machine (no regex).

    Args:
        data: Raw BODYSTRUCTURE string, e.g. ``'(("text" "plain" ...) ...)'``.

    Returns:
        Flat list of tokens where ``(`` and ``)`` are individual tokens,
        quoted strings have their quotes stripped, and atoms are kept as-is.
    """
    tokens: list[str] = []
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        if c in " \t\r\n":
            i += 1
        elif c in "()":
            tokens.append(c)
            i += 1
        elif c == '"':
            i += 1
            start = i
            while i < n and data[i] != '"':
                if data[i] == "\\" and i + 1 < n:
                    i += 1
                i += 1
            tokens.append(data[start:i])
            if i < n:
                i += 1  # skip closing quote
        else:
            start = i
            while i < n and data[i] not in ' \t\r\n()"':
                i += 1
            tokens.append(data[start:i])
    return tokens


def _parse_bodystructure_tokens(tokens: list[str]) -> list[Any]:
    """Parse a flat token list into a nested list representing the BODYSTRUCTURE tree.

    Uses a stack to handle the S-expression structure produced by
    ``_tokenize_bodystructure``.

    Args:
        tokens: Flat token list from ``_tokenize_bodystructure``.

    Returns:
        Nested ``list[Any]`` corresponding to the outermost BODYSTRUCTURE level.
        Returns an empty list if the token list is empty or malformed.
    """
    stack: list[list[Any]] = [[]]
    for token in tokens:
        if token == "(":
            new_list: list[Any] = []
            stack[-1].append(new_list)
            stack.append(new_list)
        elif token == ")":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].append(token)
    root = stack[0]
    return cast(list[Any], root[0]) if root else []


def _extract_attachments_from_bodystructure(parsed: list[Any]) -> list[Attachment]:
    """Recursively extract attachment metadata from a parsed BODYSTRUCTURE tree.

    Handles multipart messages (nested parts) and single-part messages.
    Identifies attachments by Content-Disposition and/or filename presence.

    Args:
        parsed: Nested list from ``_parse_bodystructure_tokens``.

    Returns:
        List of Attachment models extracted from the structure.
    """
    if not parsed:
        return []

    # Multipart: first element is a list (a nested body part)
    if isinstance(parsed[0], list):
        result: list[Attachment] = []
        for item in parsed:
            if isinstance(item, list):
                result.extend(_extract_attachments_from_bodystructure(item))
        return result

    # Single part: first element is the main type string
    if not isinstance(parsed[0], str):
        return []

    type_ = parsed[0].lower()
    subtype = parsed[1].lower() if len(parsed) > 1 and isinstance(parsed[1], str) else ""
    content_type = f"{type_}/{subtype}"

    # Body params at index 2 (list of key-value pairs, or "NIL")
    body_params: list[Any] = parsed[2] if len(parsed) > 2 and isinstance(parsed[2], list) else []

    # Size at index 6 (encoded size in octets as reported by the server)
    size_str = parsed[6] if len(parsed) > 6 and isinstance(parsed[6], str) else "NIL"

    # Disposition index: text/* has an extra "lines" field at [7], shifting disposition to [9]
    disp_idx = 9 if type_ == "text" else 8
    disposition = parsed[disp_idx] if len(parsed) > disp_idx else "NIL"

    # Parse disposition type and filename from disposition extension field
    disp_type: str | None = None
    disp_filename: str | None = None
    if isinstance(disposition, list) and disposition:
        if isinstance(disposition[0], str):
            disp_type = disposition[0].lower()
        raw_disp_params = disposition[1] if len(disposition) > 1 else "NIL"
        disp_list: list[Any] = raw_disp_params if isinstance(raw_disp_params, list) else []
        for i in range(0, len(disp_list) - 1, 2):
            k, v = disp_list[i], disp_list[i + 1]
            if isinstance(k, str) and k.lower() == "filename" and isinstance(v, str):
                disp_filename = v
                break

    # Fallback: look for "name" in body params
    body_filename: str | None = None
    for i in range(0, len(body_params) - 1, 2):
        k, v = body_params[i], body_params[i + 1]
        if isinstance(k, str) and k.lower() == "name" and isinstance(v, str):
            body_filename = v
            break

    filename = disp_filename or body_filename

    # Determine whether this part qualifies as an attachment
    is_attachment = False
    if disp_type == "attachment":
        is_attachment = True
        if filename is None:
            filename = "unnamed"
    elif filename and content_type not in ("text/plain", "text/html"):
        is_attachment = True

    if not is_attachment or filename is None:
        return []

    size: int | None = None
    try:
        size = int(size_str)
    except (ValueError, TypeError):
        pass

    return [Attachment(filename=_decode_header(filename), content_type=content_type, size=size)]


def _extract_bodystructure_data(lines: list[bytes | bytearray]) -> str:
    """Extract the BODYSTRUCTURE parenthesized string from a FETCH response.

    Scans response lines for ``BODYSTRUCTURE (`` and extracts the balanced
    parenthesized substring using a depth counter.

    Args:
        lines: Raw response lines from a UID FETCH (BODYSTRUCTURE) command.

    Returns:
        The balanced parenthesized BODYSTRUCTURE string (e.g. ``"((...) ...)"``).

    Raises:
        IMAPConnectionError: If no BODYSTRUCTURE is found in the response lines.
    """
    for line in lines:
        if not isinstance(line, bytes):
            continue
        m = _BODYSTRUCTURE_RE.search(line)
        if m is None:
            continue
        start = m.end() - 1  # position of the opening '('
        data_bytes = line[start:]
        depth = 0
        end = 0
        for i, ch in enumerate(data_bytes):
            if ch == ord("("):
                depth += 1
            elif ch == ord(")"):
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > 0:
            return data_bytes[:end].decode("ascii", errors="replace")
    raise IMAPConnectionError("Resposta BODYSTRUCTURE não encontrada na resposta FETCH.")


def _build_draft_message(
    sender: str,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
) -> bytes:
    """Construct an RFC 822 email message suitable for IMAP APPEND.

    Args:
        sender: The From address.
        to: List of To recipient addresses.
        subject: Email subject line.
        body: Plain-text email body.
        cc: Optional list of CC recipient addresses.

    Returns:
        The email serialised as bytes.
    """
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content(body)
    return msg.as_bytes()


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
        except OSError as exc:
            raise IMAPConnectionError(f"Erro de rede ao conectar ao servidor IMAP: {exc}") from exc
        except Exception as exc:
            raise IMAPConnectionError(
                f"Erro inesperado ao conectar ao servidor IMAP: {exc}"
            ) from exc

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
        try:
            conn = await asyncio.wait_for(
                self._queue.get(), timeout=float(self._settings.imap_timeout)
            )
        except TimeoutError as exc:
            raise IMAPConnectionError(
                "Timeout ao aguardar conexão disponível no pool IMAP."
            ) from exc
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
                log.error(
                    "Falha ao substituir conexão IMAP após erro. "
                    "O pool encolheu — conexões disponíveis reduzidas."
                )
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

    @property
    def email(self) -> str:
        """Return the configured iCloud email address."""
        return self._settings.icloud_email

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
            response = await conn.list('""', "*")
        if response.result != "OK":
            raise IMAPConnectionError("Falha ao listar pastas IMAP.")
        return [
            f
            for line in response.lines
            if isinstance(line, bytes)
            if (f := _parse_list_line(line)) is not None
        ]

    async def list_emails(
        self, folder: str, limit: int = 20, offset: int = 0, sort_order: str = "desc"
    ) -> EmailListResult:
        """List emails in a folder with offset-based pagination.

        Args:
            folder: Folder name (e.g. ``"INBOX"``).
            limit: Maximum number of emails to return.
            offset: Number of emails to skip, counted from the first position.
            sort_order: ``"desc"`` (default, newest first) or ``"asc"`` (oldest first).

        Returns:
            EmailListResult with paginated emails and total_count reflecting the
            full folder size regardless of limit/offset.

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
            if sort_order == "desc":
                all_uids.reverse()  # UIDs are monotonically increasing; highest UID = newest
            total_count = len(all_uids)
            page_uids = all_uids[offset : offset + limit]
            if not page_uids:
                return EmailListResult(emails=[], total_count=total_count)
            uid_set = ",".join(page_uids)
            fetch_resp = await conn.uid(
                "FETCH",
                uid_set,
                _HEADER_FIELDS,
            )
        parsed = _parse_fetch_response(fetch_resp.lines)
        emails = [
            _parse_email(hdr_bytes, uid, folder, headers_only=True, flags=flags)
            for uid in page_uids
            if uid in parsed
            for flags, hdr_bytes in [parsed[uid]]
        ]
        return EmailListResult(emails=emails, total_count=total_count)

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
        _validate_uid(uid)
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
                _HEADER_FIELDS,
            )
        parsed = _parse_fetch_response(fetch_resp.lines)
        return [
            _parse_email(hdr_bytes, uid, query.folder, headers_only=True, flags=flags)
            for uid in page_uids
            if uid in parsed
            for flags, hdr_bytes in [parsed[uid]]
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
        _validate_uid(uid)
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

    async def mark_as_read(self, folder: str, uid: str) -> dict[str, str]:
        """Mark an email as read by adding the ``\\Seen`` flag.

        Args:
            folder: Folder containing the email.
            uid: IMAP UID of the message.

        Returns:
            Dict with ``status`` and ``uid`` keys.

        Raises:
            IMAPConnectionError: If the STORE command fails.
        """
        _validate_uid(uid)
        async with self._pool.acquire() as conn:
            await conn.select(aioimaplib.quoted(folder))
            response = await conn.uid("STORE", uid, "+FLAGS.SILENT", "(\\Seen)")
            if response.result != "OK":
                raise IMAPConnectionError(f"Falha ao marcar email {uid} como lido.")
        return {"status": "marked_as_read", "uid": uid}

    async def mark_as_unread(self, folder: str, uid: str) -> dict[str, str]:
        """Mark an email as unread by removing the ``\\Seen`` flag.

        Args:
            folder: Folder containing the email.
            uid: IMAP UID of the message.

        Returns:
            Dict with ``status`` and ``uid`` keys.

        Raises:
            IMAPConnectionError: If the STORE command fails.
        """
        _validate_uid(uid)
        async with self._pool.acquire() as conn:
            await conn.select(aioimaplib.quoted(folder))
            response = await conn.uid("STORE", uid, "-FLAGS.SILENT", "(\\Seen)")
            if response.result != "OK":
                raise IMAPConnectionError(f"Falha ao marcar email {uid} como não lido.")
        return {"status": "marked_as_unread", "uid": uid}

    async def flag_email(self, folder: str, uid: str) -> dict[str, str]:
        """Flag an email by adding the ``\\Flagged`` flag (star).

        Args:
            folder: Folder containing the email.
            uid: IMAP UID of the message.

        Returns:
            Dict with ``status`` and ``uid`` keys.

        Raises:
            IMAPConnectionError: If the STORE command fails.
        """
        _validate_uid(uid)
        async with self._pool.acquire() as conn:
            await conn.select(aioimaplib.quoted(folder))
            response = await conn.uid("STORE", uid, "+FLAGS.SILENT", "(\\Flagged)")
            if response.result != "OK":
                raise IMAPConnectionError(f"Falha ao adicionar flag ao email {uid}.")
        return {"status": "flagged", "uid": uid}

    async def unflag_email(self, folder: str, uid: str) -> dict[str, str]:
        """Unflag an email by removing the ``\\Flagged`` flag (star).

        Args:
            folder: Folder containing the email.
            uid: IMAP UID of the message.

        Returns:
            Dict with ``status`` and ``uid`` keys.

        Raises:
            IMAPConnectionError: If the STORE command fails.
        """
        _validate_uid(uid)
        async with self._pool.acquire() as conn:
            await conn.select(aioimaplib.quoted(folder))
            response = await conn.uid("STORE", uid, "-FLAGS.SILENT", "(\\Flagged)")
            if response.result != "OK":
                raise IMAPConnectionError(f"Falha ao remover flag do email {uid}.")
        return {"status": "unflagged", "uid": uid}

    async def bulk_action(
        self,
        folder: str,
        uids: list[str],
        action: str,
        destination: str | None = None,
    ) -> dict[str, Any]:
        """Apply an action to multiple emails by UID in a single IMAP operation.

        Uses native IMAP UID sets (e.g. ``"42,43,44"``) to operate on all
        specified messages in one server round-trip.

        Args:
            folder: Folder containing the emails.
            uids: List of IMAP UIDs to act on.
            action: One of ``mark_as_read``, ``mark_as_unread``, ``flag``,
                ``unflag``, ``move``, or ``delete``.
            destination: Target folder name — required only for ``move``.

        Returns:
            Dict with ``status`` and ``uids`` keys (plus ``destination`` for
            ``move``/``delete`` actions).

        Raises:
            ValueError: If ``uids`` is non-empty but ``action`` is invalid,
                or if ``action="move"`` is used without ``destination``.
            IMAPConnectionError: If any IMAP command fails.
        """
        if not uids:
            return {"status": "no_action", "uids": []}

        for u in uids:
            _validate_uid(u)

        valid_actions = set(_BULK_STORE_ACTIONS) | {"move", "delete"}
        if action not in valid_actions:
            raise ValueError(
                f"Ação inválida: {action}. Ações válidas: {', '.join(sorted(valid_actions))}"
            )

        uid_set = ",".join(uids)

        async with self._pool.acquire() as conn:
            await conn.select(aioimaplib.quoted(folder))

            if action in _BULK_STORE_ACTIONS:
                flag_op, flag_value = _BULK_STORE_ACTIONS[action]
                response = await conn.uid("STORE", uid_set, flag_op, flag_value)
                if response.result != "OK":
                    raise IMAPConnectionError(
                        f"Falha ao executar bulk {action} nos emails {uid_set}."
                    )
                return {"status": f"bulk_{action}", "uids": uids}

            if action == "move":
                if destination is None:
                    raise ValueError("O parâmetro 'destination' é obrigatório para a ação 'move'.")
                copy_resp = await conn.uid("COPY", uid_set, aioimaplib.quoted(destination))
                if copy_resp.result != "OK":
                    raise IMAPConnectionError(
                        f"Falha ao copiar emails {uid_set} para '{destination}'."
                    )
                store_resp = await conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
                if store_resp.result != "OK":
                    raise IMAPConnectionError(f"Falha ao marcar emails {uid_set} como deletados.")
                await conn.expunge()
                return {"status": "bulk_moved", "uids": uids, "destination": destination}

            # action == "delete"
            copy_resp = await conn.uid("COPY", uid_set, aioimaplib.quoted(ICLOUD_TRASH_FOLDER))
            if copy_resp.result != "OK":
                raise IMAPConnectionError(f"Falha ao mover emails {uid_set} para a lixeira.")
            store_resp = await conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
            if store_resp.result != "OK":
                raise IMAPConnectionError(f"Falha ao marcar emails {uid_set} como deletados.")
            await conn.expunge()
            return {"status": "bulk_deleted", "uids": uids, "destination": ICLOUD_TRASH_FOLDER}

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
            response = await conn.create(aioimaplib.quoted(name))
        if response.result != "OK":
            raise IMAPConnectionError(f"Falha ao criar a pasta '{name}'.")
        return Folder(name=name)

    async def rename_folder(self, old_name: str, new_name: str) -> Folder:
        """Rename an existing IMAP mailbox folder.

        Args:
            old_name: Current folder name.
            new_name: Desired new folder name.

        Returns:
            Folder model with the new name.

        Raises:
            IMAPConnectionError: If the RENAME command fails (e.g. folder does not exist).
        """
        async with self._pool.acquire() as conn:
            response = await conn.rename(aioimaplib.quoted(old_name), aioimaplib.quoted(new_name))
        if response.result != "OK":
            raise IMAPConnectionError(f"Falha ao renomear a pasta '{old_name}' para '{new_name}'.")
        return Folder(name=new_name)

    async def delete_folder(self, name: str) -> dict[str, str]:
        """Delete an empty IMAP mailbox folder.

        Verifies that the folder is empty before issuing the DELETE command.
        Refuses to delete folders that still contain messages.

        Args:
            name: Name of the folder to delete.

        Returns:
            Dict with ``{"status": "deleted", "name": name}``.

        Raises:
            IMAPConnectionError: If the folder does not exist, contains messages,
                or the DELETE command fails.
        """
        async with self._pool.acquire() as conn:
            status_resp = await conn.status(aioimaplib.quoted(name), "(MESSAGES)")
            if status_resp.result != "OK":
                raise IMAPConnectionError(f"Falha ao verificar a pasta '{name}'.")
            status_line = b""
            for line in status_resp.lines:
                if isinstance(line, (bytes, bytearray)):
                    status_line = bytes(line)
                    break
            match = re.search(rb"MESSAGES\s+(\d+)", status_line)
            count = int(match.group(1)) if match else 0
            if count > 0:
                raise IMAPConnectionError(
                    f"A pasta '{name}' contém mensagens e não pode ser removida."
                )
            delete_resp = await conn.delete(aioimaplib.quoted(name))
        if delete_resp.result != "OK":
            raise IMAPConnectionError(f"Falha ao remover a pasta '{name}'.")
        return {"status": "deleted", "name": name}

    async def list_attachments(self, folder: str, uid: str) -> list[Attachment]:
        """List attachment metadata for an email without downloading the full message.

        Uses IMAP FETCH BODYSTRUCTURE for efficient metadata retrieval — avoids
        fetching the message body.

        Args:
            folder: Folder containing the email.
            uid: IMAP UID of the message.

        Returns:
            List of Attachment models with filename, content_type, and size.

        Raises:
            IMAPConnectionError: If the BODYSTRUCTURE response cannot be parsed.
        """
        _validate_uid(uid)
        async with self._pool.acquire() as conn:
            await conn.select(aioimaplib.quoted(folder))
            response = await conn.uid("FETCH", uid, "(BODYSTRUCTURE)")
        bodystructure_str = _extract_bodystructure_data(response.lines)
        tokens = _tokenize_bodystructure(bodystructure_str)
        parsed = _parse_bodystructure_tokens(tokens)
        return _extract_attachments_from_bodystructure(parsed)

    async def download_attachment(self, folder: str, uid: str, filename: str) -> dict[str, str]:
        """Download the binary content of a specific email attachment as base64.

        Use ``get_email`` first to see available attachment filenames.

        Args:
            folder: Folder containing the email.
            uid: IMAP UID of the message.
            filename: The filename of the attachment to download.

        Returns:
            Dict with ``filename``, ``content_type``, and ``data`` (base64-encoded) keys.

        Raises:
            IMAPConnectionError: If the email or attachment is not found.
        """
        _validate_uid(uid)
        async with self._pool.acquire() as conn:
            await conn.select(aioimaplib.quoted(folder))
            response = await conn.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
        parsed = _parse_fetch_response(response.lines)
        if uid not in parsed:
            raise IMAPConnectionError(f"Email UID {uid} não encontrado em '{folder}'.")
        _, raw_bytes = parsed[uid]
        return _extract_attachment(raw_bytes, filename)

    async def get_folder_stats(self, folder: str) -> FolderStats:
        """Return message count and unread count for a folder via IMAP STATUS.

        Args:
            folder: Name of the folder to query.

        Returns:
            FolderStats with total_count and unread_count.

        Raises:
            IMAPConnectionError: If the STATUS command fails.
        """
        async with self._pool.acquire() as conn:
            response = await conn.status(folder, "(MESSAGES UNSEEN)")
        if response.result != "OK":
            raise IMAPConnectionError(f"Falha ao obter estatísticas da pasta '{folder}'.")
        status_line = b""
        for line in response.lines:
            if isinstance(line, (bytes, bytearray)):
                status_line = bytes(line)
                break
        messages_match = re.search(rb"MESSAGES\s+(\d+)", status_line)
        unseen_match = re.search(rb"UNSEEN\s+(\d+)", status_line)
        total = int(messages_match.group(1)) if messages_match else 0
        unread = int(unseen_match.group(1)) if unseen_match else 0
        return FolderStats(folder=folder, total_count=total, unread_count=unread)

    async def save_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
    ) -> dict[str, str]:
        """Save a draft email to the Drafts folder via IMAP APPEND.

        Constructs an RFC 822 message from the given parameters and appends
        it to the "Drafts" mailbox with the \\Draft flag set.

        Args:
            to: List of recipient email addresses.
            subject: Email subject line.
            body: Plain-text email body.
            cc: Optional list of CC recipient addresses.

        Returns:
            Dict with status, folder name, and UID of the saved draft.

        Raises:
            IMAPConnectionError: If the APPEND operation fails.
        """
        sender = self._pool.email
        message_bytes = _build_draft_message(sender, to, subject, body, cc)
        async with self._pool.acquire() as conn:
            response = await conn.append(message_bytes, mailbox="Drafts", flags="(\\Draft)")
        if response.result != "OK":
            raise IMAPConnectionError("Falha ao salvar rascunho na pasta 'Drafts'.")
        uid = ""
        for line in response.lines:
            if isinstance(line, (bytes, bytearray)):
                m = re.search(rb"APPENDUID\s+\d+\s+(\d+)", bytes(line))
                if m:
                    uid = m.group(1).decode()
                    break
        return {"status": "saved", "folder": "Drafts", "uid": uid}
