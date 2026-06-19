"""Live round-trip tests for the real EventKitStore (PyObjC) backend.

These are gated behind macOS **and** an explicit opt-in env var because they
touch the real local Reminders database (create/complete/delete a throwaway
list and its tasks) and require the Reminders privacy permission to be granted
to the process running pytest.

Run with::

    ICLOUD_MCP_LIVE_EVENTKIT=1 uv run pytest tests/test_eventkit_store.py -v

The first run triggers the macOS permission prompt for the terminal/Python.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from icloud_mcp.config import ICloudMailSettings
from icloud_mcp.models import Reminder, ReminderAlarm

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("ICLOUD_MCP_LIVE_EVENTKIT") != "1",
    reason="Live EventKit tests require macOS and ICLOUD_MCP_LIVE_EVENTKIT=1 (mutates Reminders).",
)

_SETTINGS = ICloudMailSettings(  # type: ignore[call-arg]
    icloud_email="x@icloud.com", icloud_app_password="pw"
)


@pytest.fixture
async def store():  # type: ignore[no-untyped-def]
    from icloud_mcp.eventkit_store import EventKitStore

    s = EventKitStore(_SETTINGS)
    await s.connect()
    return s


@pytest.fixture
async def temp_list(store):  # type: ignore[no-untyped-def]
    """Create a throwaway Reminders list, yield it, and delete it afterwards."""
    name = f"icloud-mcp-test-{uuid.uuid4().hex[:8]}"
    rlist = await store.create_list(name, None)
    try:
        yield rlist
    finally:
        await store.delete_list(rlist.identifier)


async def test_create_fetch_complete_delete_roundtrip(store, temp_list) -> None:  # type: ignore[no-untyped-def]
    due = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    draft = Reminder(
        uid="",
        list=temp_list.name,
        summary="buy milk",
        due=due,
        priority=1,
        description="2%",
        url="https://example.com",
    )
    created = await store.create_reminder(temp_list.identifier, draft)
    assert created.uid
    assert created.summary == "buy milk"
    assert created.priority == 1
    assert created.description == "2%"
    assert created.url == "https://example.com"
    assert created.due == due

    fetched = await store.fetch_reminder(created.uid)
    assert fetched is not None and fetched.summary == "buy milk"

    done = await store.set_completion(created.uid, completed=True)
    assert done.completed is True

    await store.delete_reminder(created.uid)
    assert await store.fetch_reminder(created.uid) is None


async def test_all_day_reminder_roundtrip(store, temp_list) -> None:  # type: ignore[no-untyped-def]
    draft = Reminder(
        uid="",
        list=temp_list.name,
        summary="all day",
        due=datetime(2026, 7, 1, tzinfo=UTC),
        all_day=True,
    )
    created = await store.create_reminder(temp_list.identifier, draft)
    fetched = await store.fetch_reminder(created.uid)
    assert fetched is not None
    assert fetched.all_day is True
    assert fetched.due is not None and (fetched.due.year, fetched.due.month, fetched.due.day) == (
        2026,
        7,
        1,
    )
    await store.delete_reminder(created.uid)


async def test_recurrence_roundtrip(store, temp_list) -> None:  # type: ignore[no-untyped-def]
    draft = Reminder(
        uid="",
        list=temp_list.name,
        summary="weekly",
        due=datetime(2026, 7, 6, 9, tzinfo=UTC),
        rrule="FREQ=WEEKLY;BYDAY=MO",
        is_recurring=True,
    )
    created = await store.create_reminder(temp_list.identifier, draft)
    fetched = await store.fetch_reminder(created.uid)
    assert fetched is not None
    assert fetched.is_recurring is True
    assert fetched.rrule is not None
    assert "FREQ=WEEKLY" in fetched.rrule
    assert "MO" in fetched.rrule
    await store.delete_reminder(created.uid)


async def test_relative_alarm_roundtrip(store, temp_list) -> None:  # type: ignore[no-untyped-def]
    draft = Reminder(
        uid="",
        list=temp_list.name,
        summary="with alarm",
        due=datetime(2026, 7, 1, 9, tzinfo=UTC),
        alarms=[ReminderAlarm(minutes_before=30)],
    )
    created = await store.create_reminder(temp_list.identifier, draft)
    fetched = await store.fetch_reminder(created.uid)
    assert fetched is not None
    assert len(fetched.alarms) == 1
    assert fetched.alarms[0].minutes_before == 30
    await store.delete_reminder(created.uid)


async def test_absolute_alarm_roundtrip(store, temp_list) -> None:  # type: ignore[no-untyped-def]
    trigger = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    draft = Reminder(
        uid="",
        list=temp_list.name,
        summary="abs alarm",
        alarms=[ReminderAlarm(trigger=trigger)],
    )
    created = await store.create_reminder(temp_list.identifier, draft)
    fetched = await store.fetch_reminder(created.uid)
    assert fetched is not None
    assert len(fetched.alarms) == 1
    assert fetched.alarms[0].trigger is not None
    await store.delete_reminder(created.uid)


async def test_move_between_lists(store, temp_list) -> None:  # type: ignore[no-untyped-def]
    other_name = f"icloud-mcp-test-{uuid.uuid4().hex[:8]}"
    other = await store.create_list(other_name, None)
    try:
        created = await store.create_reminder(
            temp_list.identifier, Reminder(uid="", list=temp_list.name, summary="mover")
        )
        moved = await store.move_reminder(created.uid, other.identifier)
        assert moved.list == other.name
        await store.delete_reminder(created.uid)
    finally:
        await store.delete_list(other.identifier)
