"""Custom exceptions for the iCloud Mail MCP server."""


class ICloudMailError(Exception):
    """Base exception for all iCloud Mail MCP errors.

    All custom exceptions in this package inherit from this class,
    allowing callers to catch any package-level error with a single handler.
    """


class IMAPConnectionError(ICloudMailError):
    """Raised when an IMAP connection or pool operation fails.

    This covers scenarios such as failure to establish a connection,
    pool exhaustion, and persistent disconnects after retry attempts.

    Example:
        raise IMAPConnectionError("Falha ao conectar ao servidor IMAP após 3 tentativas.")
    """


class IMAPAuthenticationError(ICloudMailError):
    """Raised when IMAP login or credential validation fails.

    Typically caused by an invalid email address or an incorrect
    App-Specific Password.

    Example:
        raise IMAPAuthenticationError("Credenciais inválidas para o servidor IMAP.")
    """


class SMTPSendError(ICloudMailError):
    """Raised when an SMTP send operation fails after retries.

    Wraps the original transport-level error to preserve context.

    Example:
        raise SMTPSendError("Falha ao enviar e-mail via SMTP.") from original_exc
    """
