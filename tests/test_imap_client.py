"""Tests for imap_client.py — IMAP connection pool and email operations."""

from collections.abc import AsyncGenerator, Callable
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from icloud_mail_mcp.config import ICloudMailSettings
from icloud_mail_mcp.exceptions import IMAPAuthenticationError, IMAPConnectionError
from icloud_mail_mcp.imap_client import IMAPClient, IMAPConnectionPool
from icloud_mail_mcp.models import SearchQuery

# ─────────────────────────────────────────────────────────────────────────────
# Local fixture
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def imap_client_and_conn(
    settings: ICloudMailSettings,
    mock_imap_conn: AsyncMock,
) -> AsyncGenerator[tuple[IMAPClient, AsyncMock], None]:
    """Initialized IMAPClient backed by a fully mocked connection pool."""
    with patch("icloud_mail_mcp.imap_client.IMAP4_SSL", return_value=mock_imap_conn):
        with patch("icloud_mail_mcp.imap_client.RETRY_DELAYS", (0.0, 0.0, 0.0)):
            pool = IMAPConnectionPool(settings)
            await pool.initialize()
            client = IMAPClient(pool)
            yield client, mock_imap_conn
            await pool.close()


# ─────────────────────────────────────────────────────────────────────────────
# Pool tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_pool_init(settings: ICloudMailSettings, mock_imap_conn: AsyncMock) -> None:
    """initialize() creates pool_size connections and fills the internal queue."""
    with patch("icloud_mail_mcp.imap_client.IMAP4_SSL", return_value=mock_imap_conn):
        with patch("icloud_mail_mcp.imap_client.RETRY_DELAYS", (0.0, 0.0, 0.0)):
            pool = IMAPConnectionPool(settings)
            await pool.initialize()
            assert mock_imap_conn.login.call_count == settings.imap_pool_size
            assert pool._queue.qsize() == settings.imap_pool_size
            await pool.close()


async def test_pool_acquire(settings: ICloudMailSettings, mock_imap_conn: AsyncMock) -> None:
    """acquire() yields a connection, shrinks the queue, then restores it on exit."""
    with patch("icloud_mail_mcp.imap_client.IMAP4_SSL", return_value=mock_imap_conn):
        pool = IMAPConnectionPool(settings)
        await pool.initialize()
        initial_size = pool._queue.qsize()

        async with pool.acquire() as conn:
            assert pool._queue.qsize() == initial_size - 1
            assert conn is mock_imap_conn

        assert pool._queue.qsize() == initial_size
        await pool.close()


async def test_pool_acquire_stale_reconnects(
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """A stale connection (NOOP → NO) is logged out and replaced with a fresh one."""
    stale_conn = AsyncMock()
    stale_conn.wait_hello_from_server.return_value = None
    stale_conn.login.return_value = mock_imap_response("OK", [])
    stale_conn.noop.return_value = mock_imap_response("NO", [])  # stale!
    stale_conn.logout.return_value = None

    fresh_conn = AsyncMock()
    fresh_conn.wait_hello_from_server.return_value = None
    fresh_conn.login.return_value = mock_imap_response("OK", [])
    fresh_conn.noop.return_value = mock_imap_response("OK", [])
    fresh_conn.logout.return_value = None

    local_settings = ICloudMailSettings(
        icloud_email="test@icloud.com",
        icloud_app_password="xxxx-xxxx-xxxx-xxxx",
        imap_pool_size=1,
        imap_timeout=10,
    )

    # First IMAP4_SSL() call (initialize) → stale_conn; second (reconnect) → fresh_conn
    with patch("icloud_mail_mcp.imap_client.IMAP4_SSL", side_effect=[stale_conn, fresh_conn]):
        with patch("icloud_mail_mcp.imap_client.RETRY_DELAYS", (0.0, 0.0, 0.0)):
            pool = IMAPConnectionPool(local_settings)
            await pool.initialize()

            yielded_conn: Any = None
            async with pool.acquire() as conn:
                yielded_conn = conn

            stale_conn.logout.assert_called_once()
            assert yielded_conn is fresh_conn
            await pool.close()


async def test_pool_close(settings: ICloudMailSettings, mock_imap_conn: AsyncMock) -> None:
    """close() logs out every connection and empties the queue."""
    with patch("icloud_mail_mcp.imap_client.IMAP4_SSL", return_value=mock_imap_conn):
        pool = IMAPConnectionPool(settings)
        await pool.initialize()
        await pool.close()
        assert pool._queue.qsize() == 0
        assert mock_imap_conn.logout.call_count == settings.imap_pool_size


# ─────────────────────────────────────────────────────────────────────────────
# Retry tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_retry_success_on_second(settings: ICloudMailSettings) -> None:
    """Operation that fails once then succeeds returns value; sleep called once."""
    with patch("icloud_mail_mcp.imap_client.RETRY_DELAYS", (0.0, 0.0, 0.0)):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            pool = IMAPConnectionPool(settings)
            call_count = 0

            async def flaky() -> str:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("first attempt failed")
                return "success"

            result = await pool._retry_operation(flaky)
            assert result == "success"
            assert mock_sleep.call_count == 1


async def test_retry_exhaustion(settings: ICloudMailSettings) -> None:
    """Function that always fails raises IMAPConnectionError after all retries."""
    with patch("icloud_mail_mcp.imap_client.RETRY_DELAYS", (0.0, 0.0, 0.0)):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            pool = IMAPConnectionPool(settings)

            async def always_fails() -> None:
                raise RuntimeError("always fails")

            with pytest.raises(IMAPConnectionError):
                await pool._retry_operation(always_fails)


async def test_auth_failure_no_retry(settings: ICloudMailSettings) -> None:
    """IMAPAuthenticationError propagates immediately without any retry sleep."""
    with patch("icloud_mail_mcp.imap_client.RETRY_DELAYS", (0.0, 0.0, 0.0)):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            pool = IMAPConnectionPool(settings)

            async def auth_fails() -> None:
                raise IMAPAuthenticationError("bad credentials")

            with pytest.raises(IMAPAuthenticationError):
                await pool._retry_operation(auth_fails)

            mock_sleep.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# IMAPClient operation tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_list_folders(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """list_folders() parses IMAP LIST response lines into Folder models."""
    client, conn = imap_client_and_conn
    conn.list.return_value = mock_imap_response(
        "OK",
        [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "Sent Messages"',
            b"Done",  # non-matching line — must be silently ignored
        ],
    )
    folders = await client.list_folders()
    assert len(folders) == 2
    assert folders[0].name == "INBOX"
    assert folders[1].name == "Sent Messages"
    assert folders[0].delimiter == "/"


async def test_list_emails(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
    sample_email_bytes: bytes,
) -> None:
    """list_emails() paginates by reversing UIDs and fetches headers only."""
    client, conn = imap_client_and_conn
    conn.uid_search.return_value = mock_imap_response("OK", [b"100 101 102"])
    conn.uid.return_value = mock_imap_response(
        "OK",
        [
            b"1 FETCH (UID 102 FLAGS (\\Seen))",
            bytearray(sample_email_bytes),
            b"2 FETCH (UID 101 FLAGS ())",
            bytearray(sample_email_bytes),
            b"3 FETCH (UID 100 FLAGS ())",
            bytearray(sample_email_bytes),
        ],
    )
    emails = await client.list_emails("INBOX", limit=3, offset=0)
    assert len(emails) == 3
    conn.select.assert_called()
    conn.uid.assert_called_once()


async def test_list_emails_empty(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """list_emails() returns [] for an empty folder without calling FETCH."""
    client, conn = imap_client_and_conn
    conn.uid_search.return_value = mock_imap_response("OK", [b""])
    emails = await client.list_emails("INBOX")
    assert emails == []
    conn.uid.assert_not_called()


async def test_get_email(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
    sample_email_bytes: bytes,
) -> None:
    """get_email() fetches a complete email and parses body and is_read flag."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response(
        "OK",
        [
            b"1 FETCH (UID 42 FLAGS (\\Seen))",
            bytearray(sample_email_bytes),
        ],
    )
    email_obj = await client.get_email("INBOX", "42")
    assert email_obj.uid == "42"
    assert email_obj.subject == "Test Email"
    assert email_obj.sender == "sender@example.com"
    assert email_obj.is_read is True
    assert "Hello" in email_obj.body_text


async def test_get_email_not_found(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """get_email() raises IMAPConnectionError when the requested UID is absent."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])
    with pytest.raises(IMAPConnectionError):
        await client.get_email("INBOX", "999")


async def test_search_emails(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
    sample_email_bytes: bytes,
) -> None:
    """search_emails() translates SearchQuery fields into correct IMAP SEARCH tokens."""
    client, conn = imap_client_and_conn
    conn.uid_search.return_value = mock_imap_response("OK", [b"100"])
    conn.uid.return_value = mock_imap_response(
        "OK",
        [b"1 FETCH (UID 100 FLAGS ())", bytearray(sample_email_bytes)],
    )
    query = SearchQuery(
        folder="INBOX",
        sender="alice@example.com",
        subject="Hello",
        since=date(2024, 1, 1),
        before=date(2024, 12, 31),
        body="test",
        limit=10,
    )
    emails = await client.search_emails(query)
    assert len(emails) == 1

    args = conn.uid_search.call_args.args
    assert "FROM" in args
    assert '"alice@example.com"' in args
    assert "SUBJECT" in args
    assert '"Hello"' in args
    assert "SINCE" in args
    assert "01-Jan-2024" in args
    assert "BEFORE" in args
    assert "31-Dec-2024" in args
    assert "BODY" in args
    assert '"test"' in args


async def test_search_no_results(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """search_emails() returns [] when the server returns no matching UIDs."""
    client, conn = imap_client_and_conn
    conn.uid_search.return_value = mock_imap_response("OK", [b""])
    emails = await client.search_emails(SearchQuery(folder="INBOX"))
    assert emails == []


async def test_move_email(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """move_email() performs COPY + STORE \\Deleted + EXPUNGE in sequence."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])
    conn.expunge.return_value = mock_imap_response("OK", [])

    result = await client.move_email("INBOX", "42", "Archive")

    assert result == {"status": "moved", "uid": "42", "destination": "Archive"}
    assert conn.uid.call_count == 2  # COPY then STORE
    conn.expunge.assert_called_once()


async def test_delete_email(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """delete_email() delegates to move_email targeting 'Deleted Messages'."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])
    conn.expunge.return_value = mock_imap_response("OK", [])

    result = await client.delete_email("INBOX", "42")

    assert result["status"] == "moved"
    assert result["destination"] == "Deleted Messages"


async def test_create_folder(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """create_folder() calls conn.create and returns a Folder with the given name."""
    client, conn = imap_client_and_conn
    conn.create.return_value = mock_imap_response("OK", [])

    folder = await client.create_folder("MyFolder")

    assert folder.name == "MyFolder"
    conn.create.assert_called_once_with("MyFolder")
