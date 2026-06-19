"""High-level async client for iCloud Reminders, backed by EventKit.

This is the orchestration layer over a :class:`~icloud_mcp.eventkit_store.ReminderStore`:
it resolves lists by display name, filters completed tasks, sorts by due date,
runs cross-list search, and merges partial updates — all in pure Python so it is
unit-testable against an in-memory fake store, with no PyObjC involved.

The native EventKit/PyObjC plumbing lives entirely in
:mod:`icloud_mcp.eventkit_store`. The public surface mirrors the old CalDAV
reminder methods 1:1 so the MCP tools in :mod:`icloud_mcp.server` delegate here
without any signature change.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from icloud_mcp.config import ICloudMailSettings
from icloud_mcp.eventkit_store import EventKitStore, ReminderStore
from icloud_mcp.exceptions import EventKitError
from icloud_mcp.models import Reminder, ReminderAlarm, ReminderList

# Far-future sentinel used to sort reminders without a due date last.
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)

# Fields that ``update_reminder(clear=...)`` may unset entirely.
_CLEARABLE_FIELDS = ("due", "start", "description", "url", "priority")


def _ensure_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _sort_key(reminder: Reminder) -> tuple[bool, datetime, str]:
    """Sort key: dated reminders first (by due), undated last, then by title."""
    return (reminder.due is None, reminder.due or _FAR_FUTURE, reminder.summary.lower())


class EventKitClient:
    """Async orchestration over the native macOS Reminders store.

    Args:
        settings: Application settings (carries ``eventkit_timeout``).
        store: Backend to use. Defaults to a real :class:`EventKitStore`;
            tests inject an in-memory fake implementing :class:`ReminderStore`.
    """

    def __init__(self, settings: ICloudMailSettings, store: ReminderStore | None = None) -> None:
        self._settings = settings
        self._store: ReminderStore = store if store is not None else EventKitStore(settings)

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Request Reminders access (triggers the macOS prompt on first use)."""
        await self._store.connect()

    async def close(self) -> None:
        """Release the backend (no-op for EventKit)."""
        close = getattr(self._store, "close", None)
        if close is not None:
            await close()

    # -- list resolution ---------------------------------------------------

    async def _resolve_reminder_list(self, list: str) -> ReminderList:
        """Find a reminders list by display name (case-insensitive)."""
        lists = await self._store.fetch_lists()
        for rlist in lists:
            if rlist.name.lower() == list.lower():
                return rlist
        available = ", ".join(rlist.name for rlist in lists) or "(nenhuma)"
        raise EventKitError(
            f"Lista de lembretes '{list}' não encontrada. Disponíveis: {available}."
        )

    # -- read --------------------------------------------------------------

    async def list_reminder_lists(self) -> list[ReminderList]:
        """List all Reminders lists."""
        return await self._store.fetch_lists()

    async def list_reminders(self, list: str, include_completed: bool = False) -> list[Reminder]:
        """List reminders in a list, ordered by due date (undated last).

        Args:
            list: Reminders list display name.
            include_completed: When ``False`` (default), completed tasks are
                filtered out; when ``True``, they are included.

        Returns:
            Reminders ordered by ``due`` ascending, undated ones last, then by
            title. For recurring tasks ``due`` already reflects the current
            occurrence (EventKit advances it natively on completion).
        """
        rlist = await self._resolve_reminder_list(list)
        reminders = await self._store.fetch_reminders(rlist.identifier)
        if not include_completed:
            reminders = [r for r in reminders if not r.completed]
        reminders.sort(key=_sort_key)
        return reminders

    async def get_reminder(self, list: str, uid: str) -> Reminder:
        """Fetch a single reminder by its identifier within a list.

        Raises:
            EventKitError: If no reminder with the given UID exists in the list.
        """
        rlist = await self._resolve_reminder_list(list)
        reminders = await self._store.fetch_reminders(rlist.identifier)
        for reminder in reminders:
            if reminder.uid == uid:
                return reminder
        raise EventKitError(f"Lembrete com UID '{uid}' não encontrado na lista '{list}'.")

    async def search_reminders(
        self,
        query: str | None = None,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
        include_completed: bool = False,
        undated: bool = True,
        lists: list[str] | None = None,
    ) -> list[Reminder]:
        """Search reminders across one or more lists, ordered by due date.

        Lists are fetched concurrently. Common presets: "overdue" =
        ``due_before=now, undated=False``; "due today" = ``due_before`` set to
        the end of today, ``undated=False``; free-text via ``query``.

        Args:
            query: Case-insensitive substring matched against ``summary`` and
                ``description``.
            due_before: Keep dated reminders with ``due`` strictly before this.
            due_after: Keep dated reminders with ``due`` at/after this.
            include_completed: When ``False`` (default), completed tasks are
                dropped.
            undated: Whether to include reminders without a ``due`` date.
            lists: Restrict to these list display names; ``None`` searches all.

        Raises:
            EventKitError: If ``lists`` names an unknown list.
        """
        all_lists = await self._store.fetch_lists()
        if lists is None:
            targets = all_lists
        else:
            by_name = {rl.name.lower(): rl for rl in all_lists}
            missing = [name for name in lists if name.lower() not in by_name]
            if missing:
                available = ", ".join(rl.name for rl in all_lists) or "(nenhuma)"
                raise EventKitError(
                    f"Lista(s) de lembretes não encontrada(s): {', '.join(missing)}. "
                    f"Disponíveis: {available}."
                )
            targets = [by_name[name.lower()] for name in lists]
        batches = await asyncio.gather(
            *(self._store.fetch_reminders(rl.identifier) for rl in targets)
        )
        reminders = [r for batch in batches for r in batch]
        needle = query.lower() if query else None
        after = _ensure_aware(due_after) if due_after else None
        before = _ensure_aware(due_before) if due_before else None
        filtered: list[Reminder] = []
        for r in reminders:
            if not include_completed and r.completed:
                continue
            if (
                needle is not None
                and needle not in r.summary.lower()
                and (r.description is None or needle not in r.description.lower())
            ):
                continue
            if r.due is None:
                if not undated:
                    continue
            else:
                if after is not None and r.due < after:
                    continue
                if before is not None and r.due >= before:
                    continue
            filtered.append(r)
        filtered.sort(key=_sort_key)
        return filtered

    # -- write -------------------------------------------------------------

    async def create_reminder(
        self,
        list: str,
        summary: str,
        due: datetime | None = None,
        start: datetime | None = None,
        all_day: bool = False,
        priority: int | None = None,
        description: str | None = None,
        url: str | None = None,
        rrule: str | None = None,
        alarms: list[ReminderAlarm] | None = None,
    ) -> Reminder:
        """Create a reminder (with or without a ``due`` deadline).

        Args:
            rrule: Optional recurrence rule (e.g. ``"FREQ=WEEKLY;BYDAY=MO"``).
            alarms: Optional display alarms on the task.
        """
        rlist = await self._resolve_reminder_list(list)
        draft = Reminder(
            uid="",
            list=rlist.name,
            summary=summary,
            due=due,
            start=start,
            all_day=all_day,
            priority=priority,
            description=description,
            url=url,
            rrule=rrule,
            is_recurring=bool(rrule),
            alarms=alarms or [],
        )
        return await self._store.create_reminder(rlist.identifier, draft)

    async def update_reminder(
        self,
        list: str,
        uid: str,
        summary: str | None = None,
        due: datetime | None = None,
        start: datetime | None = None,
        all_day: bool | None = None,
        priority: int | None = None,
        description: str | None = None,
        url: str | None = None,
        rrule: str | None = None,
        alarms: list[ReminderAlarm] | None = None,
        clear: list[str] | None = None,
    ) -> Reminder:
        """Update fields of an existing reminder; only provided fields change.

        Partial changes are merged onto the current reminder and the full
        desired state is written back. The store mutates the underlying
        ``EKReminder`` in place, so unmodeled native properties survive.

        Args:
            all_day: When provided, switches ``due``/``start`` between
                date-valued and datetime-valued; when omitted, preserved.
            rrule: ``None`` keeps the current recurrence; an empty string ``""``
                removes recurrence; any other value replaces the rule.
            alarms: ``None`` keeps the current alarms; any list (including the
                empty list) replaces all alarms.
            clear: Field names to unset entirely (one or more of ``due``,
                ``start``, ``description``, ``url``, ``priority``). Applied
                before the set fields, so clearing and setting the same field in
                one call ends up set.

        Raises:
            EventKitError: If ``clear`` names an unknown field.
        """
        current = await self.get_reminder(list, uid)
        updates: dict[str, object | None] = {}

        for field in clear or []:
            if field not in _CLEARABLE_FIELDS:
                allowed = ", ".join(_CLEARABLE_FIELDS)
                raise EventKitError(f"Campo '{field}' não pode ser limpo. Permitidos: {allowed}.")
            updates[field] = None

        if all_day is not None:
            updates["all_day"] = all_day
        if summary is not None:
            updates["summary"] = summary
        if description is not None:
            updates["description"] = description or None
        if url is not None:
            updates["url"] = url or None
        if priority is not None:
            updates["priority"] = priority
        if due is not None:
            updates["due"] = due
        if start is not None:
            updates["start"] = start
        if rrule is not None:
            updates["rrule"] = None if rrule == "" else rrule
        if alarms is not None:
            updates["alarms"] = alarms

        merged = current.model_copy(update=updates)
        merged.is_recurring = merged.rrule is not None
        return await self._store.update_reminder(merged)

    async def complete_reminder(self, list: str, uid: str) -> Reminder:
        """Mark a reminder as completed.

        For recurring tasks, EventKit advances the task to its next occurrence
        natively instead of completing the series.
        """
        await self.get_reminder(list, uid)  # validate the uid belongs to the list
        return await self._store.set_completion(uid, completed=True)

    async def reopen_reminder(self, list: str, uid: str) -> Reminder:
        """Reopen a completed reminder."""
        await self.get_reminder(list, uid)
        return await self._store.set_completion(uid, completed=False)

    async def delete_reminder(self, list: str, uid: str) -> dict[str, str]:
        """Delete a reminder by UID. Returns a status dict."""
        await self.get_reminder(list, uid)
        await self._store.delete_reminder(uid)
        return {"status": "deleted", "uid": uid}

    async def move_reminder(self, uid: str, from_list: str, to_list: str) -> Reminder:
        """Move a reminder to another list, preserving its identity."""
        await self.get_reminder(from_list, uid)  # validate the uid belongs to from_list
        dst = await self._resolve_reminder_list(to_list)
        return await self._store.move_reminder(uid, dst.identifier)

    # -- list management ---------------------------------------------------

    async def create_reminder_list(self, name: str, color: str | None = None) -> ReminderList:
        """Create a new Reminders list."""
        return await self._store.create_list(name, color)

    async def rename_reminder_list(self, name: str, new_name: str) -> ReminderList:
        """Rename a Reminders list."""
        rlist = await self._resolve_reminder_list(name)
        return await self._store.rename_list(rlist.identifier, new_name)

    async def delete_reminder_list(self, name: str, confirm: bool = False) -> dict[str, str]:
        """Delete a Reminders list and **all** its tasks. Requires ``confirm=True``."""
        if not confirm:
            raise EventKitError(
                "Exclusão de lista de lembretes requer confirm=True "
                "(apaga a lista e todas as suas tarefas)."
            )
        rlist = await self._resolve_reminder_list(name)
        await self._store.delete_list(rlist.identifier)
        return {"status": "deleted_list", "list": rlist.name}
