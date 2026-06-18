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
from datetime import UTC, date, datetime
from xml.etree import ElementTree as ET

import httpx
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

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
        body = (
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
        root = await self._propfind(home, body, depth="1")
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

        Args:
            calendar: Calendar display name.
            start: Inclusive lower bound of the time range.
            end: Exclusive upper bound of the time range.

        Returns:
            Events ordered by start time (ascending).
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
        events = self._parse_event_responses(root, cal)
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
    ) -> CalendarEvent:
        """Create a new event and return it as stored on the server.

        The event is written at ``{calendar.url}{uid}.ics`` with
        ``If-None-Match: *`` so an accidental collision never overwrites an
        existing resource.
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
    ) -> CalendarEvent:
        """Update fields of an existing event.

        Only the provided fields change; the rest keep their current values.
        The write uses ``If-Match`` with the current ETag for safe concurrency.
        """
        existing = await self.get_event(calendar, uid)
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
        ics = _build_vevent(
            uid=uid,
            summary=merged.summary,
            start=merged.start,
            end=merged.end,
            all_day=merged.all_day,
            location=merged.location,
            description=merged.description,
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

    # -- internal helpers --------------------------------------------------

    async def _find_event(self, calendar: str, uid: str) -> CalendarEvent | None:
        cal = await self._resolve_calendar(calendar)
        body = (
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
            '<c:filter><c:comp-filter name="VCALENDAR">'
            '<c:comp-filter name="VEVENT">'
            f'<c:prop-filter name="UID">'
            f'<c:text-match collation="i;octet">{_xml_escape(uid)}</c:text-match>'
            "</c:prop-filter>"
            "</c:comp-filter></c:comp-filter></c:filter>"
            "</c:calendar-query>"
        )
        root = await self._report(cal.url, body, depth="1")
        events = self._parse_event_responses(root, cal)
        return events[0] if events else None

    def _parse_event_responses(self, root: ET.Element, cal: Calendar) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for resp in root.findall("d:response", _NS):
            href = _text(resp.find("d:href", _NS))
            propstat = _ok_propstat(resp)
            if href is None or propstat is None:
                continue
            etag = _text(propstat.find("d:prop/d:getetag", _NS))
            data = _text(propstat.find("d:prop/c:calendar-data", _NS))
            if not data:
                continue
            event = _parse_ics(
                data, cal.name, str(httpx.URL(cal.url).join(href)), _strip_etag(etag)
            )
            if event is not None:
                events.append(event)
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


def _caldav_dt(value: datetime) -> str:
    """Format a datetime as a UTC CalDAV time-range bound (YYYYMMDDTHHMMSSZ)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _build_vevent(
    *,
    uid: str,
    summary: str,
    start: datetime,
    end: datetime,
    all_day: bool,
    location: str | None,
    description: str | None,
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
    cal.add_component(event)
    result: bytes = cal.to_ical()
    return result


def _ensure_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_ics(data: str, calendar_name: str, href: str, etag: str | None) -> CalendarEvent | None:
    """Parse the first VEVENT of an iCalendar document into a CalendarEvent."""
    try:
        cal = ICalendar.from_ical(data)
    except ValueError as exc:
        log.warning("VEVENT malformado ignorado (%s): %s", href, exc)
        return None
    for comp in cal.walk("VEVENT"):
        dtstart_prop = comp.get("dtstart")
        if dtstart_prop is None:
            continue
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
        )
    return None


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
