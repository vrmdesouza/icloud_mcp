"""Tests for smtp_client.py — SMTP sending with retry logic."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from icloud_mail_mcp.config import ICloudMailSettings
from icloud_mail_mcp.exceptions import SMTPSendError
from icloud_mail_mcp.models import Email
from icloud_mail_mcp.smtp_client import SMTP_MAX_ATTEMPTS, SMTPClient


@pytest.fixture
def sample_email() -> Email:
    """Email model with threading headers for reply/forward tests."""
    return Email(
        uid="10",
        folder="INBOX",
        subject="Hello",
        sender="alice@example.com",
        to=["bob@example.com", "test@icloud.com"],
        cc=["cc@example.com"],
        date=datetime(2024, 1, 15, 10, 30, 0),
        body_text="Original body text.",
        message_id="<original@mail.example.com>",
        references="<root@mail.example.com>",
    )


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


# ─────────────────────────────────────────────────────────────────────────────
# reply_email tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_reply_email_success(
    settings: ICloudMailSettings, mock_smtp: AsyncMock, sample_email: Email
) -> None:
    """reply_email() sets In-Reply-To, References, Re: subject, To=sender, quoted body."""
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        result = await client.reply_email(original=sample_email, body="My reply.")

    assert result["status"] == "sent"
    msg = mock_smtp.send_message.call_args.args[0]
    assert msg["In-Reply-To"] == "<original@mail.example.com>"
    assert msg["References"] == "<root@mail.example.com> <original@mail.example.com>"
    assert msg["Subject"] == "Re: Hello"
    assert msg["To"] == "alice@example.com"
    assert "My reply." in msg.get_content()
    assert "alice@example.com wrote:" in msg.get_content()


async def test_reply_email_with_reply_to(
    settings: ICloudMailSettings, mock_smtp: AsyncMock, sample_email: Email
) -> None:
    """reply_email() uses Reply-To address as To when present, not From."""
    sample_email.reply_to = "alice-reply@example.com"
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        await client.reply_email(original=sample_email, body="Reply.")

    msg = mock_smtp.send_message.call_args.args[0]
    assert msg["To"] == "alice-reply@example.com"


async def test_reply_all(
    settings: ICloudMailSettings, mock_smtp: AsyncMock, sample_email: Email
) -> None:
    """reply_all=True includes original To (minus self) and original Cc in Cc."""
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        await client.reply_email(original=sample_email, body="All reply.", reply_all=True)

    msg = mock_smtp.send_message.call_args.args[0]
    # self (test@icloud.com) must not appear in To or Cc
    to_header = msg["To"] or ""
    cc_header = msg["Cc"] or ""
    assert "test@icloud.com" not in to_header
    assert "test@icloud.com" not in cc_header
    # original sender must be in To
    assert "alice@example.com" in to_header
    # original Cc (without self) must be in Cc
    assert "cc@example.com" in cc_header


async def test_reply_subject_already_prefixed(
    settings: ICloudMailSettings, mock_smtp: AsyncMock, sample_email: Email
) -> None:
    """reply_email() does not double-prefix subject that already starts with Re:."""
    sample_email.subject = "Re: Hello"
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        await client.reply_email(original=sample_email, body="Reply.")

    msg = mock_smtp.send_message.call_args.args[0]
    assert msg["Subject"] == "Re: Hello"
    assert msg["Subject"].count("Re:") == 1


# ─────────────────────────────────────────────────────────────────────────────
# forward_email tests
# ─────────────────────────────────────────────────────────────────────────────


async def test_forward_email_success(
    settings: ICloudMailSettings, mock_smtp: AsyncMock, sample_email: Email
) -> None:
    """forward_email() sets Fwd: subject, correct To, forwarded block, no threading headers."""
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        result = await client.forward_email(original=sample_email, to=["dave@example.com"])

    assert result["status"] == "sent"
    msg = mock_smtp.send_message.call_args.args[0]
    assert msg["Subject"] == "Fwd: Hello"
    assert msg["To"] == "dave@example.com"
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None
    content = msg.get_content()
    assert "Forwarded message" in content
    assert "alice@example.com" in content
    assert "Original body text." in content


async def test_forward_with_body(
    settings: ICloudMailSettings, mock_smtp: AsyncMock, sample_email: Email
) -> None:
    """forward_email() prepends user's intro text before the forwarded block."""
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        await client.forward_email(
            original=sample_email,
            to=["dave@example.com"],
            body="FYI, check this out.",
        )

    msg = mock_smtp.send_message.call_args.args[0]
    content = msg.get_content()
    assert content.index("FYI, check this out.") < content.index("Forwarded message")


async def test_forward_subject_already_prefixed(
    settings: ICloudMailSettings, mock_smtp: AsyncMock, sample_email: Email
) -> None:
    """forward_email() does not double-prefix subject that already starts with Fwd:."""
    sample_email.subject = "Fwd: Report"
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        await client.forward_email(original=sample_email, to=["dave@example.com"])

    msg = mock_smtp.send_message.call_args.args[0]
    assert msg["Subject"] == "Fwd: Report"
    assert msg["Subject"].count("Fwd:") == 1


# ─────────────────────────────────────────────────────────────────────────────
# Batch 3: SMTP quit timeout
# ─────────────────────────────────────────────────────────────────────────────


async def test_reply_all_empty_to_falls_back_to_sender(
    settings: ICloudMailSettings, mock_smtp: AsyncMock
) -> None:
    """reply_all falls back to original sender if filtering removes all To recipients."""
    # Email where the only To is the user themselves
    email_obj = Email(
        uid="10",
        folder="INBOX",
        subject="Hello",
        sender="test@icloud.com",  # same as settings.icloud_email
        to=["test@icloud.com"],
        cc=[],
        body_text="Body.",
        message_id="<msg@example.com>",
    )
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        result = await client.reply_email(original=email_obj, body="Reply.", reply_all=True)

    assert result["status"] == "sent"
    msg = mock_smtp.send_message.call_args.args[0]
    assert "test@icloud.com" in msg["To"]


async def test_quit_timeout_does_not_propagate(
    settings: ICloudMailSettings, mock_smtp: AsyncMock
) -> None:
    """TimeoutError on smtp.quit() is swallowed — send still succeeds."""
    mock_smtp.quit.side_effect = TimeoutError("quit timed out")
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        result = await client.send_email(to=["to@example.com"], subject="Test", body="Body")
    assert result["status"] == "sent"


# ─────────────────────────────────────────────────────────────────────────────
# Batch 6: Email address validation
# ─────────────────────────────────────────────────────────────────────────────


async def test_send_email_invalid_address_raises(
    settings: ICloudMailSettings, mock_smtp: AsyncMock
) -> None:
    """send_email raises ValueError for an invalid To address."""
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        with pytest.raises(ValueError, match="Endereço de e-mail inválido"):
            await client.send_email(to=["not-an-email"], subject="Test", body="Body")
    mock_smtp.connect.assert_not_called()


async def test_send_email_invalid_cc_address_raises(
    settings: ICloudMailSettings, mock_smtp: AsyncMock
) -> None:
    """send_email raises ValueError for an invalid CC address."""
    with patch("icloud_mail_mcp.smtp_client.aiosmtplib.SMTP", return_value=mock_smtp):
        client = SMTPClient(settings)
        with pytest.raises(ValueError, match="Endereço de e-mail inválido"):
            await client.send_email(
                to=["valid@example.com"],
                subject="Test",
                body="Body",
                cc=["bad address"],
            )
    mock_smtp.connect.assert_not_called()
