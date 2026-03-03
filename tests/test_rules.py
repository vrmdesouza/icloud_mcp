"""Tests for the local rules engine (CRUD, matching, persistence, application)."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from icloud_mail_mcp.models import Email, EmailListResult
from icloud_mail_mcp.rules import RulesEngine

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def rules_engine(tmp_path: Path) -> RulesEngine:
    """RulesEngine backed by a temporary directory for test isolation."""
    return RulesEngine(rules_dir=tmp_path)


def _make_email(**kwargs: object) -> Email:
    """Shortcut to create an Email with sensible defaults."""
    defaults: dict[str, object] = {"uid": "1", "folder": "INBOX"}
    defaults.update(kwargs)
    return Email(**defaults)  # type: ignore[arg-type]


# ------------------------------------------------------------------
# CRUD tests
# ------------------------------------------------------------------


def test_list_rules_empty(rules_engine: RulesEngine) -> None:
    """A fresh engine starts with zero rules."""
    assert rules_engine.list_rules() == []


def test_create_rule(rules_engine: RulesEngine) -> None:
    """Creating a rule populates name, conditions, actions, and created_at."""
    rule = rules_engine.create_rule(
        name="newsletters",
        conditions=[{"field": "sender", "operator": "contains", "value": "newsletter"}],
        actions=[{"action_type": "move", "destination": "Newsletters"}],
    )
    assert rule.name == "newsletters"
    assert len(rule.conditions) == 1
    assert rule.conditions[0].field == "sender"
    assert len(rule.actions) == 1
    assert rule.actions[0].action_type == "move"
    assert rule.created_at is not None


def test_create_rule_duplicate_name(rules_engine: RulesEngine) -> None:
    """Creating a rule with an existing name raises ValueError."""
    rules_engine.create_rule(
        name="dup",
        conditions=[{"field": "subject", "operator": "equals", "value": "x"}],
        actions=[{"action_type": "mark_as_read"}],
    )
    with pytest.raises(ValueError, match="já existe"):
        rules_engine.create_rule(
            name="dup",
            conditions=[{"field": "subject", "operator": "equals", "value": "y"}],
            actions=[{"action_type": "mark_as_read"}],
        )


def test_create_rule_invalid_field(rules_engine: RulesEngine) -> None:
    """An invalid condition field raises ValueError."""
    with pytest.raises(ValueError, match="Campo inválido"):
        rules_engine.create_rule(
            name="bad_field",
            conditions=[{"field": "date", "operator": "equals", "value": "x"}],
            actions=[{"action_type": "mark_as_read"}],
        )


def test_create_rule_invalid_operator(rules_engine: RulesEngine) -> None:
    """An invalid operator raises ValueError."""
    with pytest.raises(ValueError, match="Operador inválido"):
        rules_engine.create_rule(
            name="bad_op",
            conditions=[{"field": "sender", "operator": "regex", "value": "x"}],
            actions=[{"action_type": "mark_as_read"}],
        )


def test_create_rule_invalid_action(rules_engine: RulesEngine) -> None:
    """An invalid action_type raises ValueError."""
    with pytest.raises(ValueError, match="Tipo de ação inválido"):
        rules_engine.create_rule(
            name="bad_action",
            conditions=[{"field": "sender", "operator": "equals", "value": "x"}],
            actions=[{"action_type": "archive"}],
        )


def test_create_rule_move_without_destination(rules_engine: RulesEngine) -> None:
    """A 'move' action without destination raises ValueError."""
    with pytest.raises(ValueError, match="destination"):
        rules_engine.create_rule(
            name="no_dest",
            conditions=[{"field": "sender", "operator": "equals", "value": "x"}],
            actions=[{"action_type": "move"}],
        )


def test_delete_rule(rules_engine: RulesEngine) -> None:
    """Deleting an existing rule returns confirmation and removes it from the list."""
    rules_engine.create_rule(
        name="to_delete",
        conditions=[{"field": "subject", "operator": "equals", "value": "x"}],
        actions=[{"action_type": "delete"}],
    )
    result = rules_engine.delete_rule("to_delete")
    assert result == {"status": "deleted", "name": "to_delete"}
    assert rules_engine.list_rules() == []


def test_delete_rule_not_found(rules_engine: RulesEngine) -> None:
    """Deleting a non-existent rule raises ValueError."""
    with pytest.raises(ValueError, match="não encontrada"):
        rules_engine.delete_rule("ghost")


# ------------------------------------------------------------------
# Matching tests
# ------------------------------------------------------------------


def test_condition_equals(rules_engine: RulesEngine) -> None:
    """'equals' operator matches exact (case-insensitive) field value."""
    rule = rules_engine.create_rule(
        name="eq",
        conditions=[{"field": "sender", "operator": "equals", "value": "alice@example.com"}],
        actions=[{"action_type": "mark_as_read"}],
    )
    email_match = _make_email(sender="Alice@Example.COM")
    email_no = _make_email(sender="bob@example.com")
    assert rules_engine._email_matches_rule(email_match, rule)
    assert not rules_engine._email_matches_rule(email_no, rule)


def test_condition_contains(rules_engine: RulesEngine) -> None:
    """'contains' operator checks substring presence."""
    rule = rules_engine.create_rule(
        name="ct",
        conditions=[{"field": "subject", "operator": "contains", "value": "invoice"}],
        actions=[{"action_type": "flag"}],
    )
    assert rules_engine._email_matches_rule(_make_email(subject="Your Invoice #123"), rule)
    assert not rules_engine._email_matches_rule(_make_email(subject="Hello"), rule)


def test_condition_starts_with(rules_engine: RulesEngine) -> None:
    """'starts_with' operator matches field prefix."""
    rule = rules_engine.create_rule(
        name="sw",
        conditions=[{"field": "subject", "operator": "starts_with", "value": "[alert]"}],
        actions=[{"action_type": "mark_as_read"}],
    )
    assert rules_engine._email_matches_rule(_make_email(subject="[ALERT] Server down"), rule)
    assert not rules_engine._email_matches_rule(_make_email(subject="No alert here"), rule)


def test_condition_ends_with(rules_engine: RulesEngine) -> None:
    """'ends_with' operator matches field suffix."""
    rule = rules_engine.create_rule(
        name="ew",
        conditions=[{"field": "sender", "operator": "ends_with", "value": "@company.com"}],
        actions=[{"action_type": "mark_as_read"}],
    )
    assert rules_engine._email_matches_rule(_make_email(sender="hr@company.com"), rule)
    assert not rules_engine._email_matches_rule(_make_email(sender="hr@other.com"), rule)


def test_condition_case_insensitive(rules_engine: RulesEngine) -> None:
    """Matching is case-insensitive for both field value and condition value."""
    rule = rules_engine.create_rule(
        name="ci",
        conditions=[{"field": "subject", "operator": "contains", "value": "URGENT"}],
        actions=[{"action_type": "flag"}],
    )
    assert rules_engine._email_matches_rule(_make_email(subject="This is urgent stuff"), rule)


def test_rule_and_logic(rules_engine: RulesEngine) -> None:
    """All conditions in a rule must match (AND logic)."""
    rule = rules_engine.create_rule(
        name="and",
        conditions=[
            {"field": "sender", "operator": "contains", "value": "shop"},
            {"field": "subject", "operator": "contains", "value": "receipt"},
        ],
        actions=[{"action_type": "mark_as_read"}],
    )
    both = _make_email(sender="shop@store.com", subject="Your receipt")
    only_sender = _make_email(sender="shop@store.com", subject="Welcome")
    assert rules_engine._email_matches_rule(both, rule)
    assert not rules_engine._email_matches_rule(only_sender, rule)


# ------------------------------------------------------------------
# Persistence tests
# ------------------------------------------------------------------


def test_rules_persist_to_disk(tmp_path: Path) -> None:
    """Rules survive across RulesEngine instances via JSON persistence."""
    engine1 = RulesEngine(rules_dir=tmp_path)
    engine1.create_rule(
        name="persist",
        conditions=[{"field": "sender", "operator": "equals", "value": "x"}],
        actions=[{"action_type": "mark_as_read"}],
    )
    engine2 = RulesEngine(rules_dir=tmp_path)
    assert len(engine2.list_rules()) == 1
    assert engine2.list_rules()[0].name == "persist"


def test_load_creates_directory(tmp_path: Path) -> None:
    """Loading rules creates the parent directory if it doesn't exist."""
    nested = tmp_path / "a" / "b" / "c"
    engine = RulesEngine(rules_dir=nested)
    assert nested.exists()
    assert engine.list_rules() == []


# ------------------------------------------------------------------
# Application tests (async, mocked IMAPClient)
# ------------------------------------------------------------------


async def test_apply_rules_mark_as_read(tmp_path: Path) -> None:
    """apply_rules calls mark_as_read when a matching rule has that action."""
    engine = RulesEngine(rules_dir=tmp_path)
    engine.create_rule(
        name="read_newsletters",
        conditions=[{"field": "sender", "operator": "contains", "value": "newsletter"}],
        actions=[{"action_type": "mark_as_read"}],
    )
    imap = AsyncMock()
    imap.list_emails.return_value = EmailListResult(
        emails=[_make_email(sender="newsletter@site.com", subject="Weekly")],
        total_count=1,
    )

    stats = await engine.apply_rules("INBOX", imap)

    imap.mark_as_read.assert_called_once_with(folder="INBOX", uid="1")
    assert stats["matched"] == 1
    assert stats["actions_applied"] == 1


async def test_apply_rules_move(tmp_path: Path) -> None:
    """apply_rules calls move_email and skips subsequent actions/rules for that email."""
    engine = RulesEngine(rules_dir=tmp_path)
    engine.create_rule(
        name="move_ads",
        conditions=[{"field": "subject", "operator": "contains", "value": "promo"}],
        actions=[
            {"action_type": "move", "destination": "Promotions"},
            {"action_type": "mark_as_read"},  # should be skipped after move
        ],
    )
    imap = AsyncMock()
    imap.list_emails.return_value = EmailListResult(
        emails=[_make_email(subject="Big Promo Sale!")],
        total_count=1,
    )

    stats = await engine.apply_rules("INBOX", imap)

    imap.move_email.assert_called_once_with(folder="INBOX", uid="1", destination="Promotions")
    imap.mark_as_read.assert_not_called()
    assert stats["actions_applied"] == 1


async def test_apply_rules_no_match(tmp_path: Path) -> None:
    """apply_rules with no matching emails reports zero actions."""
    engine = RulesEngine(rules_dir=tmp_path)
    engine.create_rule(
        name="no_match",
        conditions=[{"field": "sender", "operator": "equals", "value": "nobody@example.com"}],
        actions=[{"action_type": "delete"}],
    )
    imap = AsyncMock()
    imap.list_emails.return_value = EmailListResult(
        emails=[_make_email(sender="someone@example.com")],
        total_count=1,
    )

    stats = await engine.apply_rules("INBOX", imap)

    assert stats == {"processed": 1, "matched": 0, "actions_applied": 0}


async def test_apply_rules_stats(tmp_path: Path) -> None:
    """apply_rules returns correct processed/matched/actions_applied counts."""
    engine = RulesEngine(rules_dir=tmp_path)
    engine.create_rule(
        name="flag_urgent",
        conditions=[{"field": "subject", "operator": "contains", "value": "urgent"}],
        actions=[{"action_type": "flag"}, {"action_type": "mark_as_read"}],
    )
    imap = AsyncMock()
    imap.list_emails.return_value = EmailListResult(
        emails=[
            _make_email(uid="1", subject="Urgent request"),
            _make_email(uid="2", subject="Hello"),
            _make_email(uid="3", subject="Urgent fix needed"),
        ],
        total_count=3,
    )

    stats = await engine.apply_rules("INBOX", imap)

    assert stats["processed"] == 3
    assert stats["matched"] == 2
    assert stats["actions_applied"] == 4  # 2 emails × 2 actions each


async def test_apply_rules_no_enabled_rules(tmp_path: Path) -> None:
    """apply_rules with no enabled rules returns zero stats without listing emails."""
    engine = RulesEngine(rules_dir=tmp_path)
    imap = AsyncMock()

    stats = await engine.apply_rules("INBOX", imap)

    assert stats == {"processed": 0, "matched": 0, "actions_applied": 0}
    imap.list_emails.assert_not_called()


# ------------------------------------------------------------------
# Batch 5: Corrupted JSON, delete action, disabled rule, per-email error, pagination
# ------------------------------------------------------------------


def test_load_rules_corrupted_json(tmp_path: Path) -> None:
    """RulesEngine handles corrupted JSON gracefully."""
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("{not valid json!!!", encoding="utf-8")
    engine = RulesEngine(rules_dir=tmp_path)
    assert engine.list_rules() == []


async def test_apply_rules_delete_action(tmp_path: Path) -> None:
    """apply_rules calls delete_email when a matching rule has 'delete' action."""
    engine = RulesEngine(rules_dir=tmp_path)
    engine.create_rule(
        name="delete_spam",
        conditions=[{"field": "subject", "operator": "contains", "value": "spam"}],
        actions=[{"action_type": "delete"}],
    )
    imap = AsyncMock()
    imap.list_emails.return_value = EmailListResult(
        emails=[_make_email(uid="1", subject="This is spam!")],
        total_count=1,
    )

    stats = await engine.apply_rules("INBOX", imap)

    imap.delete_email.assert_called_once_with(folder="INBOX", uid="1")
    assert stats["actions_applied"] == 1


async def test_apply_rules_disabled_rule_skipped(tmp_path: Path) -> None:
    """apply_rules skips disabled rules."""
    engine = RulesEngine(rules_dir=tmp_path)
    rule = engine.create_rule(
        name="disabled",
        conditions=[{"field": "sender", "operator": "contains", "value": "test"}],
        actions=[{"action_type": "flag"}],
    )
    rule.enabled = False
    engine._save_rules()

    imap = AsyncMock()
    stats = await engine.apply_rules("INBOX", imap)

    assert stats == {"processed": 0, "matched": 0, "actions_applied": 0}
    imap.list_emails.assert_not_called()


async def test_apply_rules_per_email_error_continues(tmp_path: Path) -> None:
    """A failing action on one email does not abort processing of remaining emails."""
    engine = RulesEngine(rules_dir=tmp_path)
    engine.create_rule(
        name="flag_all",
        conditions=[{"field": "subject", "operator": "contains", "value": "test"}],
        actions=[{"action_type": "flag"}],
    )
    imap = AsyncMock()
    imap.list_emails.return_value = EmailListResult(
        emails=[
            _make_email(uid="1", subject="test one"),
            _make_email(uid="2", subject="test two"),
        ],
        total_count=2,
    )
    # First call fails, second succeeds
    imap.flag_email.side_effect = [Exception("fail"), None]

    stats = await engine.apply_rules("INBOX", imap)

    assert stats["processed"] == 2
    assert stats["matched"] == 2
    # Only the second action succeeded
    assert stats["actions_applied"] == 1


async def test_apply_rules_pagination_no_skip_after_move(tmp_path: Path) -> None:
    """After a move, re-fetching offset=0 picks up remaining emails without skipping."""
    engine = RulesEngine(rules_dir=tmp_path)
    engine.create_rule(
        name="move_promos",
        conditions=[{"field": "subject", "operator": "contains", "value": "promo"}],
        actions=[{"action_type": "move", "destination": "Promotions"}],
    )
    imap = AsyncMock()
    # Simulate a full batch (100 emails) to avoid early exit via len < batch_size.
    # First call: 100 emails, UID 1 matches and gets moved.
    batch_1 = [_make_email(uid=str(i), subject="normal") for i in range(2, 101)]
    batch_1.insert(0, _make_email(uid="1", subject="promo sale"))
    # Second call: UID 1 gone, UID 101 moved up, it matches too.
    batch_2 = [_make_email(uid=str(i), subject="normal") for i in range(2, 101)]
    batch_2.append(_make_email(uid="101", subject="promo deal"))
    imap.list_emails.side_effect = [
        EmailListResult(emails=batch_1, total_count=100),
        EmailListResult(emails=batch_2, total_count=100),
        EmailListResult(emails=[], total_count=0),
    ]

    stats = await engine.apply_rules("INBOX", imap)

    # Both promo emails should be processed and moved
    assert stats["matched"] == 2
    assert imap.move_email.call_count == 2
