"""Stateless SMTP client for sending emails via iCloud Mail.

Opens a fresh connection per send operation — no connection pooling needed.
Retries up to SMTP_MAX_ATTEMPTS times on transient failures.
"""

import logging
from email.message import EmailMessage

import aiosmtplib

from icloud_mail_mcp.config import ICloudMailSettings
from icloud_mail_mcp.exceptions import SMTPSendError

log = logging.getLogger(__name__)

SMTP_MAX_ATTEMPTS = 2


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
    ) -> EmailMessage:
        """Construct an EmailMessage with the given headers and body.

        BCC recipients are intentionally excluded from headers — they are
        passed only in the SMTP envelope via send_message(recipients=...).

        Args:
            to: List of primary recipient email addresses.
            subject: Email subject line.
            body: Plain-text email body.
            cc: Optional list of CC recipient email addresses.

        Returns:
            A fully constructed EmailMessage ready to be sent.
        """
        msg = EmailMessage()
        msg["From"] = self._settings.icloud_email
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg.set_content(body)
        return msg

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
        msg = self._build_message(to, subject, body, cc)
        recipients = to + (cc or []) + (bcc or [])
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
                    await smtp.quit()
                except Exception:
                    pass

        raise SMTPSendError(
            f"Falha ao enviar e-mail após {SMTP_MAX_ATTEMPTS} tentativas."
        ) from last_exc
