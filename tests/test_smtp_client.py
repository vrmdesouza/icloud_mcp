"""Tests for smtp_client.py — SMTP sending with retry logic."""

from unittest.mock import AsyncMock, patch

import pytest

from icloud_mail_mcp.config import ICloudMailSettings
from icloud_mail_mcp.exceptions import SMTPSendError
from icloud_mail_mcp.smtp_client import SMTP_MAX_ATTEMPTS, SMTPClient


async def test_send_success(settings: ICloudMailSettings, mock_smtp: AsyncMock) -> None:
    """send_email() returns status='sent' and calls connect/login/send_message once."""
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        result = await client.send_email(
            to=["recipient@example.com"],
            subject="Test",
            body="Hello",
        )
    assert result["status"] == "sent"
    assert "message_id" in result
    mock_smtp.connect.assert_called_once()
    mock_smtp.login.assert_called_once()
    mock_smtp.send_message.assert_called_once()


async def test_headers_correct(settings: ICloudMailSettings, mock_smtp: AsyncMock) -> None:
    """The built EmailMessage has correct From, To, and Subject headers."""
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        await client.send_email(
            to=["to@example.com"],
            subject="My Subject",
            body="Body text",
        )
    call_args = mock_smtp.send_message.call_args
    assert call_args is not None
    msg = call_args.args[0]
    assert msg["From"] == "test@icloud.com"
    assert msg["To"] == "to@example.com"
    assert msg["Subject"] == "My Subject"


async def test_bcc_not_in_headers(settings: ICloudMailSettings, mock_smtp: AsyncMock) -> None:
    """BCC addresses must not appear in message headers but must be in envelope recipients."""
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        await client.send_email(
            to=["to@example.com"],
            subject="Test",
            body="Body",
            bcc=["bcc@example.com"],
        )
    call_args = mock_smtp.send_message.call_args
    assert call_args is not None
    msg = call_args.args[0]
    call_kwargs = call_args.kwargs
    assert "Bcc" not in msg
    assert "bcc@example.com" in call_kwargs["recipients"]


async def test_retry_on_failure(settings: ICloudMailSettings, mock_smtp: AsyncMock) -> None:
    """First send_message failure triggers a retry that succeeds."""
    mock_smtp.send_message.side_effect = [Exception("transient error"), None]
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        result = await client.send_email(
            to=["to@example.com"],
            subject="Test",
            body="Body",
        )
    assert result["status"] == "sent"
    assert mock_smtp.send_message.call_count == 2


async def test_retries_exhausted(settings: ICloudMailSettings, mock_smtp: AsyncMock) -> None:
    """SMTPSendError is raised after all retry attempts are exhausted."""
    mock_smtp.send_message.side_effect = Exception("always fails")
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        with pytest.raises(SMTPSendError):
            await client.send_email(
                to=["to@example.com"],
                subject="Test",
                body="Body",
            )
    assert mock_smtp.send_message.call_count == SMTP_MAX_ATTEMPTS


async def test_recipients_include_cc_bcc(
    settings: ICloudMailSettings, mock_smtp: AsyncMock
) -> None:
    """SMTP envelope recipients include to + cc + bcc addresses."""
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        await client.send_email(
            to=["to@example.com"],
            subject="Test",
            body="Body",
            cc=["cc@example.com"],
            bcc=["bcc@example.com"],
        )
    call_args = mock_smtp.send_message.call_args
    assert call_args is not None
    recipients = call_args.kwargs["recipients"]
    assert "to@example.com" in recipients
    assert "cc@example.com" in recipients
    assert "bcc@example.com" in recipients
