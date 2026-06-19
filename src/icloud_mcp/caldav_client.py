"""Async CalDAV client for iCloud Calendar.

CalDAV is stateless over HTTPS, so — unlike the IMAP pool — no persistent
connection pool is needed. A single :class:`httpx.AsyncClient` carries Basic
Auth (Apple ID + App-Specific Password) across requests, and the result of the
two-step service discovery (principal → calendar-home-set, landing on the
account's partition host) is cached for the process lifetime.

All public operations are ``async`` and raise the CalDAV exception hierarchy
from :mod:`icloud_mcp.exceptions` on failure.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import recurring_ical_events
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent
from icalendar.prop import vRecur

from icloud_mcp.config import ICloudMailSettings
from icloud_mcp.exceptions import (
    CalDAVAuthenticationError,
    CalDAVConnectionError,
    CalDAVError,
)
from icloud_mcp.models import Calendar, CalendarEvent

log = logging.getLogger(__name__)

# XML namespaces used by CalDAV multistatus responses.
NS_DAV = "DAV:"
NS_CALDAV = "urn:ietf:params:xml:ns:caldav"
NS_APPLE = "http://apple.com/ns/ical/"
_NS = {"d": NS_DAV, "c": NS_CALDAV, "a": NS_APPLE}

_PRODID = "-//icloud_mcp//iCloud MCP//EN"
_RETRY_DELAYS = (1.0, 2.0)  # seconds; two retries on transient transport errors

# PROPFIND body listing every collection under the home, with the properties
# needed to keep only event calendars (excluding task-only collections) and
# theme them.
_COLLECTIONS_PROPFIND = (
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
    'xmlns:a="http://apple.com/ns/ical/">'
    "<d:prop>"
    "<d:displayname/>"
    "<d:resourcetype/>"
    "<a:calendar-color/>"
    "<c:supported-calendar-component-set/>"
    "<d:current-user-privilege-set/>"
    "</d:prop>"
    "</d:propfind>"
)


class CalDAVClient:
    """High-level async client for iCloud Calendar (CalDAV).

    Args:
        settings: Application settings carrying iCloud credentials and the
            CalDAV bootstrap URL / timeout.
    """

    def __init__(self, settings: ICloudMailSettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            auth=httpx.BasicAuth(settings.icloud_email, settings.icloud_app_password),
            timeout=settings.caldav_timeout,
            headers={"User-Agent": "icloud-mcp/0.1"},
            follow_redirects=True,
        )
        self._calendar_home: str | None = None

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Run iCloud service discovery and cache the calendar-home-set URL.

        Raises:
            CalDAVAuthenticationError: On HTTP 401 (wrong/revoked password).
            CalDAVConnectionError: If discovery fails for any other reason.
        """
        if self._calendar_home is not None:
            return
        principal = await self._discover_principal()
        self._calendar_home = await self._discover_calendar_home(principal)
        log.info("CalDAV calendar-home-set descoberto: %s", self._calendar_home)

    async def close(self) -> None:
        """Close the underlying HTTP connection."""
        await self._client.aclose()

    # -- discovery ---------------------------------------------------------

    async def _discover_principal(self) -> str:
        """PROPFIND the bootstrap URL to find the current-user-principal href."""
        body = (
            '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
        )
        root = await self._propfind(self._settings.caldav_url, body, depth="0")
        href = _find_href(root, "d:current-user-principal")
        if href is None:
            raise CalDAVConnectionError(
                "Não foi possível localizar o principal do usuário no servidor CalDAV."
            )
        return str(httpx.URL(self._settings.caldav_url).join(href))

    async def _discover_calendar_home(self, principal_url: str) -> str:
        """PROPFIND the principal to find the calendar-home-set URL."""
        body = (
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><c:calendar-home-set/></d:prop>"
            "</d:propfind>"
        )
        root = await self._propfind(principal_url, body, depth="0")
        href = _find_href(root, "c:calendar-home-set")
        if href is None:
            raise CalDAVConnectionError(
                "Não foi possível localizar o calendar-home-set no servidor CalDAV."
            )
        return str(httpx.URL(principal_url).join(href))

    # -- calendars ---------------------------------------------------------

    async def list_calendars(self) -> list[Calendar]:
        """List all calendars that support events (``VEVENT``).

        Returns:
            One :class:`~icloud_mcp.models.Calendar` per writable/readable
            calendar collection in the account.
        """
        home = await self._require_home()
        root = await self._propfind(home, _COLLECTIONS_PROPFIND, depth="1")
        calendars: list[Calendar] = []
        for resp in root.findall("d:response", _NS):
            href = _text(resp.find("d:href", _NS))
            if href is None:
                continue
            propstat = _ok_propstat(resp)
            if propstat is None:
                continue
            resourcetype = propstat.find("d:prop/d:resourcetype", _NS)
            if resourcetype is None or resourcetype.find("c:calendar", _NS) is None:
                continue  # not a calendar collection (e.g. the home itself)
            if not _supports_vevent(propstat):
                continue
            name = _text(propstat.find("d:prop/d:displayname", _NS)) or href
            color = _text(propstat.find("d:prop/a:calendar-color", _NS))
            calendars.append(
                Calendar(
                    name=name,
                    url=str(httpx.URL(home).join(href)),
                    color=color[:7] if color else None,
                    read_only=_is_read_only(propstat),
                )
            )
        return calendars

    async def _resolve_calendar(self, calendar: str) -> Calendar:
        """Find a calendar by display name (case-insensitive)."""
        calendars = await self.list_calendars()
        for cal in calendars:
            if cal.name.lower() == calendar.lower():
                return cal
        available = ", ".join(c.name for c in calendars) or "(nenhum)"
        raise CalDAVError(f"Calendário '{calendar}' não encontrado. Disponíveis: {available}.")

    # -- events ------------------------------------------------------------

    async def list_events(
        self, calendar: str, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        """List events in a calendar overlapping the ``[start, end)`` window.

        Recurring events are expanded **client-side** into one
        :class:`~icloud_mcp.models.CalendarEvent` per occurrence within the
        window (each carries ``recurrence_id``). iCloud's server-side
        ``expand`` is unreliable, so it is never used.

        Args:
            calendar: Calendar display name.
            start: Inclusive lower bound of the time range.
            end: Exclusive upper bound of the time range.

        Returns:
            Events (and recurrence occurrences) ordered by start time.
        """
        cal = await self._resolve_calendar(calendar)
        body = (
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
            '<c:filter><c:comp-filter name="VCALENDAR">'
            '<c:comp-filter name="VEVENT">'
            f'<c:time-range start="{_caldav_dt(start)}" end="{_caldav_dt(end)}"/>'
            "</c:comp-filter></c:comp-filter></c:filter>"
            "</c:calendar-query>"
        )
        root = await self._report(cal.url, body, depth="1")
        events = self._parse_event_responses(root, cal, window=(start, end))
        events.sort(key=lambda e: e.start)
        return events

    async def get_event(self, calendar: str, uid: str) -> CalendarEvent:
        """Fetch a single event by its iCalendar UID.

        Uses a ``calendar-query`` REPORT filtered by UID instead of the
        RFC's ``get_object_by_uid`` flow, which is broken on iCloud.

        Raises:
            CalDAVError: If no event with the given UID exists.
        """
        event = await self._find_event(calendar, uid)
        if event is None:
            raise CalDAVError(f"Evento com UID '{uid}' não encontrado no calendário '{calendar}'.")
        return event

    async def create_event(
        self,
        calendar: str,
        summary: str,
        start: datetime,
        end: datetime,
        all_day: bool = False,
        location: str | None = None,
        description: str | None = None,
        rrule: str | None = None,
    ) -> CalendarEvent:
        """Create a new event and return it as stored on the server.

        The event is written at ``{calendar.url}{uid}.ics`` with
        ``If-None-Match: *`` so an accidental collision never overwrites an
        existing resource.

        Args:
            rrule: Optional recurrence rule (e.g. ``"FREQ=WEEKLY;BYDAY=MO"``)
                to make this a recurring series. Validated before the PUT.
        """
        cal = await self._resolve_calendar(calendar)
        uid = f"{uuid.uuid4()}"
        ics = _build_vevent(
            uid=uid,
            summary=summary,
            start=start,
            end=end,
            all_day=all_day,
            location=location,
            description=description,
            rrule=rrule,
        )
        href = str(httpx.URL(cal.url).join(f"{uid}.ics"))
        resp = await self._request(
            "PUT",
            href,
            content=ics,
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
        )
        etag = _strip_etag(resp.headers.get("ETag"))
        return CalendarEvent(
            uid=uid,
            calendar=cal.name,
            summary=summary,
            start=start,
            end=end,
            all_day=all_day,
            location=location,
            description=description,
            href=href,
            etag=etag,
            rrule=rrule,
            is_recurring=bool(rrule),
        )

    async def update_event(
        self,
        calendar: str,
        uid: str,
        summary: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        all_day: bool | None = None,
        location: str | None = None,
        description: str | None = None,
        rrule: str | None = None,
    ) -> CalendarEvent:
        """Update fields of an existing event (the whole series for recurring ones).

        Only the provided fields change; the rest keep their current values.
        The write uses ``If-Match`` with the current ETag for safe concurrency.

        Args:
            rrule: ``None`` keeps the current recurrence; an empty string ``""``
                removes recurrence (turns it into a one-off); any other value
                replaces the recurrence rule.
        """
        existing = await self.get_event(calendar, uid)
        if rrule is None:
            new_rrule = existing.rrule
        elif rrule == "":
            new_rrule = None
        else:
            new_rrule = rrule
        merged = existing.model_copy(
            update={
                k: v
                for k, v in {
                    "summary": summary,
                    "start": start,
                    "end": end,
                    "all_day": all_day,
                    "location": location,
                    "description": description,
                }.items()
                if v is not None
            }
        )
        merged.rrule = new_rrule
        merged.is_recurring = bool(new_rrule)
        ics = _build_vevent(
            uid=uid,
            summary=merged.summary,
            start=merged.start,
            end=merged.end,
            all_day=merged.all_day,
            location=merged.location,
            description=merged.description,
            rrule=new_rrule,
        )
        if existing.href is None:
            raise CalDAVError(f"Evento '{uid}' não possui href para atualização.")
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if existing.etag:
            headers["If-Match"] = existing.etag
        resp = await self._request("PUT", existing.href, content=ics, headers=headers)
        merged.etag = _strip_etag(resp.headers.get("ETag")) or existing.etag
        return merged

    async def delete_event(self, calendar: str, uid: str) -> dict[str, str]:
        """Delete an event by UID. Returns a status dict."""
        existing = await self.get_event(calendar, uid)
        if existing.href is None:
            raise CalDAVError(f"Evento '{uid}' não possui href para exclusão.")
        headers = {"If-Match": existing.etag} if existing.etag else {}
        await self._request("DELETE", existing.href, headers=headers)
        return {"status": "deleted", "uid": uid}

    async def update_occurrence(
        self,
        calendar: str,
        uid: str,
        recurrence_id: datetime,
        summary: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        location: str | None = None,
        description: str | None = None,
    ) -> CalendarEvent:
        """Edit a single occurrence of a recurring series.

        Adds (or updates) a ``RECURRENCE-ID`` override inside the same resource,
        leaving the master rule and the other occurrences untouched. The whole
        resource is PUT back with ``If-Match``.

        Args:
            recurrence_id: The original slot of the occurrence to edit, as
                returned in ``CalendarEvent.recurrence_id`` by ``list_events``.

        Raises:
            CalDAVError: If the series is not recurring or the slot does not exist.
        """
        cal = await self._resolve_calendar(calendar)
        cal_obj, href, etag = await self._fetch_resource(cal, uid)
        master = _require_recurring_master(cal_obj, uid)
        all_day = not isinstance(master.get("dtstart").dt, datetime)
        rid_value = _recurrence_id_value(master, recurrence_id)
        if not _slot_in_series(cal_obj, rid_value):
            raise CalDAVError(
                f"Ocorrência '{recurrence_id.isoformat()}' não existe na série '{uid}'."
            )
        override = _find_override(cal_obj, rid_value)
        if override is None:
            override = _new_override(master, rid_value)
            cal_obj.add_component(override)
        _apply_occurrence_fields(
            override,
            all_day=all_day,
            summary=summary,
            start=start,
            end=end,
            location=location,
            description=description,
        )
        new_etag = await self._put_resource(href, cal_obj, etag)
        event = _component_to_event(
            override,
            cal.name,
            href,
            new_etag,
            rrule=master.get("rrule").to_ical().decode(),
            is_recurring=True,
            recurrence_id=recurrence_id,
        )
        if event is None:  # override always has DTSTART; defensive for the type checker
            raise CalDAVError(f"Falha ao montar a ocorrência editada da série '{uid}'.")
        return event

    async def delete_occurrence(
        self, calendar: str, uid: str, recurrence_id: datetime
    ) -> dict[str, str]:
        """Delete a single occurrence of a recurring series via ``EXDATE``.

        Adds the slot to the master's ``EXDATE`` (and drops any override for
        that slot), then PUTs the whole resource back. The series and all other
        occurrences are preserved.
        """
        cal = await self._resolve_calendar(calendar)
        cal_obj, href, etag = await self._fetch_resource(cal, uid)
        master = _require_recurring_master(cal_obj, uid)
        rid_value = _recurrence_id_value(master, recurrence_id)
        if not _slot_in_series(cal_obj, rid_value):
            raise CalDAVError(
                f"Ocorrência '{recurrence_id.isoformat()}' não existe na série '{uid}'."
            )
        override = _find_override(cal_obj, rid_value)
        if override is not None:
            cal_obj.subcomponents.remove(override)
        master.add("exdate", rid_value)
        await self._put_resource(href, cal_obj, etag)
        return {
            "status": "deleted_occurrence",
            "uid": uid,
            "recurrence_id": recurrence_id.isoformat(),
        }

    async def _put_resource(self, href: str, cal_obj: Any, etag: str | None) -> str | None:
        """PUT a whole VCALENDAR resource back, with optimistic ``If-Match``."""
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        resp = await self._request("PUT", href, content=cal_obj.to_ical(), headers=headers)
        return _strip_etag(resp.headers.get("ETag")) or etag

    # -- internal helpers --------------------------------------------------

    async def _find_event(self, calendar: str, uid: str) -> CalendarEvent | None:
        cal = await self._resolve_calendar(calendar)
        root = await self._report(cal.url, _uid_query_body(uid), depth="1")
        events = self._parse_event_responses(root, cal, window=None)
        return events[0] if events else None

    async def _fetch_resource(self, cal: Calendar, uid: str) -> tuple[Any, str, str | None]:
        """Fetch the raw VCALENDAR resource (master + overrides) for a series.

        Returns ``(icalendar_calendar, full_href, etag)``. Unlike ``get_event``,
        this preserves the entire resource — needed to add overrides/EXDATE.

        Raises:
            CalDAVError: If no resource with the given UID exists.
        """
        root = await self._report(cal.url, _uid_query_body(uid), depth="1")
        for resp in root.findall("d:response", _NS):
            href = _text(resp.find("d:href", _NS))
            propstat = _ok_propstat(resp)
            if href is None or propstat is None:
                continue
            data = _text(propstat.find("d:prop/c:calendar-data", _NS))
            if not data:
                continue
            etag = _strip_etag(_text(propstat.find("d:prop/d:getetag", _NS)))
            ical = _safe_parse(data, href)
            if ical is None:
                continue
            return ical, str(httpx.URL(cal.url).join(href)), etag
        raise CalDAVError(f"Evento com UID '{uid}' não encontrado no calendário '{cal.name}'.")

    def _parse_event_responses(
        self,
        root: ET.Element,
        cal: Calendar,
        window: tuple[datetime, datetime] | None,
    ) -> list[CalendarEvent]:
        """Turn REPORT responses into events.

        With ``window`` set, recurring resources are expanded into their
        occurrences inside ``[window[0], window[1])``. With ``window`` ``None``,
        each resource yields its master event (``RRULE`` preserved, not expanded).
        """
        events: list[CalendarEvent] = []
        for resp in root.findall("d:response", _NS):
            href = _text(resp.find("d:href", _NS))
            propstat = _ok_propstat(resp)
            if href is None or propstat is None:
                continue
            etag = _strip_etag(_text(propstat.find("d:prop/d:getetag", _NS)))
            data = _text(propstat.find("d:prop/c:calendar-data", _NS))
            if not data:
                continue
            full_href = str(httpx.URL(cal.url).join(href))
            if window is None:
                master = _parse_ics_master(data, cal.name, full_href, etag)
                if master is not None:
                    events.append(master)
            else:
                events.extend(_expand_ics(data, cal.name, full_href, etag, window))
        return events

    async def _require_home(self) -> str:
        if self._calendar_home is None:
            await self.connect()
        assert self._calendar_home is not None  # noqa: S101 — set by connect()
        return self._calendar_home

    # -- HTTP plumbing -----------------------------------------------------

    async def _propfind(self, url: str, body: str, depth: str) -> ET.Element:
        resp = await self._request(
            "PROPFIND",
            url,
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": depth},
        )
        return _parse_multistatus(resp)

    async def _report(self, url: str, body: str, depth: str) -> ET.Element:
        resp = await self._request(
            "REPORT",
            url,
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": depth},
        )
        return _parse_multistatus(resp)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Issue a CalDAV request with simple retry on transient failures.

        Retries (1s, 2s) on transport errors and HTTP 5xx. HTTP 401 raises
        :class:`CalDAVAuthenticationError` immediately; other 4xx raise
        :class:`CalDAVError`.
        """
        last_exc: Exception | None = None
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                resp = await self._client.request(method, url, content=content, headers=headers)
            except httpx.TransportError as exc:
                last_exc = exc
                log.warning("Erro de transporte CalDAV (%s %s): %s", method, url, exc)
            else:
                if resp.status_code == 401:
                    raise CalDAVAuthenticationError(
                        "Credenciais CalDAV inválidas. Use uma App-Specific Password "
                        "(a senha normal do Apple ID não funciona)."
                    )
                if resp.status_code < 500:
                    if resp.status_code >= 400:
                        raise CalDAVError(
                            f"Servidor CalDAV retornou {resp.status_code} para "
                            f"{method} {url}: {resp.text[:200]}"
                        )
                    return resp
                last_exc = CalDAVConnectionError(
                    f"Servidor CalDAV retornou {resp.status_code} para {method} {url}."
                )
            if attempt < len(_RETRY_DELAYS):
                await asyncio.sleep(_RETRY_DELAYS[attempt])
        raise CalDAVConnectionError(
            f"Falha na requisição CalDAV {method} {url} após {len(_RETRY_DELAYS) + 1} tentativas."
        ) from last_exc


# -- module-level parsing/building helpers ---------------------------------


def _parse_multistatus(resp: httpx.Response) -> ET.Element:
    """Parse a 207 Multi-Status XML body into its root element."""
    try:
        return ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise CalDAVError(f"Resposta XML inválida do servidor CalDAV: {exc}") from exc


def _ok_propstat(response: ET.Element) -> ET.Element | None:
    """Return the ``propstat`` whose status is 2xx, if any."""
    for propstat in response.findall("d:propstat", _NS):
        status = _text(propstat.find("d:status", _NS)) or ""
        if " 200 " in status or status.endswith(" 200 OK"):
            return propstat
    # Some servers omit status granularity; fall back to the first propstat.
    return response.find("d:propstat", _NS)


def _find_href(root: ET.Element, prop_path: str) -> str | None:
    """Find the first ``href`` nested under ``prop/<prop_path>``."""
    el = root.find(f"d:response/d:propstat/d:prop/{prop_path}/d:href", _NS)
    return _text(el)


def _supports_vevent(propstat: ET.Element) -> bool:
    comp_set = propstat.find("d:prop/c:supported-calendar-component-set", _NS)
    if comp_set is None:
        return True  # property absent → assume general-purpose calendar
    return any(comp.get("name") == "VEVENT" for comp in comp_set.findall("c:comp", _NS))


def _is_read_only(propstat: ET.Element) -> bool:
    priv_set = propstat.find("d:prop/d:current-user-privilege-set", _NS)
    if priv_set is None:
        return False
    privileges = priv_set.findall("d:privilege", _NS)
    if not privileges:
        return False
    return not any(p.find("d:write", _NS) is not None for p in privileges)


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def _strip_etag(etag: str | None) -> str | None:
    if etag is None:
        return None
    return etag.strip().strip('"') or None


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _uid_query_body(uid: str, comp: str = "VEVENT") -> str:
    """Build a calendar-query REPORT body filtering ``comp`` components by UID."""
    return (
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
        '<c:filter><c:comp-filter name="VCALENDAR">'
        f'<c:comp-filter name="{comp}">'
        f'<c:prop-filter name="UID">'
        f'<c:text-match collation="i;octet">{_xml_escape(uid)}</c:text-match>'
        "</c:prop-filter>"
        "</c:comp-filter></c:comp-filter></c:filter>"
        "</c:calendar-query>"
    )


def _caldav_dt(value: datetime) -> str:
    """Format a datetime as a UTC CalDAV time-range bound (YYYYMMDDTHHMMSSZ)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _build_rrule(rrule: str) -> Any:
    """Validate and parse a raw RRULE string into an icalendar vRecur.

    Args:
        rrule: A recurrence rule, with or without the ``RRULE:`` prefix
            (e.g. ``"FREQ=WEEKLY;BYDAY=MO"``).

    Raises:
        CalDAVError: If the rule cannot be parsed.
    """
    cleaned = rrule.strip()
    if cleaned.upper().startswith("RRULE:"):
        cleaned = cleaned[len("RRULE:") :]
    try:
        recur = vRecur.from_ical(cleaned)
    except (ValueError, KeyError) as exc:
        raise CalDAVError(
            f"RRULE inválida: '{rrule}'. Use sintaxe iCalendar, ex.: 'FREQ=WEEKLY;BYDAY=MO'."
        ) from exc
    if "FREQ" not in recur:
        raise CalDAVError(f"RRULE inválida: '{rrule}'. A regra precisa conter 'FREQ'.")
    return recur


def _build_vevent(
    *,
    uid: str,
    summary: str,
    start: datetime,
    end: datetime,
    all_day: bool,
    location: str | None,
    description: str | None,
    rrule: str | None = None,
) -> bytes:
    """Serialize a single-event VCALENDAR document to iCalendar bytes."""
    cal = ICalendar()
    cal.add("prodid", _PRODID)
    cal.add("version", "2.0")
    event = IEvent()
    event.add("uid", uid)
    event.add("summary", summary)
    event.add("dtstamp", datetime.now(UTC))
    if all_day:
        event.add("dtstart", start.date())
        event.add("dtend", end.date())
    else:
        event.add("dtstart", _ensure_aware(start))
        event.add("dtend", _ensure_aware(end))
    if location:
        event.add("location", location)
    if description:
        event.add("description", description)
    if rrule:
        event.add("rrule", _build_rrule(rrule))
    cal.add_component(event)
    result: bytes = cal.to_ical()
    return result


def _ensure_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# -- single-occurrence editing helpers -------------------------------------


def _require_recurring_master(cal_obj: Any, uid: str) -> Any:
    """Return the series master VEVENT, or raise if not a recurring series."""
    master = _master_component(cal_obj)
    if master is None or master.get("rrule") is None:
        raise CalDAVError(f"Evento '{uid}' não é uma série recorrente.")
    return master


def _as_dt(value: date | datetime) -> datetime:
    """Normalize a date or datetime to an aware datetime (for comparison)."""
    if isinstance(value, datetime):
        return _ensure_aware(value)
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _same_moment(a: date | datetime, b: date | datetime) -> bool:
    """Whether two RECURRENCE-ID values point at the same instant."""
    return _as_dt(a) == _as_dt(b)


def _recurrence_id_value(master: Any, recurrence_id: datetime) -> date | datetime:
    """Type the RECURRENCE-ID/EXDATE value to match the master's DTSTART kind."""
    dtstart = master.get("dtstart").dt
    if isinstance(dtstart, datetime):
        return _ensure_aware(recurrence_id)
    return recurrence_id.date()


def _master_duration(master: Any) -> timedelta:
    """Duration of the master event (used to default a new override's end)."""
    dtstart = master.get("dtstart").dt
    dtend_prop = master.get("dtend")
    if dtend_prop is None:
        return timedelta(0)
    return _as_dt(dtend_prop.dt) - _as_dt(dtstart)


def _find_override(cal_obj: Any, rid_value: date | datetime) -> Any:
    """Find an existing override VEVENT for the given recurrence slot."""
    for comp in cal_obj.walk("VEVENT"):
        rid = comp.get("recurrence-id")
        if rid is not None and _same_moment(rid.dt, rid_value):
            return comp
    return None


def _slot_in_series(cal_obj: Any, rid_value: date | datetime) -> bool:
    """Validate that ``rid_value`` is a real occurrence of the series."""
    anchor = _as_dt(rid_value)
    try:
        occurrences = recurring_ical_events.of(cal_obj).between(
            anchor - timedelta(days=2), anchor + timedelta(days=2)
        )
    except Exception:  # noqa: BLE001 — never block a write on an expansion hiccup
        return True
    for occ in occurrences:
        rid = occ.get("recurrence-id")
        if rid is not None and _same_moment(rid.dt, rid_value):
            return True
    return False


def _new_override(master: Any, rid_value: date | datetime) -> Any:
    """Build a fresh override VEVENT seeded from the master at ``rid_value``."""
    duration = _master_duration(master)
    override = IEvent()
    override.add("uid", master.get("uid"))
    override.add("recurrence-id", rid_value)
    override.add("dtstamp", datetime.now(UTC))
    override.add("summary", str(master.get("summary", "")))
    override.add("dtstart", rid_value)
    override.add("dtend", rid_value + duration)
    location = master.get("location")
    description = master.get("description")
    if location:
        override.add("location", str(location))
    if description:
        override.add("description", str(description))
    return override


def _set_prop(comp: Any, name: str, value: Any) -> None:
    """Replace a single property on a component (icalendar dict is caseless)."""
    if name in comp:
        del comp[name]
    if value is not None:
        comp.add(name, value)


def _apply_occurrence_fields(
    override: Any,
    *,
    all_day: bool,
    summary: str | None,
    start: datetime | None,
    end: datetime | None,
    location: str | None,
    description: str | None,
) -> None:
    """Apply the provided field edits onto an override VEVENT in place."""
    if summary is not None:
        _set_prop(override, "summary", summary)
    if location is not None:
        _set_prop(override, "location", location or None)
    if description is not None:
        _set_prop(override, "description", description or None)
    if start is None and end is None:
        return
    duration = _master_duration(override)
    if start is not None:
        new_start: date | datetime = start.date() if all_day else _ensure_aware(start)
        _set_prop(override, "dtstart", new_start)
        if end is None:
            _set_prop(override, "dtend", new_start + duration)
    if end is not None:
        new_end: date | datetime = end.date() if all_day else _ensure_aware(end)
        _set_prop(override, "dtend", new_end)


def _safe_parse(data: str, href: str) -> Any:
    """Parse an iCalendar document, logging and skipping malformed input."""
    try:
        return ICalendar.from_ical(data)
    except ValueError as exc:
        log.warning("VEVENT malformado ignorado (%s): %s", href, exc)
        return None


def _recurrence_info(cal: Any) -> tuple[bool, str | None]:
    """Inspect a VCALENDAR for recurrence: ``(is_recurring, rrule_string)``.

    ``rrule_string`` comes from the master VEVENT (the one carrying ``RRULE``);
    an ``RDATE``-only series is recurring but has no ``RRULE`` string.
    """
    recurring = False
    for comp in cal.walk("VEVENT"):
        rrule_prop = comp.get("rrule")
        if rrule_prop is not None:
            decoded: str = rrule_prop.to_ical().decode()
            return True, decoded
        if comp.get("rdate") is not None:
            recurring = True
    return recurring, None


def _master_component(cal: Any) -> Any:
    """Return the series master (VEVENT with RRULE) or the first VEVENT."""
    first: Any = None
    for comp in cal.walk("VEVENT"):
        if first is None:
            first = comp
        if comp.get("rrule") is not None:
            return comp
    return first


def _component_to_event(
    comp: Any,
    calendar_name: str,
    href: str,
    etag: str | None,
    *,
    rrule: str | None,
    is_recurring: bool,
    recurrence_id: datetime | None = None,
) -> CalendarEvent | None:
    """Build a CalendarEvent from an icalendar VEVENT component."""
    dtstart_prop = comp.get("dtstart")
    if dtstart_prop is None:
        return None
    start, all_day = _coerce_dt(dtstart_prop.dt)
    dtend_prop = comp.get("dtend")
    end = _coerce_dt(dtend_prop.dt)[0] if dtend_prop is not None else start
    return CalendarEvent(
        uid=str(comp.get("uid", "")),
        calendar=calendar_name,
        summary=str(comp.get("summary", "")),
        start=start,
        end=end,
        all_day=all_day,
        location=_opt_str(comp.get("location")),
        description=_opt_str(comp.get("description")),
        href=href,
        etag=etag,
        rrule=rrule,
        is_recurring=is_recurring,
        recurrence_id=recurrence_id,
    )


def _parse_ics_master(
    data: str, calendar_name: str, href: str, etag: str | None
) -> CalendarEvent | None:
    """Parse the master event of a resource, preserving its RRULE (no expansion)."""
    cal = _safe_parse(data, href)
    if cal is None:
        return None
    is_recurring, rrule = _recurrence_info(cal)
    master = _master_component(cal)
    if master is None:
        return None
    return _component_to_event(
        master, calendar_name, href, etag, rrule=rrule, is_recurring=is_recurring
    )


def _expand_ics(
    data: str,
    calendar_name: str,
    href: str,
    etag: str | None,
    window: tuple[datetime, datetime],
) -> list[CalendarEvent]:
    """Expand a resource into its occurrences within ``window``.

    Non-recurring events pass through as a single occurrence. On expansion
    failure (exotic rules), falls back to the unexpanded master.
    """
    cal = _safe_parse(data, href)
    if cal is None:
        return []
    is_recurring, rrule = _recurrence_info(cal)
    win_start = _ensure_aware(window[0])
    win_end = _ensure_aware(window[1])
    try:
        occurrences = recurring_ical_events.of(cal).between(win_start, win_end)
    except Exception as exc:  # noqa: BLE001 — exotic RRULEs can raise; degrade gracefully
        log.warning("Falha ao expandir recorrência (%s): %s", href, exc)
        master = _parse_ics_master(data, calendar_name, href, etag)
        return [master] if master is not None else []
    events: list[CalendarEvent] = []
    for occ in occurrences:
        recurrence_id: datetime | None = None
        if is_recurring:
            rid_prop = occ.get("recurrence-id")
            if rid_prop is not None:
                recurrence_id = _coerce_dt(rid_prop.dt)[0]
        event = _component_to_event(
            occ,
            calendar_name,
            href,
            etag,
            rrule=rrule,
            is_recurring=is_recurring,
            recurrence_id=recurrence_id,
        )
        if event is not None:
            events.append(event)
    return events


def _coerce_dt(value: datetime | date) -> tuple[datetime, bool]:
    """Normalize an icalendar DTSTART/DTEND value to an aware datetime.

    Returns ``(datetime, all_day)`` — a bare ``date`` denotes an all-day event.
    """
    if isinstance(value, datetime):
        return _ensure_aware(value), False
    return datetime(value.year, value.month, value.day, tzinfo=UTC), True


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
