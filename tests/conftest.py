"""Shared test fixtures for icloud_mcp tests."""

from collections.abc import Callable, Generator
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from icloud_mcp.config import ICloudMailSettings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Generator[None, None, None]:
    """Clear lru_cache before and after each test to prevent cross-test pollution."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> ICloudMailSettings:
    """ICloudMailSettings with fake credentials for testing."""
    return ICloudMailSettings(
        icloud_email="test@icloud.com",
        icloud_app_password="xxxx-xxxx-xxxx-xxxx",
        imap_pool_size=2,
        imap_timeout=10,
    )


@pytest.fixture
def mock_imap_response() -> Callable[[str, list[Any]], MagicMock]:
    """Factory that creates MagicMock objects simulating aioimaplib responses."""

    def _make(result: str, lines: list[Any]) -> MagicMock:
        mock = MagicMock()
        mock.result = result
        mock.lines = lines
        return mock

    return _make


@pytest.fixture
def mock_imap_conn(mock_imap_response: Callable[[str, list[Any]], MagicMock]) -> AsyncMock:
    """AsyncMock simulating an authenticated aioimaplib IMAP4_SSL connection."""
    conn = AsyncMock()
    ok = mock_imap_response("OK", [])
    conn.wait_hello_from_server.return_value = None
    conn.login.return_value = ok
    conn.noop.return_value = ok
    conn.logout.return_value = ok
    conn.list.return_value = ok
    conn.select.return_value = ok
    conn.uid_search.return_value = mock_imap_response("OK", [b""])
    conn.uid.return_value = ok
    conn.expunge.return_value = ok
    conn.create.return_value = ok
    conn.append.return_value = ok
    return conn


@pytest.fixture
def mock_smtp() -> AsyncMock:
    """AsyncMock simulating an aiosmtplib.SMTP connection."""
    smtp = AsyncMock()
    smtp.connect.return_value = None
    smtp.login.return_value = None
    smtp.send_message.return_value = None
    smtp.quit.return_value = None
    return smtp


@pytest.fixture(scope="module")
def sample_email_bytes() -> bytes:
    """Complete RFC 822 email bytes for testing email parsing."""
    return (
        b"From: sender@example.com\r\n"
        b"To: recipient@example.com\r\n"
        b"Subject: Test Email\r\n"
        b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Hello, this is the email body.\r\n"
    )


@pytest.fixture(scope="module")
def encoded_header_email_bytes() -> bytes:
    """RFC 822 email with Q-encoded Subject and base64-encoded From header."""
    return (
        b"From: =?utf-8?B?Sm/Do28gU2lsdmE=?= <joao@example.com>\r\n"
        b"To: recipient@example.com\r\n"
        b"Subject: =?utf-8?Q?Relat=C3=B3rio_Mensal?=\r\n"
        b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Body content.\r\n"
    )


@pytest.fixture(scope="module")
def multipart_email_bytes() -> bytes:
    """RFC 822 multipart/mixed email bytes with a text body and a PDF attachment."""
    msg = MIMEMultipart("mixed")
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Subject"] = "Email with attachment"

    body = MIMEText("This email has an attachment.", "plain", "utf-8")
    msg.attach(body)

    pdf_part = MIMEBase("application", "pdf")
    pdf_part.set_payload(b"fake PDF content for testing")
    encoders.encode_base64(pdf_part)
    pdf_part.add_header("Content-Disposition", "attachment", filename="report.pdf")
    msg.attach(pdf_part)

    return msg.as_bytes()
