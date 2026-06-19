"""Native macOS Reminders backend via EventKit (PyObjC).

This module is the **only** place that touches PyObjC / ``EKEventStore``. It
exposes a narrow :class:`ReminderStore` protocol consumed by
:class:`icloud_mcp.eventkit_client.EventKitClient`, translating between the
native EventKit objects (``EKReminder``/``EKCalendar``/``EKAlarm``/
``EKRecurrenceRule``) and the Pydantic models in :mod:`icloud_mcp.models`.

Why EventKit and not CalDAV: since iOS 13 / macOS Catalina the Reminders app
migrates tasks off CalDAV into a private store that only the local Reminders
app (and EventKit) can read. CalDAV ``VTODO`` therefore only ever sees the empty
"Reminders ⚠️" shell on upgraded accounts. EventKit is the supported local path.

All public operations are ``async`` (they run the blocking PyObjC calls in a
worker thread via :func:`asyncio.to_thread`) and raise the EventKit exception
hierarchy from :mod:`icloud_mcp.exceptions` on failure.

Access requires the macOS Reminders privacy permission for the host process
(Claude Desktop, the terminal, or Python). The first :meth:`EventKitStore.connect`
triggers the system prompt; a denial raises :class:`EventKitAuthorizationError`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from datetime import UTC, datetime
from typing import Any, Protocol

from icloud_mcp.config import ICloudMailSettings
from icloud_mcp.exceptions import (
    EventKitAuthorizationError,
    EventKitError,
    EventKitNotAvailableError,
)
from icloud_mcp.models import Reminder, ReminderAlarm, ReminderList

log = logging.getLogger(__name__)

# -- guarded PyObjC import --------------------------------------------------
# EventKit only exists on macOS. Keep the import failure so __init__ can raise a
# clear, localized error instead of an ImportError deep in a call stack.
try:  # pragma: no cover - import guard, exercised only by platform
    import objc  # noqa: F401
    from EventKit import (
        EKAlarm,
        EKEntityTypeReminder,
        EKEventStore,
        EKRecurrenceDayOfWeek,
        EKRecurrenceEnd,
        EKRecurrenceFrequencyDaily,
        EKRecurrenceFrequencyMonthly,
        EKRecurrenceFrequencyWeekly,
        EKRecurrenceFrequencyYearly,
        EKRecurrenceRule,
        EKReminder,
    )
    from Foundation import (
        NSURL,
        NSCalendar,
        NSCalendarIdentifierGregorian,
        NSDate,
        NSDateComponents,
        NSTimeZone,
    )
    from icalendar.prop import vRecur

    # RRULE FREQ token <-> EKRecurrenceFrequency (constants are macOS-only).
    _FREQ_TO_EK = {
        "DAILY": EKRecurrenceFrequencyDaily,
        "WEEKLY": EKRecurrenceFrequencyWeekly,
        "MONTHLY": EKRecurrenceFrequencyMonthly,
        "YEARLY": EKRecurrenceFrequencyYearly,
    }
    _EK_TO_FREQ = {v: k for k, v in _FREQ_TO_EK.items()}

    _EVENTKIT_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - non-macOS / missing bindings
    _EVENTKIT_IMPORT_ERROR = exc

# RRULE BYDAY token <-> EKRecurrenceDayOfWeek index (1=Sunday … 7=Saturday).
_RRULE_TO_EK_WEEKDAY = {"SU": 1, "MO": 2, "TU": 3, "WE": 4, "TH": 5, "FR": 6, "SA": 7}
_EK_TO_RRULE_WEEKDAY = {v: k for k, v in _RRULE_TO_EK_WEEKDAY.items()}

# ``EKAuthorizationStatus``: 0 notDetermined, 1 restricted, 2 denied,
# 3 authorized / fullAccess (reminders). WriteOnly (4) is events-only.
_AUTH_AUTHORIZED = 3

# ``NSDateComponentUndefined`` (NSIntegerMax) marks an unset component field.
_NS_UNDEFINED = 9223372036854775807

# Shown whenever Reminders access is missing — actionable, in PT-BR.
_DENIED_MSG = (
    "Acesso aos Lembretes negado pelo macOS. Conceda em Ajustes do Sistema "
    "→ Privacidade e Segurança → Lembretes ao app que executa o servidor "
    "(Claude Desktop ou o terminal) e reinicie."
)


class ReminderStore(Protocol):
    """Narrow CRUD contract over a Reminders backend.

    Implemented by :class:`EventKitStore` (real, PyObjC) and by the in-memory
    fake used in tests. The orchestration layer (filtering, sorting, search,
    name resolution) lives in :class:`~icloud_mcp.eventkit_client.EventKitClient`,
    so this protocol stays deliberately dumb: lists and per-list fetches plus
    flat create/update/complete/delete/move and list management.
    """

    async def connect(self) -> None: ...
    async def fetch_lists(self) -> list[ReminderList]: ...
    async def fetch_reminders(self, list_id: str) -> list[Reminder]: ...
    async def fetch_reminder(self, uid: str) -> Reminder | None: ...
    async def create_reminder(self, list_id: str, reminder: Reminder) -> Reminder: ...
    async def update_reminder(self, reminder: Reminder) -> Reminder: ...
    async def set_completion(self, uid: str, *, completed: bool) -> Reminder: ...
    async def delete_reminder(self, uid: str) -> None: ...
    async def move_reminder(self, uid: str, to_list_id: str) -> Reminder: ...
    async def create_list(self, name: str, color: str | None) -> ReminderList: ...
    async def rename_list(self, list_id: str, new_name: str) -> ReminderList: ...
    async def delete_list(self, list_id: str) -> None: ...


class EventKitStore:
    """Real Reminders backend backed by the local macOS EventKit store.

    Args:
        settings: Application settings; only ``eventkit_timeout`` is used here
            (the wait budget for EventKit's async fetch completion handler).

    Raises:
        EventKitNotAvailableError: When constructed off macOS or without the
            PyObjC EventKit bindings installed.
    """

    def __init__(self, settings: ICloudMailSettings) -> None:
        if sys.platform != "darwin" or _EVENTKIT_IMPORT_ERROR is not None:
            raise EventKitNotAvailableError(
                "EventKit só está disponível no macOS com PyObjC instalado "
                f"(plataforma={sys.platform}, erro de import={_EVENTKIT_IMPORT_ERROR})."
            )
        self._settings = settings
        self._store = EKEventStore.alloc().init()

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Request Reminders access and verify it was granted.

        The first call triggers the macOS privacy prompt for the host process.

        Raises:
            EventKitAuthorizationError: If access is denied or restricted.
        """
        await asyncio.to_thread(self._request_access)

    async def close(self) -> None:
        """No-op: EventKit holds no connection to tear down."""

    def _request_access(self) -> None:
        status = EKEventStore.authorizationStatusForEntityType_(EKEntityTypeReminder)
        if status == _AUTH_AUTHORIZED:
            return

        done = threading.Event()
        result: dict[str, object] = {}

        def handler(granted: bool, error: object) -> None:
            result["granted"] = bool(granted)
            result["error"] = error
            done.set()

        # macOS 14+ split the entitlement into full/write-only access.
        if hasattr(self._store, "requestFullAccessToRemindersWithCompletion_"):
            self._store.requestFullAccessToRemindersWithCompletion_(handler)
        else:
            self._store.requestAccessToEntityType_completion_(EKEntityTypeReminder, handler)

        if not done.wait(timeout=self._settings.eventkit_timeout):
            raise EventKitAuthorizationError(
                "Tempo esgotado aguardando a autorização de acesso aos Lembretes."
            )
        if not result.get("granted"):
            raise EventKitAuthorizationError(_DENIED_MSG)

    def _assert_authorized(self) -> None:
        """Raise a clear error if Reminders access is not granted.

        Without this, EventKit silently returns no lists/tasks when access is
        missing — indistinguishable from genuinely empty Reminders. Re-reading
        the live status also picks up access granted after startup.
        """
        if EKEventStore.authorizationStatusForEntityType_(EKEntityTypeReminder) != _AUTH_AUTHORIZED:
            raise EventKitAuthorizationError(_DENIED_MSG)

    # -- lists -------------------------------------------------------------

    async def fetch_lists(self) -> list[ReminderList]:
        return await asyncio.to_thread(self._fetch_lists)

    def _fetch_lists(self) -> list[ReminderList]:
        self._assert_authorized()
        calendars = self._store.calendarsForEntityType_(EKEntityTypeReminder)
        return [_calendar_to_list(cal) for cal in calendars]

    async def create_list(self, name: str, color: str | None) -> ReminderList:
        return await asyncio.to_thread(self._create_list, name, color)

    def _create_list(self, name: str, color: str | None) -> ReminderList:
        from EventKit import EKCalendar

        self._assert_authorized()
        cal = EKCalendar.calendarForEntityType_eventStore_(EKEntityTypeReminder, self._store)
        cal.setTitle_(name)
        source = self._default_reminders_source()
        if source is not None:
            cal.setSource_(source)
        ns_color = _hex_to_nscolor(color)
        if ns_color is not None:
            cal.setColor_(ns_color)
        ok, err = self._store.saveCalendar_commit_error_(cal, True, None)
        if not ok:
            raise EventKitError(f"Falha ao criar a lista de lembretes: {err}")
        return _calendar_to_list(cal)

    async def rename_list(self, list_id: str, new_name: str) -> ReminderList:
        return await asyncio.to_thread(self._rename_list, list_id, new_name)

    def _rename_list(self, list_id: str, new_name: str) -> ReminderList:
        self._assert_authorized()
        cal = self._store.calendarWithIdentifier_(list_id)
        if cal is None:
            raise EventKitError(f"Lista de lembretes não encontrada: {list_id}")
        cal.setTitle_(new_name)
        ok, err = self._store.saveCalendar_commit_error_(cal, True, None)
        if not ok:
            raise EventKitError(f"Falha ao renomear a lista de lembretes: {err}")
        return _calendar_to_list(cal)

    async def delete_list(self, list_id: str) -> None:
        await asyncio.to_thread(self._delete_list, list_id)

    def _delete_list(self, list_id: str) -> None:
        self._assert_authorized()
        cal = self._store.calendarWithIdentifier_(list_id)
        if cal is None:
            raise EventKitError(f"Lista de lembretes não encontrada: {list_id}")
        ok, err = self._store.removeCalendar_commit_error_(cal, True, None)
        if not ok:
            raise EventKitError(f"Falha ao remover a lista de lembretes: {err}")

    def _default_reminders_source(self) -> Any:
        default_cal = self._store.defaultCalendarForNewReminders()
        if default_cal is not None:
            return default_cal.source()
        sources = self._store.sources()
        return sources[0] if sources else None

    # -- reminders: read ---------------------------------------------------

    async def fetch_reminders(self, list_id: str) -> list[Reminder]:
        return await asyncio.to_thread(self._fetch_reminders, list_id)

    def _fetch_reminders(self, list_id: str) -> list[Reminder]:
        cal = self._store.calendarWithIdentifier_(list_id)
        if cal is None:
            raise EventKitError(f"Lista de lembretes não encontrada: {list_id}")
        ek_reminders = self._fetch_matching([cal])
        return [_reminder_to_model(r) for r in ek_reminders]

    async def fetch_reminder(self, uid: str) -> Reminder | None:
        return await asyncio.to_thread(self._fetch_reminder, uid)

    def _fetch_reminder(self, uid: str) -> Reminder | None:
        ek = self._find_ek_reminder(uid)
        return _reminder_to_model(ek) if ek is not None else None

    def _fetch_matching(self, calendars: list[Any]) -> list[Any]:
        """Run EventKit's async predicate fetch and block for the result."""
        self._assert_authorized()
        predicate = self._store.predicateForRemindersInCalendars_(calendars)
        done = threading.Event()
        box: dict[str, Any] = {}

        def handler(reminders: Any) -> None:
            box["reminders"] = reminders
            done.set()

        self._store.fetchRemindersMatchingPredicate_completion_(predicate, handler)
        if not done.wait(timeout=self._settings.eventkit_timeout):
            raise EventKitError("Tempo esgotado ao buscar lembretes no EventKit.")
        reminders = box.get("reminders")
        return list(reminders) if reminders is not None else []

    def _find_ek_reminder(self, uid: str) -> Any:
        calendars = self._store.calendarsForEntityType_(EKEntityTypeReminder)
        for ek in self._fetch_matching(list(calendars)):
            if ek.calendarItemExternalIdentifier() == uid:
                return ek
        return None

    # -- reminders: write --------------------------------------------------

    async def create_reminder(self, list_id: str, reminder: Reminder) -> Reminder:
        return await asyncio.to_thread(self._create_reminder, list_id, reminder)

    def _create_reminder(self, list_id: str, reminder: Reminder) -> Reminder:
        self._assert_authorized()
        cal = self._store.calendarWithIdentifier_(list_id)
        if cal is None:
            raise EventKitError(f"Lista de lembretes não encontrada: {list_id}")
        ek = EKReminder.reminderWithEventStore_(self._store)
        ek.setCalendar_(cal)
        _apply_model_to_ek(ek, reminder)
        ok, err = self._store.saveReminder_commit_error_(ek, True, None)
        if not ok:
            raise EventKitError(f"Falha ao criar o lembrete: {err}")
        return _reminder_to_model(ek)

    async def update_reminder(self, reminder: Reminder) -> Reminder:
        return await asyncio.to_thread(self._update_reminder, reminder)

    def _update_reminder(self, reminder: Reminder) -> Reminder:
        ek = self._find_ek_reminder(reminder.uid)
        if ek is None:
            raise EventKitError(f"Lembrete não encontrado: {reminder.uid}")
        # Mutate the fetched object in place so unmodeled native properties
        # (location, X-APPLE flags, …) survive the round-trip.
        _apply_model_to_ek(ek, reminder)
        ok, err = self._store.saveReminder_commit_error_(ek, True, None)
        if not ok:
            raise EventKitError(f"Falha ao atualizar o lembrete: {err}")
        return _reminder_to_model(ek)

    async def set_completion(self, uid: str, *, completed: bool) -> Reminder:
        return await asyncio.to_thread(self._set_completion, uid, completed)

    def _set_completion(self, uid: str, completed: bool) -> Reminder:
        ek = self._find_ek_reminder(uid)
        if ek is None:
            raise EventKitError(f"Lembrete não encontrado: {uid}")
        # Only toggle completion — never rewrite due/rrule here — so EventKit's
        # native advance of recurring tasks on completion is left intact.
        ek.setCompleted_(completed)
        ok, err = self._store.saveReminder_commit_error_(ek, True, None)
        if not ok:
            raise EventKitError(f"Falha ao alterar a conclusão do lembrete: {err}")
        return _reminder_to_model(ek)

    async def delete_reminder(self, uid: str) -> None:
        await asyncio.to_thread(self._delete_reminder, uid)

    def _delete_reminder(self, uid: str) -> None:
        ek = self._find_ek_reminder(uid)
        if ek is None:
            raise EventKitError(f"Lembrete não encontrado: {uid}")
        ok, err = self._store.removeReminder_commit_error_(ek, True, None)
        if not ok:
            raise EventKitError(f"Falha ao remover o lembrete: {err}")

    async def move_reminder(self, uid: str, to_list_id: str) -> Reminder:
        return await asyncio.to_thread(self._move_reminder, uid, to_list_id)

    def _move_reminder(self, uid: str, to_list_id: str) -> Reminder:
        ek = self._find_ek_reminder(uid)
        if ek is None:
            raise EventKitError(f"Lembrete não encontrado: {uid}")
        cal = self._store.calendarWithIdentifier_(to_list_id)
        if cal is None:
            raise EventKitError(f"Lista de lembretes não encontrada: {to_list_id}")
        ek.setCalendar_(cal)
        ok, err = self._store.saveReminder_commit_error_(ek, True, None)
        if not ok:
            raise EventKitError(f"Falha ao mover o lembrete: {err}")
        return _reminder_to_model(ek)


# -- conversions: EKCalendar <-> ReminderList -------------------------------


def _calendar_to_list(cal: Any) -> ReminderList:
    return ReminderList(
        name=cal.title(),
        identifier=cal.calendarIdentifier(),
        color=_color_to_hex(cal),
        read_only=not cal.allowsContentModifications(),
    )


def _color_to_hex(cal: Any) -> str | None:
    try:
        color = cal.color()
        if color is None:
            return None
        rgb = color.colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
        if rgb is None:
            return None
        r = int(round(rgb.redComponent() * 255))
        g = int(round(rgb.greenComponent() * 255))
        b = int(round(rgb.blueComponent() * 255))
        return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:  # pragma: no cover - cosmetic; never fail a fetch over color
        return None


def _hex_to_nscolor(color: str | None) -> Any:
    if not color:
        return None
    try:  # pragma: no cover - AppKit only available on macOS
        from AppKit import NSColor

        h = color.lstrip("#")
        r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0)
    except Exception:
        return None


# -- conversions: EKReminder <-> Reminder -----------------------------------


def _reminder_to_model(ek: Any) -> Reminder:
    due, due_all_day = _components_to_dt(ek.dueDateComponents())
    start, start_all_day = _components_to_dt(ek.startDateComponents())
    rrule = _recurrence_to_rrule(ek)
    return Reminder(
        uid=ek.calendarItemExternalIdentifier(),
        list=ek.calendar().title(),
        summary=ek.title() or "",
        completed=bool(ek.isCompleted()),
        completed_at=_nsdate_to_dt(ek.completionDate()),
        due=due,
        start=start,
        all_day=due_all_day or start_all_day,
        priority=ek.priority() or None,
        description=ek.notes() or None,
        url=_nsurl_to_str(ek.URL()),
        rrule=rrule,
        is_recurring=rrule is not None,
        alarms=_alarms_to_models(ek),
        created=_nsdate_to_dt(ek.creationDate()),
        modified=_nsdate_to_dt(ek.lastModifiedDate()),
    )


def _apply_model_to_ek(ek: Any, reminder: Reminder) -> None:
    """Write every modeled field from ``reminder`` onto the EKReminder.

    The caller is responsible for having merged partial updates into a full
    desired-state model first; this writes that state verbatim (``None`` clears
    the corresponding native property).
    """
    ek.setTitle_(reminder.summary or "")
    ek.setNotes_(reminder.description)
    ek.setURL_(NSURL.URLWithString_(reminder.url) if reminder.url else None)
    ek.setPriority_(reminder.priority or 0)
    ek.setDueDateComponents_(_dt_to_components(reminder.due, reminder.all_day))
    ek.setStartDateComponents_(_dt_to_components(reminder.start, reminder.all_day))
    _apply_recurrence(ek, reminder.rrule)
    _apply_alarms(ek, reminder.alarms)


# -- conversions: dates -----------------------------------------------------


def _components_to_dt(comps: Any) -> tuple[datetime | None, bool]:
    """Convert ``NSDateComponents`` to an aware UTC datetime + all-day flag."""
    if comps is None:
        return None, False
    year, month, day = comps.year(), comps.month(), comps.day()
    if _NS_UNDEFINED in (year, month, day) or year <= 0:
        return None, False
    hour = comps.hour()
    all_day = hour == _NS_UNDEFINED
    if all_day:
        return datetime(year, month, day, tzinfo=UTC), True
    gregorian = NSCalendar.calendarWithIdentifier_(NSCalendarIdentifierGregorian)
    ns_date = gregorian.dateFromComponents_(comps)
    return datetime.fromtimestamp(ns_date.timeIntervalSince1970(), tz=UTC), False


def _dt_to_components(value: datetime | None, all_day: bool) -> Any:
    if value is None:
        return None
    comps = NSDateComponents.alloc().init()
    comps.setYear_(value.year)
    comps.setMonth_(value.month)
    comps.setDay_(value.day)
    if not all_day:
        comps.setHour_(value.hour)
        comps.setMinute_(value.minute)
        comps.setSecond_(value.second)
        utc_offset = value.utcoffset()
        offset = int(utc_offset.total_seconds()) if utc_offset else 0
        comps.setTimeZone_(NSTimeZone.timeZoneForSecondsFromGMT_(offset))
    return comps


def _nsdate_to_dt(ns_date: Any) -> datetime | None:
    if ns_date is None:
        return None
    return datetime.fromtimestamp(ns_date.timeIntervalSince1970(), tz=UTC)


def _dt_to_nsdate(value: datetime | None) -> Any:
    if value is None:
        return None
    return NSDate.dateWithTimeIntervalSince1970_(value.timestamp())


def _nsurl_to_str(ns_url: Any) -> str | None:
    if ns_url is None:
        return None
    return str(ns_url.absoluteString())


# -- conversions: recurrence ------------------------------------------------
#
# EventKit models recurrence as ``EKRecurrenceRule`` objects, not RRULE
# strings, so we map the common subset (FREQ/INTERVAL/BYDAY/COUNT/UNTIL) both
# ways. Exotic rules (BYSETPOS, BYMONTHDAY, …) are best-effort and may be lossy.


def _recurrence_to_rrule(ek: Any) -> str | None:
    """Serialize the reminder's first ``EKRecurrenceRule`` to an RRULE string."""
    rules = ek.recurrenceRules()
    if not rules:
        return None
    rule = rules[0]
    parts = [f"FREQ={_EK_TO_FREQ.get(rule.frequency(), 'DAILY')}"]
    interval = rule.interval()
    if interval and interval != 1:
        parts.append(f"INTERVAL={interval}")
    days = rule.daysOfTheWeek()
    if days:
        tokens = []
        for day in days:
            token = _EK_TO_RRULE_WEEKDAY.get(day.dayOfTheWeek(), "")
            week = day.weekNumber()
            tokens.append(f"{week}{token}" if week else token)
        parts.append("BYDAY=" + ",".join(tokens))
    end = rule.recurrenceEnd()
    if end is not None:
        if end.occurrenceCount():
            parts.append(f"COUNT={end.occurrenceCount()}")
        elif end.endDate() is not None:
            until = _nsdate_to_dt(end.endDate())
            if until is not None:
                parts.append("UNTIL=" + until.strftime("%Y%m%dT%H%M%SZ"))
    return ";".join(parts)


def _apply_recurrence(ek: Any, rrule: str | None) -> None:
    """Set the reminder's recurrence from an RRULE string (``None``/"" clears)."""
    if not rrule:
        ek.setRecurrenceRules_(None)
        return
    parsed = vRecur.from_ical(rrule)
    freq = _FREQ_TO_EK[str(parsed["FREQ"][0]).upper()]
    interval = int(parsed.get("INTERVAL", [1])[0])

    end = None
    if "COUNT" in parsed:
        end = EKRecurrenceEnd.recurrenceEndWithOccurrenceCount_(int(parsed["COUNT"][0]))
    elif "UNTIL" in parsed:
        until = _coerce_until(parsed["UNTIL"][0])
        if until is not None:
            end = EKRecurrenceEnd.recurrenceEndWithEndDate_(_dt_to_nsdate(until))

    days = None
    if "BYDAY" in parsed:
        days = [_byday_to_ek(token) for token in parsed["BYDAY"]]

    if days:
        rule = EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_daysOfTheWeek_daysOfTheMonth_monthsOfTheYear_weeksOfTheYear_daysOfTheYear_setPositions_end_(  # noqa: E501
            freq, interval, days, None, None, None, None, None, end
        )
    else:
        rule = EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_end_(
            freq, interval, end
        )
    ek.setRecurrenceRules_([rule])


def _coerce_until(value: Any) -> datetime | None:
    """Normalize an RRULE ``UNTIL`` (date or datetime) to an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if hasattr(value, "year"):  # datetime.date
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def _byday_to_ek(token: str) -> Any:
    """Convert an RRULE BYDAY token (``MO``, ``2MO``, ``-1FR``) to EventKit."""
    token = str(token)
    weekday = token[-2:]
    ordinal = token[:-2]
    week = int(ordinal) if ordinal else 0
    index = _RRULE_TO_EK_WEEKDAY[weekday]
    return EKRecurrenceDayOfWeek.dayOfWeek_weekNumber_(index, week)


# -- conversions: alarms ----------------------------------------------------


def _alarms_to_models(ek: Any) -> list[ReminderAlarm]:
    """Map ``EKReminder.alarms`` to :class:`ReminderAlarm`."""
    result: list[ReminderAlarm] = []
    for alarm in ek.alarms() or []:
        absolute = alarm.absoluteDate()
        if absolute is not None:
            result.append(ReminderAlarm(trigger=_nsdate_to_dt(absolute)))
        else:
            minutes = int(round(-alarm.relativeOffset() / 60))
            result.append(ReminderAlarm(minutes_before=minutes))
    return result


def _apply_alarms(ek: Any, alarms: list[ReminderAlarm]) -> None:
    """Replace the reminder's alarms with ``alarms`` (empty list clears them)."""
    new_alarms = []
    for alarm in alarms:
        if alarm.minutes_before is not None:
            new_alarms.append(EKAlarm.alarmWithRelativeOffset_(-alarm.minutes_before * 60))
        elif alarm.trigger is not None:
            trigger = alarm.trigger if alarm.trigger.tzinfo else alarm.trigger.replace(tzinfo=UTC)
            new_alarms.append(EKAlarm.alarmWithAbsoluteDate_(_dt_to_nsdate(trigger)))
    ek.setAlarms_(new_alarms or None)
