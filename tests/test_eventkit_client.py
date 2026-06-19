"""Tests for EventKitClient orchestration against an in-memory fake store.

These exercise the pure-Python orchestration layer (list resolution, completed
filtering, due-date sorting, cross-list search, partial-update merging, clear
semantics) with no PyObjC involved, so they run on any platform.
"""

from datetime import UTC, datetime

import pytest

from icloud_mcp.config import ICloudMailSettings
from icloud_mcp.eventkit_client import EventKitClient
from icloud_mcp.exceptions import EventKitError
from icloud_mcp.models import Reminder, ReminderAlarm, ReminderList


class FakeReminderStore:
    """In-memory ReminderStore for testing EventKitClient orchestration."""

    def __init__(
        self,
        lists: list[ReminderList] | None = None,
        reminders: list[Reminder] | None = None,
    ) -> None:
        self.lists: list[ReminderList] = lists if lists is not None else []
        self.reminders: dict[str, Reminder] = {r.uid: r for r in (reminders or [])}
        self.connected = False
        self.closed = False
        self._seq = 0

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def fetch_lists(self) -> list[ReminderList]:
        return list(self.lists)

    def _name_for(self, list_id: str) -> str:
        for rlist in self.lists:
            if rlist.identifier == list_id:
                return rlist.name
        raise EventKitError(f"unknown list_id {list_id}")

    async def fetch_reminders(self, list_id: str) -> list[Reminder]:
        name = self._name_for(list_id)
        return [r for r in self.reminders.values() if r.list == name]

    async def fetch_reminder(self, uid: str) -> Reminder | None:
        return self.reminders.get(uid)

    async def create_reminder(self, list_id: str, reminder: Reminder) -> Reminder:
        name = self._name_for(list_id)
        self._seq += 1
        uid = f"uid-{self._seq}"
        created = reminder.model_copy(update={"uid": uid, "list": name})
        self.reminders[uid] = created
        return created

    async def update_reminder(self, reminder: Reminder) -> Reminder:
        if reminder.uid not in self.reminders:
            raise EventKitError(f"missing {reminder.uid}")
        self.reminders[reminder.uid] = reminder
        return reminder

    async def set_completion(self, uid: str, *, completed: bool) -> Reminder:
        current = self.reminders[uid]
        updated = current.model_copy(
            update={
                "completed": completed,
                "completed_at": datetime.now(UTC) if completed else None,
            }
        )
        self.reminders[uid] = updated
        return updated

    async def delete_reminder(self, uid: str) -> None:
        self.reminders.pop(uid, None)

    async def move_reminder(self, uid: str, to_list_id: str) -> Reminder:
        name = self._name_for(to_list_id)
        moved = self.reminders[uid].model_copy(update={"list": name})
        self.reminders[uid] = moved
        return moved

    async def create_list(self, name: str, color: str | None) -> ReminderList:
        rlist = ReminderList(name=name, identifier=f"id-{name}", color=color)
        self.lists.append(rlist)
        return rlist

    async def rename_list(self, list_id: str, new_name: str) -> ReminderList:
        for i, rlist in enumerate(self.lists):
            if rlist.identifier == list_id:
                renamed = rlist.model_copy(update={"name": new_name})
                self.lists[i] = renamed
                return renamed
        raise EventKitError(f"unknown list_id {list_id}")

    async def delete_list(self, list_id: str) -> None:
        self.lists = [r for r in self.lists if r.identifier != list_id]


_SETTINGS = ICloudMailSettings(icloud_email="x@icloud.com", icloud_app_password="pw")  # type: ignore[call-arg]


def _client(store: FakeReminderStore) -> EventKitClient:
    return EventKitClient(_SETTINGS, store=store)


def _tasks_list() -> ReminderList:
    return ReminderList(name="Tasks", identifier="id-tasks")


# -- lifecycle / lists ------------------------------------------------------


async def test_connect_and_close_delegate_to_store() -> None:
    store = FakeReminderStore()
    client = _client(store)
    await client.connect()
    await client.close()
    assert store.connected and store.closed


async def test_list_reminder_lists() -> None:
    store = FakeReminderStore(lists=[_tasks_list()])
    result = await _client(store).list_reminder_lists()
    assert [r.name for r in result] == ["Tasks"]


async def test_resolve_unknown_list_raises() -> None:
    store = FakeReminderStore(lists=[_tasks_list()])
    with pytest.raises(EventKitError, match="não encontrada"):
        await _client(store).list_reminders("Nope")


# -- read -------------------------------------------------------------------


async def test_list_reminders_hides_completed_by_default() -> None:
    store = FakeReminderStore(
        lists=[_tasks_list()],
        reminders=[
            Reminder(uid="a", list="Tasks", summary="open"),
            Reminder(uid="b", list="Tasks", summary="done", completed=True),
        ],
    )
    result = await _client(store).list_reminders("Tasks")
    assert [r.uid for r in result] == ["a"]


async def test_list_reminders_include_completed() -> None:
    store = FakeReminderStore(
        lists=[_tasks_list()],
        reminders=[
            Reminder(uid="a", list="Tasks", summary="open"),
            Reminder(uid="b", list="Tasks", summary="done", completed=True),
        ],
    )
    result = await _client(store).list_reminders("Tasks", include_completed=True)
    assert {r.uid for r in result} == {"a", "b"}


async def test_list_reminders_sorted_dated_first_then_title() -> None:
    store = FakeReminderStore(
        lists=[_tasks_list()],
        reminders=[
            Reminder(uid="undated", list="Tasks", summary="zzz"),
            Reminder(uid="late", list="Tasks", summary="b", due=datetime(2026, 7, 2, tzinfo=UTC)),
            Reminder(uid="early", list="Tasks", summary="a", due=datetime(2026, 7, 1, tzinfo=UTC)),
        ],
    )
    result = await _client(store).list_reminders("Tasks")
    assert [r.uid for r in result] == ["early", "late", "undated"]


async def test_get_reminder_found_and_missing() -> None:
    store = FakeReminderStore(
        lists=[_tasks_list()],
        reminders=[Reminder(uid="a", list="Tasks", summary="x")],
    )
    client = _client(store)
    assert (await client.get_reminder("Tasks", "a")).uid == "a"
    with pytest.raises(EventKitError, match="não encontrado"):
        await client.get_reminder("Tasks", "missing")


# -- write ------------------------------------------------------------------


async def test_create_reminder_sets_list_name_and_fields() -> None:
    store = FakeReminderStore(lists=[_tasks_list()])
    created = await _client(store).create_reminder(
        "Tasks", summary="Pay rent", due=datetime(2026, 7, 1, 9, tzinfo=UTC), priority=1
    )
    assert created.list == "Tasks"
    assert created.summary == "Pay rent"
    assert created.priority == 1
    assert created.uid in store.reminders


async def test_update_reminder_merges_only_provided_fields() -> None:
    store = FakeReminderStore(
        lists=[_tasks_list()],
        reminders=[
            Reminder(uid="a", list="Tasks", summary="old", description="keep me", priority=5),
        ],
    )
    updated = await _client(store).update_reminder("Tasks", "a", summary="new")
    assert updated.summary == "new"
    assert updated.description == "keep me"  # untouched
    assert updated.priority == 5


async def test_update_reminder_clear_unsets_field() -> None:
    store = FakeReminderStore(
        lists=[_tasks_list()],
        reminders=[
            Reminder(uid="a", list="Tasks", summary="x", due=datetime(2026, 7, 1, tzinfo=UTC)),
        ],
    )
    updated = await _client(store).update_reminder("Tasks", "a", clear=["due"])
    assert updated.due is None


async def test_update_reminder_clear_then_set_wins() -> None:
    store = FakeReminderStore(
        lists=[_tasks_list()],
        reminders=[
            Reminder(uid="a", list="Tasks", summary="x", due=datetime(2026, 7, 1, tzinfo=UTC)),
        ],
    )
    new_due = datetime(2026, 8, 1, tzinfo=UTC)
    updated = await _client(store).update_reminder("Tasks", "a", due=new_due, clear=["due"])
    assert updated.due == new_due


async def test_update_reminder_clear_unknown_field_raises() -> None:
    store = FakeReminderStore(
        lists=[_tasks_list()],
        reminders=[Reminder(uid="a", list="Tasks", summary="x")],
    )
    with pytest.raises(EventKitError, match="não pode ser limpo"):
        await _client(store).update_reminder("Tasks", "a", clear=["summary"])


async def test_update_reminder_empty_rrule_clears_recurrence() -> None:
    store = FakeReminderStore(
        lists=[_tasks_list()],
        reminders=[
            Reminder(uid="a", list="Tasks", summary="x", rrule="FREQ=DAILY", is_recurring=True),
        ],
    )
    updated = await _client(store).update_reminder("Tasks", "a", rrule="")
    assert updated.rrule is None
    assert updated.is_recurring is False


async def test_update_reminder_replaces_alarms() -> None:
    store = FakeReminderStore(
        lists=[_tasks_list()],
        reminders=[Reminder(uid="a", list="Tasks", summary="x")],
    )
    alarms = [ReminderAlarm(minutes_before=30)]
    updated = await _client(store).update_reminder("Tasks", "a", alarms=alarms)
    assert updated.alarms == alarms


async def test_complete_and_reopen_reminder() -> None:
    store = FakeReminderStore(
        lists=[_tasks_list()],
        reminders=[Reminder(uid="a", list="Tasks", summary="x")],
    )
    client = _client(store)
    done = await client.complete_reminder("Tasks", "a")
    assert done.completed is True
    reopened = await client.reopen_reminder("Tasks", "a")
    assert reopened.completed is False


async def test_delete_reminder() -> None:
    store = FakeReminderStore(
        lists=[_tasks_list()],
        reminders=[Reminder(uid="a", list="Tasks", summary="x")],
    )
    result = await _client(store).delete_reminder("Tasks", "a")
    assert result == {"status": "deleted", "uid": "a"}
    assert "a" not in store.reminders


async def test_move_reminder_changes_list() -> None:
    store = FakeReminderStore(
        lists=[_tasks_list(), ReminderList(name="Personal", identifier="id-personal")],
        reminders=[Reminder(uid="a", list="Tasks", summary="x")],
    )
    moved = await _client(store).move_reminder("a", from_list="Tasks", to_list="Personal")
    assert moved.list == "Personal"


# -- list management --------------------------------------------------------


async def test_create_rename_delete_reminder_list() -> None:
    store = FakeReminderStore()
    client = _client(store)
    created = await client.create_reminder_list("Groceries", color="#00FF00")
    assert created.name == "Groceries"
    renamed = await client.rename_reminder_list("Groceries", "Shopping")
    assert renamed.name == "Shopping"
    result = await client.delete_reminder_list("Shopping", confirm=True)
    assert result == {"status": "deleted_list", "list": "Shopping"}
    assert store.lists == []


async def test_delete_reminder_list_requires_confirm() -> None:
    store = FakeReminderStore(lists=[_tasks_list()])
    with pytest.raises(EventKitError, match="confirm=True"):
        await _client(store).delete_reminder_list("Tasks")


# -- search -----------------------------------------------------------------


def _search_store() -> FakeReminderStore:
    return FakeReminderStore(
        lists=[_tasks_list(), ReminderList(name="Personal", identifier="id-personal")],
        reminders=[
            Reminder(
                uid="o1", list="Tasks", summary="overdue", due=datetime(2026, 6, 10, tzinfo=UTC)
            ),
            Reminder(
                uid="f1", list="Tasks", summary="future", due=datetime(2026, 7, 1, tzinfo=UTC)
            ),
            Reminder(uid="u1", list="Personal", summary="someday call mom"),
            Reminder(uid="d1", list="Personal", summary="done", completed=True),
        ],
    )


async def test_search_all_lists_default_hides_completed() -> None:
    result = await _client(_search_store()).search_reminders()
    assert {r.uid for r in result} == {"o1", "f1", "u1"}


async def test_search_overdue_preset() -> None:
    result = await _client(_search_store()).search_reminders(
        due_before=datetime(2026, 6, 18, tzinfo=UTC), undated=False
    )
    assert {r.uid for r in result} == {"o1"}


async def test_search_due_window() -> None:
    result = await _client(_search_store()).search_reminders(
        due_after=datetime(2026, 6, 20, tzinfo=UTC),
        due_before=datetime(2026, 8, 1, tzinfo=UTC),
        undated=False,
    )
    assert {r.uid for r in result} == {"f1"}


async def test_search_query_matches_title() -> None:
    result = await _client(_search_store()).search_reminders(query="mom")
    assert {r.uid for r in result} == {"u1"}


async def test_search_include_completed() -> None:
    result = await _client(_search_store()).search_reminders(include_completed=True)
    assert "d1" in {r.uid for r in result}


async def test_search_restrict_lists() -> None:
    result = await _client(_search_store()).search_reminders(lists=["Personal"])
    assert {r.uid for r in result} == {"u1"}


async def test_search_unknown_list_raises() -> None:
    with pytest.raises(EventKitError, match="não encontrada"):
        await _client(_search_store()).search_reminders(lists=["Ghost"])
