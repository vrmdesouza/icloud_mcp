"""Custom exceptions for the iCloud MCP server."""


class ICloudError(Exception):
    """Base exception for all iCloud MCP errors.

    All custom exceptions in this package inherit from this class,
    allowing callers to catch any package-level error with a single handler.
    """


# Backward-compatible alias: the base used to be Mail-specific.
ICloudMailError = ICloudError


class IMAPConnectionError(ICloudError):
    """Raised when an IMAP connection or pool operation fails.

    This covers scenarios such as failure to establish a connection,
    pool exhaustion, and persistent disconnects after retry attempts.

    Example:
        raise IMAPConnectionError("Falha ao conectar ao servidor IMAP após 3 tentativas.")
    """


class IMAPAuthenticationError(ICloudError):
    """Raised when IMAP login or credential validation fails.

    Typically caused by an invalid email address or an incorrect
    App-Specific Password.

    Example:
        raise IMAPAuthenticationError("Credenciais inválidas para o servidor IMAP.")
    """


class SMTPSendError(ICloudError):
    """Raised when an SMTP send operation fails after retries.

    Wraps the original transport-level error to preserve context.

    Example:
        raise SMTPSendError("Falha ao enviar e-mail via SMTP.") from original_exc
    """


class CalDAVError(ICloudError):
    """Base exception for iCloud Calendar (CalDAV) errors.

    Covers protocol-level failures that are not strictly connection or
    authentication problems (e.g. an unexpected server response).

    Example:
        raise CalDAVError("Resposta inesperada do servidor CalDAV.")
    """


class CalDAVConnectionError(CalDAVError):
    """Raised when a CalDAV request or service discovery fails.

    This covers transport errors, failed principal/calendar-home discovery,
    and persistent failures after retry attempts.

    Example:
        raise CalDAVConnectionError("Falha ao descobrir o calendar-home-set no iCloud.")
    """


class CalDAVAuthenticationError(CalDAVError):
    """Raised when CalDAV authentication fails (HTTP 401).

    Typically caused by using the regular Apple ID password instead of an
    App-Specific Password, or by credentials revoked after a password reset.

    Example:
        raise CalDAVAuthenticationError("Credenciais inválidas para o servidor CalDAV.")
    """


class EventKitError(ICloudError):
    """Base exception for iCloud Reminders (EventKit) errors.

    Covers operational failures of the native macOS Reminders backend, such as
    a save/remove that the store rejected, or a reminder/list not found.

    Example:
        raise EventKitError("Lembrete não encontrado.")
    """


class EventKitAuthorizationError(EventKitError):
    """Raised when access to Reminders is denied or restricted by macOS (TCC).

    The host process (Claude Desktop, the terminal, or Python itself) must be
    granted access to Reminders in Ajustes do Sistema → Privacidade e
    Segurança → Lembretes.

    Example:
        raise EventKitAuthorizationError("Acesso aos Lembretes negado pelo macOS.")
    """


class EventKitNotAvailableError(EventKitError):
    """Raised when the EventKit backend is unavailable.

    Typically because the server is running outside macOS, or the PyObjC
    EventKit bindings are not installed.

    Example:
        raise EventKitNotAvailableError("EventKit só está disponível no macOS.")
    """
