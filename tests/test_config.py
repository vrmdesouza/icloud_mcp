"""Tests for config.py — settings loading and validation."""

import pytest
from pydantic import ValidationError

from icloud_mail_mcp.config import ICloudMailSettings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove iCloud env vars before each test to prevent .env file interference."""
    monkeypatch.delenv("ICLOUD_EMAIL", raising=False)
    monkeypatch.delenv("ICLOUD_APP_PASSWORD", raising=False)
    monkeypatch.delenv("IMAP_POOL_SIZE", raising=False)
    monkeypatch.delenv("IMAP_TIMEOUT", raising=False)


def test_valid_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings load correctly from environment variables with correct server endpoints."""
    monkeypatch.setenv("ICLOUD_EMAIL", "test@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx-xxxx-xxxx-xxxx")
    s = ICloudMailSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.icloud_email == "test@icloud.com"
    assert s.imap_host == "imap.mail.me.com"
    assert s.imap_port == 993
    assert s.smtp_host == "smtp.mail.me.com"
    assert s.smtp_port == 587


def test_missing_email_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """ValidationError is raised when ICLOUD_EMAIL is missing."""
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx-xxxx-xxxx-xxxx")
    with pytest.raises(ValidationError):
        ICloudMailSettings(_env_file=None)  # type: ignore[call-arg]


def test_missing_password_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """ValidationError is raised when ICLOUD_APP_PASSWORD is missing."""
    monkeypatch.setenv("ICLOUD_EMAIL", "test@icloud.com")
    with pytest.raises(ValidationError):
        ICloudMailSettings(_env_file=None)  # type: ignore[call-arg]


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default values for imap_pool_size=3 and imap_timeout=30 are applied."""
    monkeypatch.setenv("ICLOUD_EMAIL", "test@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "xxxx-xxxx-xxxx-xxxx")
    s = ICloudMailSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.imap_pool_size == 3
    assert s.imap_timeout == 30
