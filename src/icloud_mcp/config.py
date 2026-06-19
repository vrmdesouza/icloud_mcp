"""Configuration loading and validation for the iCloud MCP server.

Reads settings from environment variables (or a .env file) using pydantic-settings.
Fails fast at startup if required variables are missing.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ICloudMailSettings(BaseSettings):
    """Application settings loaded from environment variables.

    Required variables:
        ICLOUD_EMAIL: The iCloud email address (e.g. you@icloud.com).
        ICLOUD_APP_PASSWORD: An App-Specific Password generated at appleid.apple.com.

    Optional variables:
        IMAP_POOL_SIZE: Number of persistent IMAP connections to maintain (default 3).
        IMAP_TIMEOUT: Timeout in seconds for IMAP operations (default 30).
        CALDAV_TIMEOUT: Timeout in seconds for CalDAV (Calendar) operations (default 30).
        EVENTKIT_TIMEOUT: Timeout in seconds for EventKit (Reminders) fetches (default 30).

    The same App-Specific Password is used for Mail (IMAP/SMTP) and
    Calendar (CalDAV) — Apple shares the credential across both services.
    Reminders use the native macOS EventKit API and need no credential — access
    is granted locally via the macOS Reminders privacy permission.

    Example:
        settings = ICloudMailSettings()
        print(settings.icloud_email)
    """

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    icloud_email: str
    icloud_app_password: str
    imap_pool_size: int = 3
    imap_timeout: int = 30
    caldav_timeout: int = 30
    eventkit_timeout: int = 30

    @property
    def imap_host(self) -> str:
        """IMAP server hostname for iCloud Mail."""
        return "imap.mail.me.com"

    @property
    def imap_port(self) -> int:
        """IMAP server port (SSL/TLS)."""
        return 993

    @property
    def smtp_host(self) -> str:
        """SMTP server hostname for iCloud Mail."""
        return "smtp.mail.me.com"

    @property
    def smtp_port(self) -> int:
        """SMTP server port (STARTTLS)."""
        return 587

    @property
    def caldav_url(self) -> str:
        """Bootstrap CalDAV URL for iCloud service discovery.

        The actual calendar-home-set lives on a per-account partition host
        (e.g. ``https://p67-caldav.icloud.com``) discovered at runtime.
        """
        return "https://caldav.icloud.com"


@lru_cache(maxsize=1)
def get_settings() -> ICloudMailSettings:
    """Return the application settings singleton.

    Reads from environment variables and the .env file on first call;
    subsequent calls return the cached instance.

    Returns:
        The validated ICloudMailSettings instance.

    Raises:
        pydantic_settings.ValidationError: If required env vars are missing.
    """
    return ICloudMailSettings()  # type: ignore[call-arg]
