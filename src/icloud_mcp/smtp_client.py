"""Stateless SMTP client for sending emails via iCloud Mail.

Opens a fresh connection per send operation — no connection pooling needed.
Retries up to SMTP_MAX_ATTEMPTS times on transient failures.
"""

import asyncio
import email.utils
import logging
import re
from datetime import datetime
from email.message import EmailMessage

import aiosmtplib

from icloud_mcp.config import ICloudMailSettings
from icloud_mcp.exceptions import SMTPSendError
from icloud_mcp.models import Email

log = logging.getLogger(__name__)

SMTP_MAX_ATTEMPTS = 2

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email_address(addr: str) -> None:
    """Validate that an email address has a basic valid format.

    Args:
        addr: Email address string to validate.

    Raises:
        ValueError: If the address does not match a basic email pattern.
    """
    if not _EMAIL_RE.match(addr):
        raise ValueError(f"Endereço de e-mail inválido: '{addr}'.")


def _quote_text(body: str, sender: str, date: datetime | None) -> str:
    """Format a reply quotation block for the original email body.

    Args:
        body: Plain text body of the original email.
        sender: Sender address/name of the original email.
        date: Date of the original email, or None if unavailable.

    Returns:
        Attribution line followed by each body line prefixed with ``"> "``.
    """
    if date is not None:
        formatted_date = email.utils.format_datetime(date)
        attribution = f"On {formatted_date}, {sender} wrote:"
    else:
        attribution = f"{sender} wrote:"
    quoted_lines = "\n".join(f"> {line}" for line in body.splitlines())
    return f"{attribution}\n{quoted_lines}"


def _format_forward_body(original: Email) -> str:
    """Format the forwarded-message block for a forwarded email.

    Args:
        original: The original Email being forwarded.

    Returns:
        A formatted string containing the forwarded message header and body.
    """
    to_str = ", ".join(original.to)
    date_str = email.utils.format_datetime(original.date) if original.date is not None else ""
    return (
        "---------- Forwarded message ----------\n"
        f"From: {original.sender}\n"
        f"Date: {date_str}\n"
        f"Subject: {original.subject}\n"
        f"To: {to_str}\n"
        f"\n{original.body_text}"
    )


class SMTPClient:
    """Stateless SMTP client for iCloud Mail.

    Opens a new SMTP connection for each send operation. On transient failures,
    retries up to SMTP_MAX_ATTEMPTS times before raising SMTPSendError.

    Args:
        settings: Application settings containing SMTP credentials and host info.

    Example:
        client = SMTPClient(settings)
        result = await client.send_email(
            to=["recipient@example.com"],
            subject="Hello",
            body="World",
        )
    """

    def __init__(self, settings: ICloudMailSettings) -> None:
        self._settings = settings

    def _build_message(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> EmailMessage:
        """Construct an EmailMessage with the given headers and body.

        BCC recipients are intentionally excluded from headers — they are
        passed only in the SMTP envelope via send_message(recipients=...).

        Args:
            to: List of primary recipient email addresses.
            subject: Email subject line.
            body: Plain-text email body.
            cc: Optional list of CC recipient email addresses.
            in_reply_to: RFC 2822 In-Reply-To header value (parent message-id).
            references: RFC 2822 References header value (chain of message-ids).

        Returns:
            A fully constructed EmailMessage ready to be sent.
        """
        msg = EmailMessage()
        msg["From"] = self._settings.icloud_email
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = ", ".join(cc)
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references
        msg.set_content(body)
        return msg

    async def _send_message(
        self,
        msg: EmailMessage,
        recipients: list[str],
    ) -> dict[str, str]:
        """Send an EmailMessage via SMTP with automatic retry on failure.

        Args:
            msg: The fully constructed EmailMessage to send.
            recipients: SMTP envelope recipients (to + cc + bcc).

        Returns:
            A dict with ``{"status": "sent", "message_id": "<id>"}`` on success.

        Raises:
            SMTPSendError: If all send attempts fail.
        """
        last_exc: Exception = RuntimeError("Nenhuma tentativa realizada.")

        for attempt in range(1, SMTP_MAX_ATTEMPTS + 1):
            smtp = aiosmtplib.SMTP(
                hostname=self._settings.smtp_host,
                port=self._settings.smtp_port,
                start_tls=True,
            )
            try:
                await smtp.connect()
                await smtp.login(
                    self._settings.icloud_email,
                    self._settings.icloud_app_password,
                )
                await smtp.send_message(msg, recipients=recipients)
                message_id = msg.get("Message-ID", "")
                log.info("E-mail enviado com sucesso. Message-ID: %s", message_id)
                return {"status": "sent", "message_id": message_id}
            except Exception as exc:
                last_exc = exc
                if attempt < SMTP_MAX_ATTEMPTS:
                    log.warning(
                        "Falha ao enviar e-mail (tentativa %d/%d): %s. Tentando novamente...",
                        attempt,
                        SMTP_MAX_ATTEMPTS,
                        exc,
                    )
                else:
                    log.error(
                        "Falha ao enviar e-mail após %d tentativas: %s",
                        SMTP_MAX_ATTEMPTS,
                        exc,
                    )
            finally:
                try:
                    await asyncio.wait_for(smtp.quit(), timeout=5.0)
                except Exception:
                    pass

        raise SMTPSendError(
            f"Falha ao enviar e-mail após {SMTP_MAX_ATTEMPTS} tentativas."
        ) from last_exc

    async def send_email(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> dict[str, str]:
        """Send an email via iCloud SMTP with automatic retry on failure.

        Builds the message once, then attempts to connect and send up to
        SMTP_MAX_ATTEMPTS times. BCC recipients are included in the SMTP
        envelope but never appear in the message headers.

        Args:
            to: List of primary recipient email addresses.
            subject: Email subject line.
            body: Plain-text email body.
            cc: Optional list of CC recipient email addresses.
            bcc: Optional list of BCC recipient email addresses.

        Returns:
            A dict with ``{"status": "sent", "message_id": "<id>"}`` on success.

        Raises:
            SMTPSendError: If all send attempts fail.
        """
        for addr in to + (cc or []) + (bcc or []):
            _validate_email_address(addr)
        msg = self._build_message(to, subject, body, cc)
        recipients = to + (cc or []) + (bcc or [])
        return await self._send_message(msg, recipients)

    async def reply_email(
        self,
        original: Email,
        body: str,
        reply_all: bool = False,
    ) -> dict[str, str]:
        """Reply to an existing email, preserving the thread via RFC 2822 headers.

        The To field defaults to the original sender (or Reply-To if present).
        When reply_all=True, all original recipients are included (excluding self).

        Args:
            original: The Email being replied to.
            body: The reply body text (written by the user).
            reply_all: If True, include all original recipients in To/Cc.

        Returns:
            A dict with ``{"status": "sent", "message_id": "<id>"}`` on success.

        Raises:
            SMTPSendError: If all send attempts fail.
        """
        # Determine primary To address
        primary_to = original.reply_to if original.reply_to else original.sender

        if reply_all:
            self_email = self._settings.icloud_email.lower()
            to_addrs = [primary_to] + [addr for addr in original.to if addr.lower() != self_email]
            cc_addrs: list[str] | None = [
                addr for addr in original.cc if addr.lower() != self_email
            ] or None
            # Remove self from To as well
            to_addrs = [addr for addr in to_addrs if addr.lower() != self_email]
            # Guard: if filtering removed all To recipients, fall back to primary_to
            if not to_addrs:
                to_addrs = [primary_to]
        else:
            to_addrs = [primary_to]
            cc_addrs = None

        # Build subject with "Re: " prefix (avoid double-prefixing)
        if re.match(r"(?i)^re:\s", original.subject):
            subject = original.subject
        else:
            subject = f"Re: {original.subject}"

        # Build quoted body
        full_body = body + "\n\n" + _quote_text(original.body_text, original.sender, original.date)

        # Build threading headers
        in_reply_to = original.message_id
        references: str | None
        if original.references and original.message_id:
            references = original.references + " " + original.message_id
        elif original.message_id:
            references = original.message_id
        else:
            references = original.references

        msg = self._build_message(
            to=to_addrs,
            subject=subject,
            body=full_body,
            cc=cc_addrs,
            in_reply_to=in_reply_to,
            references=references,
        )
        recipients = to_addrs + (cc_addrs or [])
        return await self._send_message(msg, recipients)

    async def forward_email(
        self,
        original: Email,
        to: list[str],
        body: str | None = None,
    ) -> dict[str, str]:
        """Forward an existing email to new recipients.

        The original email body is appended as a forwarded-message block.
        No threading headers (In-Reply-To/References) are set on forwards.

        Args:
            original: The Email being forwarded.
            to: List of recipient email addresses for the forward.
            body: Optional introductory text to include before the forwarded block.

        Returns:
            A dict with ``{"status": "sent", "message_id": "<id>"}`` on success.

        Raises:
            SMTPSendError: If all send attempts fail.
        """
        # Build subject with "Fwd: " prefix (avoid double-prefixing)
        if re.match(r"(?i)^fwd:\s", original.subject):
            subject = original.subject
        else:
            subject = f"Fwd: {original.subject}"

        # Build body: user intro (if any) + forwarded block
        forward_block = _format_forward_body(original)
        full_body = (body + "\n\n" if body else "") + forward_block

        for addr in to:
            _validate_email_address(addr)
        msg = self._build_message(to=to, subject=subject, body=full_body)
        return await self._send_message(msg, to)
