"""Tests for imap_client.py — IMAP connection pool and email operations."""

import base64
from collections.abc import AsyncGenerator, Callable
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from icloud_mcp.config import ICloudMailSettings
from icloud_mcp.exceptions import IMAPAuthenticationError, IMAPConnectionError
from icloud_mcp.imap_client import (
    IMAPClient,
    IMAPConnectionPool,
    _build_search_criteria,
    _decode_header,
    _validate_uid,
)
from icloud_mcp.models import EmailListResult, FolderStats, SearchQuery

# ─────────────────────────────────────────────────────────────────────────────
# Local fixture
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def imap_client_and_conn(
    settings: ICloudMailSettings,
    mock_imap_conn: AsyncMock,
) -> AsyncGenerator[tuple[IMAPClient, AsyncMock], None]:
    """Initialized IMAPClient backed by a fully mocked connection pool."""
    with patch("icloud_mcp.imap_client.IMAP4_SSL", return_value=mock_imap_conn):
        with patch("icloud_mcp.imap_client.RETRY_DELAYS", (0.0, 0.0, 0.0)):
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
    with patch("icloud_mcp.imap_client.IMAP4_SSL", return_value=mock_imap_conn):
        with patch("icloud_mcp.imap_client.RETRY_DELAYS", (0.0, 0.0, 0.0)):
            pool = IMAPConnectionPool(settings)
            await pool.initialize()
            assert mock_imap_conn.login.call_count == settings.imap_pool_size
            assert pool._queue.qsize() == settings.imap_pool_size
            await pool.close()


async def test_pool_acquire(settings: ICloudMailSettings, mock_imap_conn: AsyncMock) -> None:
    """acquire() yields a connection, shrinks the queue, then restores it on exit."""
    with patch("icloud_mcp.imap_client.IMAP4_SSL", return_value=mock_imap_conn):
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
    with patch("icloud_mcp.imap_client.IMAP4_SSL", side_effect=[stale_conn, fresh_conn]):
        with patch("icloud_mcp.imap_client.RETRY_DELAYS", (0.0, 0.0, 0.0)):
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
    with patch("icloud_mcp.imap_client.IMAP4_SSL", return_value=mock_imap_conn):
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
    with patch("icloud_mcp.imap_client.RETRY_DELAYS", (0.0, 0.0, 0.0)):
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
    with patch("icloud_mcp.imap_client.RETRY_DELAYS", (0.0, 0.0, 0.0)):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            pool = IMAPConnectionPool(settings)

            async def always_fails() -> None:
                raise RuntimeError("always fails")

            with pytest.raises(IMAPConnectionError):
                await pool._retry_operation(always_fails)


async def test_auth_failure_no_retry(settings: ICloudMailSettings) -> None:
    """IMAPAuthenticationError propagates immediately without any retry sleep."""
    with patch("icloud_mcp.imap_client.RETRY_DELAYS", (0.0, 0.0, 0.0)):
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
    result = await client.list_emails("INBOX", limit=3, offset=0)
    assert isinstance(result, EmailListResult)
    assert result.total_count == 3
    assert len(result.emails) == 3
    conn.select.assert_called()
    conn.uid.assert_called_once()


async def test_list_emails_empty(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """list_emails() returns [] for an empty folder without calling FETCH."""
    client, conn = imap_client_and_conn
    conn.uid_search.return_value = mock_imap_response("OK", [b""])
    result = await client.list_emails("INBOX")
    assert result.emails == []
    assert result.total_count == 0
    conn.uid.assert_not_called()


async def test_list_emails_sort_asc(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
    sample_email_bytes: bytes,
) -> None:
    """list_emails() with sort_order='asc' sends UIDs in ascending order (oldest first)."""
    client, conn = imap_client_and_conn
    conn.uid_search.return_value = mock_imap_response("OK", [b"100 101 102"])
    conn.uid.return_value = mock_imap_response(
        "OK",
        [
            b"1 FETCH (UID 100 FLAGS ())",
            bytearray(sample_email_bytes),
            b"2 FETCH (UID 101 FLAGS ())",
            bytearray(sample_email_bytes),
            b"3 FETCH (UID 102 FLAGS ())",
            bytearray(sample_email_bytes),
        ],
    )
    result = await client.list_emails("INBOX", limit=3, offset=0, sort_order="asc")
    assert len(result.emails) == 3
    uid_set_arg = conn.uid.call_args[0][1]
    assert uid_set_arg == "100,101,102"


async def test_list_emails_sort_desc(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
    sample_email_bytes: bytes,
) -> None:
    """list_emails() with sort_order='desc' sends UIDs in descending order (newest first)."""
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
    result = await client.list_emails("INBOX", limit=3, offset=0, sort_order="desc")
    assert len(result.emails) == 3
    uid_set_arg = conn.uid.call_args[0][1]
    assert uid_set_arg == "102,101,100"


async def test_list_emails_total_count_with_offset(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
    sample_email_bytes: bytes,
) -> None:
    """list_emails() total_count reflects all UIDs regardless of offset/limit."""
    client, conn = imap_client_and_conn
    conn.uid_search.return_value = mock_imap_response("OK", [b"100 101 102 103 104"])
    conn.uid.return_value = mock_imap_response(
        "OK",
        [
            b"1 FETCH (UID 102 FLAGS ())",
            bytearray(sample_email_bytes),
            b"2 FETCH (UID 101 FLAGS ())",
            bytearray(sample_email_bytes),
        ],
    )
    result = await client.list_emails("INBOX", limit=2, offset=2)
    assert result.total_count == 5
    assert len(result.emails) == 2


async def test_list_emails_total_count_exceeds_limit(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
    sample_email_bytes: bytes,
) -> None:
    """list_emails() total_count reflects full folder size even when limit < total."""
    client, conn = imap_client_and_conn
    uids = " ".join(str(i) for i in range(100, 110))  # 10 UIDs
    conn.uid_search.return_value = mock_imap_response("OK", [uids.encode()])
    conn.uid.return_value = mock_imap_response(
        "OK",
        [
            b"1 FETCH (UID 109 FLAGS ())",
            bytearray(sample_email_bytes),
            b"2 FETCH (UID 108 FLAGS ())",
            bytearray(sample_email_bytes),
            b"3 FETCH (UID 107 FLAGS ())",
            bytearray(sample_email_bytes),
        ],
    )
    result = await client.list_emails("INBOX", limit=3, offset=0)
    assert result.total_count == 10
    assert len(result.emails) == 3


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


async def test_search_emails_is_read(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """is_read=True → SEEN; is_read=False → UNSEEN in IMAP SEARCH args."""
    client, conn = imap_client_and_conn
    conn.uid_search.return_value = mock_imap_response("OK", [b""])

    await client.search_emails(SearchQuery(folder="INBOX", is_read=True))
    assert "SEEN" in conn.uid_search.call_args.args
    assert "UNSEEN" not in conn.uid_search.call_args.args

    conn.uid_search.reset_mock()
    conn.uid_search.return_value = mock_imap_response("OK", [b""])

    await client.search_emails(SearchQuery(folder="INBOX", is_read=False))
    assert "UNSEEN" in conn.uid_search.call_args.args
    assert "SEEN" not in conn.uid_search.call_args.args


async def test_search_emails_is_flagged(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """is_flagged=True → FLAGGED; is_flagged=False → UNFLAGGED in IMAP SEARCH args."""
    client, conn = imap_client_and_conn
    conn.uid_search.return_value = mock_imap_response("OK", [b""])

    await client.search_emails(SearchQuery(folder="INBOX", is_flagged=True))
    assert "FLAGGED" in conn.uid_search.call_args.args
    assert "UNFLAGGED" not in conn.uid_search.call_args.args

    conn.uid_search.reset_mock()
    conn.uid_search.return_value = mock_imap_response("OK", [b""])

    await client.search_emails(SearchQuery(folder="INBOX", is_flagged=False))
    assert "UNFLAGGED" in conn.uid_search.call_args.args
    assert "FLAGGED" not in conn.uid_search.call_args.args


async def test_search_emails_min_size(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """min_size=1024 → LARGER and '1024' appear in IMAP SEARCH args."""
    client, conn = imap_client_and_conn
    conn.uid_search.return_value = mock_imap_response("OK", [b""])

    await client.search_emails(SearchQuery(folder="INBOX", min_size=1024))

    args = conn.uid_search.call_args.args
    assert "LARGER" in args
    assert "1024" in args


async def test_search_emails_has_attachments(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """has_attachments=True → HEADER/Content-Type/multipart/mixed; False → NOT prefix."""
    client, conn = imap_client_and_conn
    conn.uid_search.return_value = mock_imap_response("OK", [b""])

    await client.search_emails(SearchQuery(folder="INBOX", has_attachments=True))
    args = conn.uid_search.call_args.args
    assert "HEADER" in args
    assert "Content-Type" in args
    assert '"multipart/mixed"' in args
    assert "NOT" not in args

    conn.uid_search.reset_mock()
    conn.uid_search.return_value = mock_imap_response("OK", [b""])

    await client.search_emails(SearchQuery(folder="INBOX", has_attachments=False))
    args = conn.uid_search.call_args.args
    assert "NOT" in args
    assert "HEADER" in args
    assert "Content-Type" in args
    assert '"multipart/mixed"' in args


async def test_search_emails_combined_new_filters(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """is_read=False + is_flagged=True + min_size=500 all appear simultaneously."""
    client, conn = imap_client_and_conn
    conn.uid_search.return_value = mock_imap_response("OK", [b""])

    await client.search_emails(
        SearchQuery(folder="INBOX", is_read=False, is_flagged=True, min_size=500)
    )

    args = conn.uid_search.call_args.args
    assert "UNSEEN" in args
    assert "FLAGGED" in args
    assert "LARGER" in args
    assert "500" in args


async def test_search_emails_new_filters_with_existing(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """sender + is_read=True → both FROM and SEEN appear in IMAP SEARCH args."""
    client, conn = imap_client_and_conn
    conn.uid_search.return_value = mock_imap_response("OK", [b""])

    await client.search_emails(
        SearchQuery(folder="INBOX", sender="alice@example.com", is_read=True)
    )

    args = conn.uid_search.call_args.args
    assert "FROM" in args
    assert '"alice@example.com"' in args
    assert "SEEN" in args


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
    conn.create.assert_called_once_with('"MyFolder"')


async def test_rename_folder(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """rename_folder() calls conn.rename and returns a Folder with the new name."""
    client, conn = imap_client_and_conn
    conn.rename.return_value = mock_imap_response("OK", [])

    folder = await client.rename_folder("OldName", "NewName")

    assert folder.name == "NewName"
    conn.rename.assert_called_once_with('"OldName"', '"NewName"')


async def test_rename_folder_not_found(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """rename_folder() raises IMAPConnectionError when RENAME returns NO."""
    client, conn = imap_client_and_conn
    conn.rename.return_value = mock_imap_response("NO", [])

    with pytest.raises(IMAPConnectionError):
        await client.rename_folder("NonExistent", "NewName")


async def test_delete_folder(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """delete_folder() succeeds when folder is empty."""
    client, conn = imap_client_and_conn
    conn.status.return_value = mock_imap_response("OK", [b'"MyFolder" (MESSAGES 0)'])
    conn.delete.return_value = mock_imap_response("OK", [])

    result = await client.delete_folder("MyFolder")

    assert result == {"status": "deleted", "name": "MyFolder"}
    conn.status.assert_called_once_with('"MyFolder"', "(MESSAGES)")
    conn.delete.assert_called_once_with('"MyFolder"')


async def test_delete_folder_not_found(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """delete_folder() raises IMAPConnectionError when folder does not exist."""
    client, conn = imap_client_and_conn
    conn.status.return_value = mock_imap_response("NO", [])

    with pytest.raises(IMAPConnectionError):
        await client.delete_folder("NonExistent")

    conn.delete.assert_not_called()


async def test_delete_folder_not_empty(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """delete_folder() raises IMAPConnectionError when folder contains messages."""
    client, conn = imap_client_and_conn
    conn.status.return_value = mock_imap_response("OK", [b'"MyFolder" (MESSAGES 5)'])

    with pytest.raises(IMAPConnectionError):
        await client.delete_folder("MyFolder")

    conn.delete.assert_not_called()


async def test_get_folder_stats(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """get_folder_stats() returns FolderStats with correct counts from STATUS response."""
    client, conn = imap_client_and_conn
    conn.status.return_value = mock_imap_response("OK", [b'"INBOX" (MESSAGES 42 UNSEEN 3)'])

    result = await client.get_folder_stats("INBOX")

    assert isinstance(result, FolderStats)
    assert result.folder == "INBOX"
    assert result.total_count == 42
    assert result.unread_count == 3
    conn.status.assert_called_once_with("INBOX", "(MESSAGES UNSEEN)")


async def test_get_folder_stats_empty(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """get_folder_stats() returns zero counts for an empty folder."""
    client, conn = imap_client_and_conn
    conn.status.return_value = mock_imap_response("OK", [b'"INBOX" (MESSAGES 0 UNSEEN 0)'])

    result = await client.get_folder_stats("INBOX")

    assert result.total_count == 0
    assert result.unread_count == 0


async def test_get_folder_stats_not_found(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """get_folder_stats() raises IMAPConnectionError when STATUS returns NO."""
    client, conn = imap_client_and_conn
    conn.status.return_value = mock_imap_response("NO", [])

    with pytest.raises(IMAPConnectionError):
        await client.get_folder_stats("NonExistent")


async def test_mark_as_read(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """mark_as_read() sends STORE +FLAGS.SILENT (\\Seen) and returns status dict."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])

    result = await client.mark_as_read("INBOX", "42")

    assert result == {"status": "marked_as_read", "uid": "42"}
    conn.uid.assert_called_once_with("STORE", "42", "+FLAGS.SILENT", "(\\Seen)")


async def test_mark_as_read_fails(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """mark_as_read() raises IMAPConnectionError when STORE returns NO."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("NO", [])

    with pytest.raises(IMAPConnectionError):
        await client.mark_as_read("INBOX", "42")


async def test_mark_as_unread(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """mark_as_unread() sends STORE -FLAGS.SILENT (\\Seen) and returns status dict."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])

    result = await client.mark_as_unread("INBOX", "42")

    assert result == {"status": "marked_as_unread", "uid": "42"}
    conn.uid.assert_called_once_with("STORE", "42", "-FLAGS.SILENT", "(\\Seen)")


async def test_mark_as_unread_fails(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """mark_as_unread() raises IMAPConnectionError when STORE returns NO."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("NO", [])

    with pytest.raises(IMAPConnectionError):
        await client.mark_as_unread("INBOX", "42")


async def test_flag_email(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """flag_email() sends STORE +FLAGS.SILENT (\\Flagged) and returns status dict."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])

    result = await client.flag_email("INBOX", "42")

    assert result == {"status": "flagged", "uid": "42"}
    conn.uid.assert_called_once_with("STORE", "42", "+FLAGS.SILENT", "(\\Flagged)")


async def test_flag_email_fails(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """flag_email() raises IMAPConnectionError when STORE returns NO."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("NO", [])

    with pytest.raises(IMAPConnectionError):
        await client.flag_email("INBOX", "42")


async def test_unflag_email(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """unflag_email() sends STORE -FLAGS.SILENT (\\Flagged) and returns status dict."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])

    result = await client.unflag_email("INBOX", "42")

    assert result == {"status": "unflagged", "uid": "42"}
    conn.uid.assert_called_once_with("STORE", "42", "-FLAGS.SILENT", "(\\Flagged)")


async def test_unflag_email_fails(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """unflag_email() raises IMAPConnectionError when STORE returns NO."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("NO", [])

    with pytest.raises(IMAPConnectionError):
        await client.unflag_email("INBOX", "42")


# ─────────────────────────────────────────────────────────────────────────────
# bulk_action tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_bulk_mark_as_read(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """bulk_action(mark_as_read) sends STORE +FLAGS.SILENT (\\Seen) on UID set."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])

    result = await client.bulk_action("INBOX", ["42", "43"], "mark_as_read")

    assert result == {"status": "bulk_mark_as_read", "uids": ["42", "43"]}
    conn.uid.assert_called_once_with("STORE", "42,43", "+FLAGS.SILENT", "(\\Seen)")


async def test_bulk_mark_as_unread(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """bulk_action(mark_as_unread) sends STORE -FLAGS.SILENT (\\Seen) on UID set."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])

    result = await client.bulk_action("INBOX", ["42", "43"], "mark_as_unread")

    assert result == {"status": "bulk_mark_as_unread", "uids": ["42", "43"]}
    conn.uid.assert_called_once_with("STORE", "42,43", "-FLAGS.SILENT", "(\\Seen)")


async def test_bulk_flag(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """bulk_action(flag) sends STORE +FLAGS.SILENT (\\Flagged) on UID set."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])

    result = await client.bulk_action("INBOX", ["42", "43"], "flag")

    assert result == {"status": "bulk_flag", "uids": ["42", "43"]}
    conn.uid.assert_called_once_with("STORE", "42,43", "+FLAGS.SILENT", "(\\Flagged)")


async def test_bulk_unflag(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """bulk_action(unflag) sends STORE -FLAGS.SILENT (\\Flagged) on UID set."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])

    result = await client.bulk_action("INBOX", ["42", "43"], "unflag")

    assert result == {"status": "bulk_unflag", "uids": ["42", "43"]}
    conn.uid.assert_called_once_with("STORE", "42,43", "-FLAGS.SILENT", "(\\Flagged)")


async def test_bulk_move(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """bulk_action(move) performs COPY + STORE \\Deleted + EXPUNGE on UID set."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])
    conn.expunge.return_value = mock_imap_response("OK", [])

    result = await client.bulk_action("INBOX", ["42", "43"], "move", destination="Archive")

    assert result == {"status": "bulk_moved", "uids": ["42", "43"], "destination": "Archive"}
    assert conn.uid.call_count == 2
    conn.expunge.assert_called_once()


async def test_bulk_delete(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """bulk_action(delete) moves UID set to 'Deleted Messages'."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])
    conn.expunge.return_value = mock_imap_response("OK", [])

    result = await client.bulk_action("INBOX", ["42", "43"], "delete")

    assert result == {
        "status": "bulk_deleted",
        "uids": ["42", "43"],
        "destination": "Deleted Messages",
    }
    assert conn.uid.call_count == 2
    conn.expunge.assert_called_once()


async def test_bulk_action_empty_uids(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
) -> None:
    """bulk_action with empty uids returns no_action without calling conn."""
    client, conn = imap_client_and_conn

    result = await client.bulk_action("INBOX", [], "mark_as_read")

    assert result == {"status": "no_action", "uids": []}
    conn.uid.assert_not_called()
    conn.select.assert_not_called()


async def test_bulk_action_invalid_action(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
) -> None:
    """bulk_action raises ValueError for unknown action names."""
    client, _ = imap_client_and_conn

    with pytest.raises(ValueError, match="Ação inválida"):
        await client.bulk_action("INBOX", ["42"], "archive")


async def test_bulk_action_move_without_destination(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """bulk_action(move) raises ValueError when destination is None."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])

    with pytest.raises(ValueError, match="destination"):
        await client.bulk_action("INBOX", ["42"], "move", destination=None)


async def test_bulk_action_store_fails(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """bulk_action raises IMAPConnectionError when STORE returns NO."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("NO", [])

    with pytest.raises(IMAPConnectionError):
        await client.bulk_action("INBOX", ["42", "43"], "mark_as_read")


# ─────────────────────────────────────────────────────────────────────────────
# download_attachment tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_download_attachment(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
    multipart_email_bytes: bytes,
) -> None:
    """download_attachment() returns base64 data for a matching filename."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response(
        "OK",
        [
            b"1 FETCH (UID 42 FLAGS ())",
            bytearray(multipart_email_bytes),
        ],
    )

    result = await client.download_attachment("INBOX", "42", "report.pdf")

    assert result["filename"] == "report.pdf"
    assert result["content_type"] == "application/pdf"
    assert base64.b64decode(result["data"]) == b"fake PDF content for testing"
    conn.uid.assert_called_once_with("FETCH", "42", "(FLAGS BODY.PEEK[])")


async def test_download_attachment_not_found(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
    multipart_email_bytes: bytes,
) -> None:
    """download_attachment() raises IMAPConnectionError when filename is absent."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response(
        "OK",
        [
            b"1 FETCH (UID 42 FLAGS ())",
            bytearray(multipart_email_bytes),
        ],
    )

    with pytest.raises(IMAPConnectionError, match="Anexo"):
        await client.download_attachment("INBOX", "42", "nonexistent.zip")


# ─────────────────────────────────────────────────────────────────────────────
# list_attachments tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_list_attachments(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """list_attachments() returns one Attachment for a multipart/mixed email with a PDF."""
    client, conn = imap_client_and_conn
    bodystructure_line = (
        b"1 FETCH (UID 42 BODYSTRUCTURE "
        b'(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 28 1 NIL NIL NIL NIL)'
        b'("application" "pdf" ("name" "report.pdf") NIL NIL "base64" 38000 NIL '
        b'("attachment" ("filename" "report.pdf")) NIL NIL) '
        b'"mixed" ("boundary" "===abc===") NIL NIL NIL))'
    )
    conn.uid.return_value = mock_imap_response("OK", [bodystructure_line])

    attachments = await client.list_attachments("INBOX", "42")

    assert len(attachments) == 1
    assert attachments[0].filename == "report.pdf"
    assert attachments[0].content_type == "application/pdf"
    assert attachments[0].size == 38000
    conn.uid.assert_called_once_with("FETCH", "42", "(BODYSTRUCTURE)")


async def test_list_attachments_no_attachments(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """list_attachments() returns [] for a single-part plain text email."""
    client, conn = imap_client_and_conn
    bodystructure_line = (
        b"1 FETCH (UID 42 BODYSTRUCTURE "
        b'("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 100 5 NIL NIL NIL NIL))'
    )
    conn.uid.return_value = mock_imap_response("OK", [bodystructure_line])

    attachments = await client.list_attachments("INBOX", "42")

    assert attachments == []


async def test_list_attachments_multiple(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """list_attachments() returns 2 Attachments for a multipart email with PDF + image."""
    client, conn = imap_client_and_conn
    bodystructure_line = (
        b"1 FETCH (UID 42 BODYSTRUCTURE "
        b'(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 28 1 NIL NIL NIL NIL)'
        b'("application" "pdf" ("name" "doc.pdf") NIL NIL "base64" 50000 NIL '
        b'("attachment" ("filename" "doc.pdf")) NIL NIL)'
        b'("image" "jpeg" ("name" "photo.jpg") NIL NIL "base64" 20000 NIL '
        b'("attachment" ("filename" "photo.jpg")) NIL NIL)'
        b'"mixed" ("boundary" "===abc===") NIL NIL NIL))'
    )
    conn.uid.return_value = mock_imap_response("OK", [bodystructure_line])

    attachments = await client.list_attachments("INBOX", "42")

    assert len(attachments) == 2
    filenames = {a.filename for a in attachments}
    assert filenames == {"doc.pdf", "photo.jpg"}


async def test_list_attachments_email_not_found(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """list_attachments() raises IMAPConnectionError when BODYSTRUCTURE is absent."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [b"OK completed"])

    with pytest.raises(IMAPConnectionError):
        await client.list_attachments("INBOX", "42")


async def test_list_attachments_filename_from_body_params(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """list_attachments() extracts filename from body params when disposition has no filename."""
    client, conn = imap_client_and_conn
    # Filename only in body params ("name"), disposition is NIL
    bodystructure_line = (
        b"1 FETCH (UID 42 BODYSTRUCTURE "
        b'("application" "zip" ("name" "archive.zip") NIL NIL "base64" 5000 NIL NIL NIL NIL))'
    )
    conn.uid.return_value = mock_imap_response("OK", [bodystructure_line])

    attachments = await client.list_attachments("INBOX", "42")

    assert len(attachments) == 1
    assert attachments[0].filename == "archive.zip"
    assert attachments[0].content_type == "application/zip"
    assert attachments[0].size == 5000


async def test_list_attachments_inline_skipped(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """list_attachments() skips inline parts that have no filename."""
    client, conn = imap_client_and_conn
    # image/png with inline disposition and NIL params (no filename)
    bodystructure_line = (
        b"1 FETCH (UID 42 BODYSTRUCTURE "
        b'(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 100 5 NIL NIL NIL NIL)'
        b'("image" "png" NIL NIL NIL "base64" 5000 NIL ("inline" NIL) NIL NIL)'
        b'"mixed" NIL NIL NIL NIL))'
    )
    conn.uid.return_value = mock_imap_response("OK", [bodystructure_line])

    attachments = await client.list_attachments("INBOX", "42")

    assert attachments == []


async def test_download_attachment_email_not_found(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """download_attachment() raises IMAPConnectionError when UID is absent."""
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response("OK", [])

    with pytest.raises(IMAPConnectionError, match="não encontrado"):
        await client.download_attachment("INBOX", "99", "report.pdf")


# ─────────────────────────────────────────────────────────────────────────────
# save_draft
# ─────────────────────────────────────────────────────────────────────────────


async def test_save_draft(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """save_draft() appends a draft and returns status/folder/uid."""
    client, conn = imap_client_and_conn
    conn.append.return_value = mock_imap_response("OK", [b"[APPENDUID 1 456]"])

    result = await client.save_draft(to=["to@example.com"], subject="Test", body="Body")

    assert result == {"status": "saved", "folder": "Drafts", "uid": "456"}
    conn.append.assert_called_once()
    call_kwargs = conn.append.call_args
    message_bytes: bytes = call_kwargs.args[0]
    assert b"From:" in message_bytes
    assert b"To: to@example.com" in message_bytes
    assert b"Subject: Test" in message_bytes
    assert call_kwargs.kwargs.get("flags") == "(\\Draft)"
    assert call_kwargs.kwargs.get("mailbox") == "Drafts"


async def test_save_draft_with_cc(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """save_draft() includes Cc header when cc list is provided."""
    client, conn = imap_client_and_conn
    conn.append.return_value = mock_imap_response("OK", [b"[APPENDUID 1 789]"])

    result = await client.save_draft(
        to=["to@example.com"], subject="CC Test", body="Body", cc=["cc@example.com"]
    )

    assert result["uid"] == "789"
    message_bytes: bytes = conn.append.call_args.args[0]
    assert b"Cc: cc@example.com" in message_bytes


async def test_save_draft_failure(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """save_draft() raises IMAPConnectionError when APPEND returns NO."""
    client, conn = imap_client_and_conn
    conn.append.return_value = mock_imap_response("NO", [b"[CANNOT] Mailbox not found"])

    with pytest.raises(IMAPConnectionError, match="rascunho"):
        await client.save_draft(to=["to@example.com"], subject="Test", body="Body")


# ─────────────────────────────────────────────────────────────────────────────
# Threading header extraction
# ─────────────────────────────────────────────────────────────────────────────


async def test_parse_email_threading_headers(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """get_email() populates message_id, in_reply_to, references, and reply_to from raw headers."""
    threading_email_bytes = (
        b"From: alice@example.com\r\n"
        b"To: bob@example.com\r\n"
        b"Subject: Re: Hello\r\n"
        b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        b"Message-ID: <reply@mail.example.com>\r\n"
        b"In-Reply-To: <original@mail.example.com>\r\n"
        b"References: <root@mail.example.com> <original@mail.example.com>\r\n"
        b"Reply-To: alice-replies@example.com\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Yes, that works!\r\n"
    )
    client, conn = imap_client_and_conn
    conn.uid.return_value = mock_imap_response(
        "OK",
        [
            b"1 FETCH (UID 55 FLAGS ())",
            bytearray(threading_email_bytes),
        ],
    )

    email_obj = await client.get_email("INBOX", "55")

    assert email_obj.message_id == "<reply@mail.example.com>"
    assert email_obj.in_reply_to == "<original@mail.example.com>"
    assert email_obj.references == "<root@mail.example.com> <original@mail.example.com>"
    assert email_obj.reply_to == "alice-replies@example.com"


# ─────────────────────────────────────────────────────────────────────────────
# Batch 1: IMAP injection sanitization + UID validation
# ─────────────────────────────────────────────────────────────────────────────


def test_validate_uid_rejects_non_numeric() -> None:
    """_validate_uid raises ValueError for non-numeric UIDs."""
    with pytest.raises(ValueError, match="UID inválido"):
        _validate_uid("1:* +FLAGS \\Deleted")


def test_validate_uid_accepts_numeric() -> None:
    """_validate_uid does not raise for purely numeric UIDs."""
    _validate_uid("12345")


async def test_get_email_rejects_non_numeric_uid(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
) -> None:
    """get_email raises ValueError for a non-numeric UID."""
    client, _ = imap_client_and_conn
    with pytest.raises(ValueError, match="UID inválido"):
        await client.get_email("INBOX", "abc")


async def test_move_email_rejects_non_numeric_uid(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
) -> None:
    """move_email raises ValueError for a non-numeric UID."""
    client, _ = imap_client_and_conn
    with pytest.raises(ValueError, match="UID inválido"):
        await client.move_email("INBOX", "1:*", "Archive")


async def test_mark_as_read_rejects_non_numeric_uid(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
) -> None:
    """mark_as_read raises ValueError for a non-numeric UID."""
    client, _ = imap_client_and_conn
    with pytest.raises(ValueError, match="UID inválido"):
        await client.mark_as_read("INBOX", "bad")


def test_build_search_criteria_strips_double_quotes() -> None:
    """Double quotes in search input are stripped to prevent IMAP injection."""
    query = SearchQuery(subject='foo" DELETED')
    criteria = _build_search_criteria(query)
    # The embedded " is stripped; the result is a single quoted token
    assert criteria == ["SUBJECT", '"foo DELETED"']


def test_build_search_criteria_strips_backslash() -> None:
    """Backslashes in search input are stripped to prevent IMAP injection."""
    query = SearchQuery(sender="alice\\@example.com")
    criteria = _build_search_criteria(query)
    assert "\\" not in criteria[1]


def test_build_search_criteria_normal_input_unchanged() -> None:
    """Normal input without special characters passes through unchanged."""
    query = SearchQuery(sender="alice@example.com", subject="Hello World")
    criteria = _build_search_criteria(query)
    assert criteria == ["FROM", '"alice@example.com"', "SUBJECT", '"Hello World"']


# ─────────────────────────────────────────────────────────────────────────────
# Batch 2: Folder quoting + pool resilience + error wrapping
# ─────────────────────────────────────────────────────────────────────────────


async def test_create_folder_uses_quoted_name(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
) -> None:
    """create_folder passes the folder name through aioimaplib.quoted()."""
    client, conn = imap_client_and_conn
    await client.create_folder("My Folder")
    call_args = conn.create.call_args
    assert call_args is not None
    # aioimaplib.quoted wraps with double quotes
    assert '"My Folder"' == call_args.args[0]


async def test_delete_folder_uses_quoted_name(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """delete_folder passes the folder name through aioimaplib.quoted()."""
    client, conn = imap_client_and_conn
    conn.status.return_value = mock_imap_response("OK", [b'"My Folder" (MESSAGES 0)'])
    conn.delete.return_value = mock_imap_response("OK", [])
    await client.delete_folder("My Folder")
    # Both status and delete should use quoted name
    assert '"My Folder"' in str(conn.status.call_args)
    assert '"My Folder"' in str(conn.delete.call_args)


async def test_acquire_timeout_raises_imap_connection_error(
    settings: ICloudMailSettings,
) -> None:
    """acquire() raises IMAPConnectionError when pool queue times out."""
    settings.imap_timeout = 0  # immediate timeout
    pool = IMAPConnectionPool(settings)
    # Don't initialize — pool is empty so acquire will timeout
    with pytest.raises(IMAPConnectionError, match="Timeout"):
        async with pool.acquire():
            pass


async def test_create_connection_wraps_oserror(
    settings: ICloudMailSettings,
) -> None:
    """_create_connection wraps OSError into IMAPConnectionError."""
    with patch(
        "icloud_mcp.imap_client.IMAP4_SSL",
        side_effect=OSError("Connection refused"),
    ):
        pool = IMAPConnectionPool(settings)
        with pytest.raises(IMAPConnectionError, match="Erro de rede"):
            await pool._create_connection()


# ─────────────────────────────────────────────────────────────────────────────
# Batch 3: FETCH order preservation + threading headers
# ─────────────────────────────────────────────────────────────────────────────


async def test_list_emails_preserves_uid_order(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
    sample_email_bytes: bytes,
) -> None:
    """list_emails returns emails in the same order as page_uids."""
    client, conn = imap_client_and_conn
    conn.select.return_value = mock_imap_response("OK", [])
    # IMAP returns UIDs in ascending order
    conn.uid_search.return_value = mock_imap_response("OK", [b"1 2 3"])

    # Simulate server returning FETCH results in arbitrary order
    conn.uid.return_value = mock_imap_response(
        "OK",
        [
            b"2 FETCH (UID 2 FLAGS ())",
            bytearray(sample_email_bytes),
            b"3 FETCH (UID 3 FLAGS ())",
            bytearray(sample_email_bytes),
            b"1 FETCH (UID 1 FLAGS ())",
            bytearray(sample_email_bytes),
        ],
    )

    result = await client.list_emails("INBOX", limit=3, offset=0)

    # desc reverses UIDs to [3, 2, 1]; results must respect that order
    assert [e.uid for e in result.emails] == ["3", "2", "1"]


async def test_list_emails_fetches_threading_headers(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """list_emails FETCH includes MESSAGE-ID, IN-REPLY-TO, REFERENCES, REPLY-TO."""
    client, conn = imap_client_and_conn
    conn.select.return_value = mock_imap_response("OK", [])
    conn.uid_search.return_value = mock_imap_response("OK", [b"1"])

    email_bytes = (
        b"From: sender@example.com\r\n"
        b"Subject: Test\r\n"
        b"Message-ID: <msg1@example.com>\r\n"
        b"In-Reply-To: <parent@example.com>\r\n"
        b"References: <root@example.com>\r\n"
        b"Reply-To: reply@example.com\r\n"
        b"\r\n"
    )

    conn.uid.return_value = mock_imap_response(
        "OK",
        [b"1 FETCH (UID 1 FLAGS ())", bytearray(email_bytes)],
    )

    result = await client.list_emails("INBOX", limit=1)

    assert result.emails[0].message_id == "<msg1@example.com>"
    assert result.emails[0].in_reply_to == "<parent@example.com>"
    assert result.emails[0].references == "<root@example.com>"
    assert result.emails[0].reply_to == "reply@example.com"


# ─────────────────────────────────────────────────────────────────────────────
# Batch 6: Expanded test coverage (header decoding, multipart, edge cases)
# ─────────────────────────────────────────────────────────────────────────────


def test_decode_header_encoded_utf8() -> None:
    """_decode_header decodes Q-encoded UTF-8 headers."""
    encoded = "=?utf-8?Q?Relat=C3=B3rio_Mensal?="
    assert _decode_header(encoded) == "Relatório Mensal"


def test_decode_header_encoded_base64() -> None:
    """_decode_header decodes base64-encoded headers."""
    encoded = "=?utf-8?B?Sm/Do28gU2lsdmE=?="
    assert _decode_header(encoded) == "João Silva"


def test_decode_header_plain_string_unchanged() -> None:
    """_decode_header returns plain strings unchanged."""
    assert _decode_header("Hello World") == "Hello World"


def test_decode_header_none_returns_empty() -> None:
    """_decode_header returns empty string for None input."""
    assert _decode_header(None) == ""


async def test_get_email_with_encoded_headers(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
    encoded_header_email_bytes: bytes,
) -> None:
    """get_email correctly decodes RFC 2047 encoded headers."""
    client, conn = imap_client_and_conn
    conn.select.return_value = mock_imap_response("OK", [])
    conn.uid.return_value = mock_imap_response(
        "OK",
        [
            b"1 FETCH (UID 1 FLAGS ())",
            bytearray(encoded_header_email_bytes),
        ],
    )

    email_obj = await client.get_email("INBOX", "1")

    assert "João Silva" in email_obj.sender
    assert email_obj.subject == "Relatório Mensal"


async def test_get_email_multipart(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
    multipart_email_bytes: bytes,
) -> None:
    """get_email correctly parses a multipart/mixed email with an attachment."""
    client, conn = imap_client_and_conn
    conn.select.return_value = mock_imap_response("OK", [])
    conn.uid.return_value = mock_imap_response(
        "OK",
        [
            b"1 FETCH (UID 1 FLAGS ())",
            bytearray(multipart_email_bytes),
        ],
    )

    email_obj = await client.get_email("INBOX", "1")

    assert "attachment" in email_obj.body_text.lower() or len(email_obj.attachments) > 0
    assert any(a.filename == "report.pdf" for a in email_obj.attachments)


async def test_list_emails_empty_folder(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """list_emails on an empty folder returns empty list with total_count=0."""
    client, conn = imap_client_and_conn
    conn.select.return_value = mock_imap_response("OK", [])
    conn.uid_search.return_value = mock_imap_response("OK", [b""])

    result = await client.list_emails("INBOX")

    assert result.emails == []
    assert result.total_count == 0


async def test_list_emails_offset_beyond_total(
    imap_client_and_conn: tuple[IMAPClient, AsyncMock],
    mock_imap_response: Callable[[str, list[Any]], MagicMock],
) -> None:
    """list_emails with offset beyond total returns empty list."""
    client, conn = imap_client_and_conn
    conn.select.return_value = mock_imap_response("OK", [])
    conn.uid_search.return_value = mock_imap_response("OK", [b"1 2 3"])

    result = await client.list_emails("INBOX", limit=10, offset=100)

    assert result.emails == []
    assert result.total_count == 3
