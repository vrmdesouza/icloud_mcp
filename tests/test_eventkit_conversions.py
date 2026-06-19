"""In-memory conversion tests for the EventKit store (no auth, no mutation).

These build throwaway ``EKReminder``/``EKAlarm`` objects in memory and exercise
the pure conversion helpers (recurrence, alarms, dates) round-trip. They need
macOS + PyObjC but **not** the Reminders permission, since nothing is fetched or
saved — so they run on any Mac dev machine and guard against PyObjC selector
typos (e.g. ``dayOfTheWeek`` vs ``dayOfWeek``).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="EventKit conversions require macOS + PyObjC."
)


@pytest.fixture
def ek_reminder():  # type: ignore[no-untyped-def]
    from EventKit import EKEventStore, EKReminder

    store = EKEventStore.alloc().init()
    return EKReminder.reminderWithEventStore_(store)


@pytest.mark.parametrize(
    "rrule",
    [
        "FREQ=DAILY",
        "FREQ=DAILY;COUNT=10",
        "FREQ=WEEKLY;BYDAY=MO",
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE,FR",
        "FREQ=MONTHLY;BYDAY=2TU",
        "FREQ=YEARLY",
    ],
)
def test_recurrence_rrule_roundtrip(ek_reminder, rrule: str) -> None:  # type: ignore[no-untyped-def]
    from icloud_mcp.eventkit_store import _apply_recurrence, _recurrence_to_rrule

    _apply_recurrence(ek_reminder, rrule)
    assert _recurrence_to_rrule(ek_reminder) == rrule


def test_recurrence_until_roundtrip(ek_reminder) -> None:  # type: ignore[no-untyped-def]
    from icloud_mcp.eventkit_store import _apply_recurrence, _recurrence_to_rrule

    _apply_recurrence(ek_reminder, "FREQ=WEEKLY;UNTIL=20260801T000000Z")
    out = _recurrence_to_rrule(ek_reminder)
    assert out is not None
    assert out.startswith("FREQ=WEEKLY")
    assert "UNTIL=20260801T000000Z" in out


def test_recurrence_clear(ek_reminder) -> None:  # type: ignore[no-untyped-def]
    from icloud_mcp.eventkit_store import _apply_recurrence, _recurrence_to_rrule

    _apply_recurrence(ek_reminder, "FREQ=DAILY")
    _apply_recurrence(ek_reminder, "")
    assert _recurrence_to_rrule(ek_reminder) is None


def test_relative_alarm_roundtrip(ek_reminder) -> None:  # type: ignore[no-untyped-def]
    from icloud_mcp.eventkit_store import _alarms_to_models, _apply_alarms
    from icloud_mcp.models import ReminderAlarm

    _apply_alarms(ek_reminder, [ReminderAlarm(minutes_before=30)])
    out = _alarms_to_models(ek_reminder)
    assert len(out) == 1
    assert out[0].minutes_before == 30
    assert out[0].trigger is None


def test_absolute_alarm_roundtrip(ek_reminder) -> None:  # type: ignore[no-untyped-def]
    from icloud_mcp.eventkit_store import _alarms_to_models, _apply_alarms
    from icloud_mcp.models import ReminderAlarm

    trigger = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    _apply_alarms(ek_reminder, [ReminderAlarm(trigger=trigger)])
    out = _alarms_to_models(ek_reminder)
    assert len(out) == 1
    assert out[0].minutes_before is None
    assert out[0].trigger == trigger


def test_alarms_cleared_with_empty_list(ek_reminder) -> None:  # type: ignore[no-untyped-def]
    from icloud_mcp.eventkit_store import _alarms_to_models, _apply_alarms
    from icloud_mcp.models import ReminderAlarm

    _apply_alarms(ek_reminder, [ReminderAlarm(minutes_before=15)])
    _apply_alarms(ek_reminder, [])
    assert _alarms_to_models(ek_reminder) == []


def test_timed_date_components_roundtrip() -> None:
    from icloud_mcp.eventkit_store import _components_to_dt, _dt_to_components

    value = datetime(2026, 7, 1, 9, 30, 0, tzinfo=UTC)
    dt, all_day = _components_to_dt(_dt_to_components(value, all_day=False))
    assert all_day is False
    assert dt == value


def test_all_day_date_components_roundtrip() -> None:
    from icloud_mcp.eventkit_store import _components_to_dt, _dt_to_components

    value = datetime(2026, 7, 1, tzinfo=UTC)
    dt, all_day = _components_to_dt(_dt_to_components(value, all_day=True))
    assert all_day is True
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2026, 7, 1)
