"""Tests for models.py — Pydantic data models."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from icloud_mail_mcp.models import Email, Folder, RuleAction, RuleCondition, SearchQuery


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


def test_email_threading_fields() -> None:
    """Email threading fields default to None and store values when provided."""
    email = Email(uid="1", folder="INBOX")
    assert email.message_id is None
    assert email.in_reply_to is None
    assert email.references is None
    assert email.reply_to is None

    email2 = Email(
        uid="2",
        folder="INBOX",
        message_id="<abc@mail.example.com>",
        in_reply_to="<parent@mail.example.com>",
        references="<root@mail.example.com> <parent@mail.example.com>",
        reply_to="reply@example.com",
    )
    assert email2.message_id == "<abc@mail.example.com>"
    assert email2.in_reply_to == "<parent@mail.example.com>"
    assert email2.references == "<root@mail.example.com> <parent@mail.example.com>"
    assert email2.reply_to == "reply@example.com"

    data = email2.model_dump(mode="json")
    restored = Email.model_validate(data)
    assert restored.message_id == email2.message_id
    assert restored.in_reply_to == email2.in_reply_to
    assert restored.references == email2.references
    assert restored.reply_to == email2.reply_to


# ─────────────────────────────────────────────────────────────────────────────
# Batch 5: Literal type constraints on RuleCondition / RuleAction
# ─────────────────────────────────────────────────────────────────────────────


def test_rule_condition_valid_literals() -> None:
    """RuleCondition accepts valid field and operator Literal values."""
    cond = RuleCondition(field="sender", operator="contains", value="test")
    assert cond.field == "sender"
    assert cond.operator == "contains"


def test_rule_condition_invalid_field_raises() -> None:
    """RuleCondition rejects an invalid field value via pydantic validation."""
    with pytest.raises(ValidationError):
        RuleCondition(field="date", operator="equals", value="x")


def test_rule_condition_invalid_operator_raises() -> None:
    """RuleCondition rejects an invalid operator value via pydantic validation."""
    with pytest.raises(ValidationError):
        RuleCondition(field="sender", operator="regex", value="x")


def test_rule_action_invalid_type_raises() -> None:
    """RuleAction rejects an invalid action_type via pydantic validation."""
    with pytest.raises(ValidationError):
        RuleAction(action_type="archive")
