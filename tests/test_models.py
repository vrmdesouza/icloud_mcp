"""Tests for models.py — Pydantic data models."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from icloud_mail_mcp.models import Email, Folder, SearchQuery


def test_email_defaults() -> None:
    """Email with only required fields has correct defaults for all optional attributes."""
    email = Email(uid="42", folder="INBOX")
    assert email.subject == ""
    assert email.to == []
    assert email.cc == []
    assert email.date is None
    assert email.is_read is False
    assert email.attachments == []
    assert email.body_text == ""
    assert email.body_html == ""


def test_email_serialization_roundtrip() -> None:
    """Email survives a model_dump(mode='json') → model_validate roundtrip."""
    original = Email(
        uid="1",
        folder="INBOX",
        subject="Hello",
        sender="alice@example.com",
        to=["bob@example.com"],
        cc=["cc@example.com"],
        date=datetime(2024, 1, 15, 10, 30, 0),
        body_text="Hello world",
        is_read=True,
    )
    data = original.model_dump(mode="json")
    restored = Email.model_validate(data)
    assert restored.uid == original.uid
    assert restored.subject == original.subject
    assert restored.sender == original.sender
    assert restored.to == original.to
    assert restored.date == original.date
    assert restored.is_read == original.is_read


def test_search_query_limit_validation() -> None:
    """SearchQuery.limit enforces ge=1 and le=100 constraints."""
    assert SearchQuery(limit=1).limit == 1
    assert SearchQuery(limit=100).limit == 100

    with pytest.raises(ValidationError):
        SearchQuery(limit=0)
    with pytest.raises(ValidationError):
        SearchQuery(limit=101)
    with pytest.raises(ValidationError):
        SearchQuery(limit=-1)


def test_folder_model() -> None:
    """Folder has correct defaults and stores explicitly provided field values."""
    folder = Folder(name="INBOX")
    assert folder.name == "INBOX"
    assert folder.delimiter == "/"
    assert folder.flags == []

    folder2 = Folder(name="Sent Messages", delimiter=".", flags=["\\HasNoChildren"])
    assert folder2.name == "Sent Messages"
    assert folder2.delimiter == "."
    assert "\\HasNoChildren" in folder2.flags
