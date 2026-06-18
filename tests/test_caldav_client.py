"""Tests for caldav_client.py — async iCloud CalDAV client over httpx.

All HTTP traffic is intercepted with httpx.MockTransport; no real network.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from icloud_mcp.caldav_client import (
    CalDAVClient,
    _apply_recurring_due,
    _next_occurrence,
)
from icloud_mcp.config import ICloudMailSettings
from icloud_mcp.exceptions import (
    CalDAVAuthenticationError,
    CalDAVConnectionError,
    CalDAVError,
)
from icloud_mcp.models import Reminder

# -- canned multistatus XML bodies -----------------------------------------

PRINCIPAL_XML = b"""<?xml version="1.0"?>
<multistatus xmlns="DAV:">
  <response>
    <href>/</href>
    <propstat>
      <prop><current-user-principal><href>/123456/principal/</href></current-user-principal></prop>
      <status>HTTP/1.1 200 OK</status>
    </propstat>
  </response>
</multistatus>"""

HOME_XML = b"""<?xml version="1.0"?>
<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <response>
    <href>/123456/principal/</href>
    <propstat>
      <prop><c:calendar-home-set>
        <href>https://p99-caldav.icloud.com/123456/calendars/</href>
      </c:calendar-home-set></prop>
      <status>HTTP/1.1 200 OK</status>
    </propstat>
  </response>
</multistatus>"""

CALENDARS_XML = b"""<?xml version="1.0"?>
<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
             xmlns:a="http://apple.com/ns/ical/">
  <response>
    <href>/123456/calendars/</href>
    <propstat>
      <prop><resourcetype><collection/></resourcetype></prop>
      <status>HTTP/1.1 200 OK</status>
    </propstat>
  </response>
  <response>
    <href>/123456/calendars/work/</href>
    <propstat>
      <prop>
        <displayname>Work</displayname>
        <resourcetype><collection/><c:calendar/></resourcetype>
        <a:calendar-color>#FF0000FF</a:calendar-color>
        <c:supported-calendar-component-set>
          <c:comp name="VEVENT"/></c:supported-calendar-component-set>
        <current-user-privilege-set>
          <privilege><read/></privilege><privilege><write/></privilege>
        </current-user-privilege-set>
      </prop>
      <status>HTTP/1.1 200 OK</status>
    </propstat>
  </response>
  <response>
    <href>/123456/calendars/holidays/</href>
    <propstat>
      <prop>
        <displayname>Holidays</displayname>
        <resourcetype><collection/><c:calendar/></resourcetype>
        <c:supported-calendar-component-set>
          <c:comp name="VEVENT"/></c:supported-calendar-component-set>
        <current-user-privilege-set><privilege><read/></privilege></current-user-privilege-set>
      </prop>
      <status>HTTP/1.1 200 OK</status>
    </propstat>
  </response>
  <response>
    <href>/123456/calendars/reminders/</href>
    <propstat>
      <prop>
        <displayname>Tasks</displayname>
        <resourcetype><collection/><c:calendar/></resourcetype>
        <c:supported-calendar-component-set>
          <c:comp name="VTODO"/></c:supported-calendar-component-set>
      </prop>
      <status>HTTP/1.1 200 OK</status>
    </propstat>
  </response>
</multistatus>"""


def _vevent_response(uid: str, summary: str, dtstart: str, dtend: str, etag: str) -> str:
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DTSTART:{dtstart}\r\n"
        f"DTEND:{dtend}\r\n"
        "LOCATION:Room 1\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    return (
        f"  <response>\n"
        f"    <href>/123456/calendars/work/{uid}.ics</href>\n"
        f"    <propstat><prop>\n"
        f'      <getetag>"{etag}"</getetag>\n'
        f"      <c:calendar-data>{ics}</c:calendar-data>\n"
        f"    </prop><status>HTTP/1.1 200 OK</status></propstat>\n"
        f"  </response>\n"
    )


def _events_multistatus(*responses: str) -> bytes:
    inner = "".join(responses)
    return (
        '<?xml version="1.0"?>\n'
        '<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">\n'
        f"{inner}"
        "</multistatus>"
    ).encode()


Handler = Callable[[httpx.Request], httpx.Response]


def _settings() -> ICloudMailSettings:
    return ICloudMailSettings(
        icloud_email="user@icloud.com",
        icloud_app_password="abcd-efgh-ijkl-mnop",
    )


def _make_client(handler: Handler) -> CalDAVClient:
    """Build a CalDAVClient whose HTTP layer is a MockTransport."""
    client = CalDAVClient(_settings())
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    return client


def _discovery(request: httpx.Request) -> httpx.Response | None:
    """Handle the two discovery PROPFINDs; return None if not a discovery call."""
    if request.method == "PROPFIND" and request.url.path == "/":
        return httpx.Response(207, content=PRINCIPAL_XML)
    if request.method == "PROPFIND" and request.url.path.endswith("/principal/"):
        return httpx.Response(207, content=HOME_XML)
    if request.method == "PROPFIND" and request.url.path.endswith("/calendars/"):
        return httpx.Response(207, content=CALENDARS_XML)
    return None


def _default_handler(request: httpx.Request) -> httpx.Response:
    disc = _discovery(request)
    if disc is not None:
        return disc
    if request.method == "REPORT":
        body = request.content.decode("utf-8")
        if "time-range" in body:
            return httpx.Response(
                207,
                content=_events_multistatus(
                    _vevent_response(
                        "evt-2", "Review", "20260601T140000Z", "20260601T150000Z", "e2"
                    ),
                    _vevent_response(
                        "evt-1", "Standup", "20260601T090000Z", "20260601T093000Z", "e1"
                    ),
                ),
            )
        # UID-filtered query (get/update/delete)
        match = re.search(r"<c:text-match[^>]*>([^<]+)</c:text-match>", body)
        uid = match.group(1) if match else ""
        if uid == "missing-uid":
            return httpx.Response(207, content=_events_multistatus())
        return httpx.Response(
            207,
            content=_events_multistatus(
                _vevent_response(
                    uid, "Existing", "20260601T090000Z", "20260601T100000Z", "etag-old"
                )
            ),
        )
    if request.method == "PUT":
        return httpx.Response(201, headers={"ETag": '"etag-new"'})
    if request.method == "DELETE":
        return httpx.Response(204)
    return httpx.Response(500, text="unexpected")


# -- discovery -------------------------------------------------------------


async def test_connect_discovers_calendar_home() -> None:
    client = _make_client(_default_handler)
    await client.connect()
    assert client._calendar_home == "https://p99-caldav.icloud.com/123456/calendars/"
    await client.close()


async def test_connect_is_idempotent() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            calls["n"] += 1
        return _default_handler(request)

    client = _make_client(handler)
    await client.connect()
    await client.connect()
    assert calls["n"] == 1
    await client.close()


# -- calendars -------------------------------------------------------------


async def test_list_calendars_filters_and_parses() -> None:
    client = _make_client(_default_handler)
    calendars = await client.list_calendars()
    names = {c.name for c in calendars}
    # 'Tasks' (VTODO only) and the home collection are excluded.
    assert names == {"Work", "Holidays"}
    work = next(c for c in calendars if c.name == "Work")
    assert work.url == "https://p99-caldav.icloud.com/123456/calendars/work/"
    assert work.color == "#FF0000"
    assert work.read_only is False
    holidays = next(c for c in calendars if c.name == "Holidays")
    assert holidays.read_only is True
    await client.close()


async def test_resolve_unknown_calendar_raises() -> None:
    client = _make_client(_default_handler)
    with pytest.raises(CalDAVError, match="não encontrado"):
        await client.list_events(
            "Nonexistent",
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 6, 2, tzinfo=UTC),
        )
    await client.close()


# -- events: read ----------------------------------------------------------


async def test_list_events_parses_and_sorts() -> None:
    client = _make_client(_default_handler)
    events = await client.list_events(
        "Work", datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 2, tzinfo=UTC)
    )
    assert [e.uid for e in events] == ["evt-1", "evt-2"]  # sorted by start
    first = events[0]
    assert first.summary == "Standup"
    assert first.start == datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    assert first.location == "Room 1"
    assert first.etag == "etag-old" or first.etag == "e1"
    await client.close()


async def test_get_event_found() -> None:
    client = _make_client(_default_handler)
    event = await client.get_event("Work", "evt-xyz")
    assert event.uid == "evt-xyz"
    assert event.href == "https://p99-caldav.icloud.com/123456/calendars/work/evt-xyz.ics"
    await client.close()


async def test_get_event_missing_raises() -> None:
    client = _make_client(_default_handler)
    with pytest.raises(CalDAVError, match="não encontrado"):
        await client.get_event("Work", "missing-uid")
    await client.close()


# -- events: write ---------------------------------------------------------


async def test_create_event_puts_ics() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            captured["url"] = str(request.url)
            captured["body"] = request.content
            captured["if_none_match"] = request.headers.get("If-None-Match")
        return _default_handler(request)

    client = _make_client(handler)
    event = await client.create_event(
        "Work",
        summary="Lunch",
        start=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        end=datetime(2026, 6, 1, 13, 0, tzinfo=UTC),
        location="Cafe",
    )
    assert event.summary == "Lunch"
    assert event.etag == "etag-new"
    assert event.href is not None and event.href.endswith(".ics")
    assert captured["if_none_match"] == "*"
    assert b"SUMMARY:Lunch" in captured["body"]  # type: ignore[operator]
    assert b"LOCATION:Cafe" in captured["body"]  # type: ignore[operator]
    await client.close()


async def test_create_all_day_event_uses_date() -> None:
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            captured["body"] = request.content
        return _default_handler(request)

    client = _make_client(handler)
    event = await client.create_event(
        "Work",
        summary="Holiday",
        start=datetime(2026, 6, 1, tzinfo=UTC),
        end=datetime(2026, 6, 2, tzinfo=UTC),
        all_day=True,
    )
    assert event.all_day is True
    # All-day events serialize DTSTART as a VALUE=DATE (no time component).
    assert b"DTSTART;VALUE=DATE:20260601" in captured["body"]
    await client.close()


async def test_update_event_merges_fields_and_sends_if_match() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            captured["body"] = request.content
            captured["if_match"] = request.headers.get("If-Match")
        return _default_handler(request)

    client = _make_client(handler)
    event = await client.update_event("Work", "evt-7", summary="Renamed")
    assert event.summary == "Renamed"
    assert event.etag == "etag-new"
    assert captured["if_match"] == "etag-old"
    assert b"SUMMARY:Renamed" in captured["body"]  # type: ignore[operator]
    await client.close()


async def test_delete_event() -> None:
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            captured["url"] = str(request.url)
            captured["if_match"] = request.headers.get("If-Match")
        return _default_handler(request)

    client = _make_client(handler)
    result = await client.delete_event("Work", "evt-9")
    assert result == {"status": "deleted", "uid": "evt-9"}
    assert captured["if_match"] == "etag-old"
    assert captured["url"] == "https://p99-caldav.icloud.com/123456/calendars/work/evt-9.ics"
    await client.close()


# -- error handling --------------------------------------------------------


async def test_401_raises_authentication_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    client = _make_client(handler)
    with pytest.raises(CalDAVAuthenticationError, match="App-Specific Password"):
        await client.connect()
    await client.close()


async def test_4xx_raises_caldav_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    client = _make_client(handler)
    with pytest.raises(CalDAVError, match="403"):
        await client.connect()
    await client.close()


async def test_retry_on_5xx_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("icloud_mcp.caldav_client._RETRY_DELAYS", (0.0, 0.0))
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(503, text="busy")
        return _default_handler(request)

    client = _make_client(handler)
    await client.connect()  # should succeed on the 2nd attempt
    assert state["n"] == 2
    await client.close()


async def test_retry_exhausted_raises_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("icloud_mcp.caldav_client._RETRY_DELAYS", (0.0, 0.0))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    client = _make_client(handler)
    with pytest.raises(CalDAVConnectionError, match="após 3 tentativas"):
        await client.connect()
    await client.close()


async def test_transport_error_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("icloud_mcp.caldav_client._RETRY_DELAYS", (0.0, 0.0))
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            state["n"] += 1
            if state["n"] == 1:
                raise httpx.ConnectError("boom")
        return _default_handler(request)

    client = _make_client(handler)
    await client.connect()
    assert state["n"] == 2
    await client.close()


# -- recurrence: fixtures & helpers ----------------------------------------


def _raw_event_response(ics: str, name: str = "rec", etag: str = "etag-old") -> str:
    """Wrap an arbitrary iCalendar document as a single REPORT response."""
    return (
        f"  <response>\n"
        f"    <href>/123456/calendars/work/{name}.ics</href>\n"
        f"    <propstat><prop>\n"
        f'      <getetag>"{etag}"</getetag>\n'
        f"      <c:calendar-data>{ics}</c:calendar-data>\n"
        f"    </prop><status>HTTP/1.1 200 OK</status></propstat>\n"
        f"  </response>\n"
    )


def _ics(*vevents: str, prodid: str = "-//t//EN") -> str:
    body = "".join(vevents)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:{prodid}\r\n{body}END:VCALENDAR\r\n"


# A weekly Monday standup starting 2026-06-01 (June 2026 Mondays: 1, 8, 15, 22, 29).
WEEKLY_VEVENT = (
    "BEGIN:VEVENT\r\nUID:weekly-1\r\nSUMMARY:Standup\r\n"
    "DTSTART:20260601T090000Z\r\nDTEND:20260601T093000Z\r\n"
    "RRULE:FREQ=WEEKLY;BYDAY=MO\r\nEND:VEVENT\r\n"
)


def _recurring_handler(ics: str) -> Handler:
    """Handler that serves ``ics`` for any REPORT (time-range or UID query)."""

    def handler(request: httpx.Request) -> httpx.Response:
        disc = _discovery(request)
        if disc is not None:
            return disc
        if request.method == "REPORT":
            return httpx.Response(207, content=_events_multistatus(_raw_event_response(ics)))
        if request.method == "PUT":
            return httpx.Response(201, headers={"ETag": '"etag-new"'})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500, text="unexpected")

    return handler


# -- recurrence: reading (client-side expansion) ---------------------------


async def test_list_events_expands_recurring_series() -> None:
    client = _make_client(_recurring_handler(_ics(WEEKLY_VEVENT)))
    events = await client.list_events(
        "Work", datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 30, tzinfo=UTC)
    )
    # Five Mondays in June 2026, each a distinct occurrence of the same series.
    assert len(events) == 5
    assert {e.uid for e in events} == {"weekly-1"}
    assert all(e.is_recurring for e in events)
    assert all(e.rrule == "FREQ=WEEKLY;BYDAY=MO" for e in events)
    assert [e.start.day for e in events] == [1, 8, 15, 22, 29]
    assert all(e.recurrence_id is not None for e in events)
    await client.close()


async def test_list_events_respects_exdate() -> None:
    vevent = (
        "BEGIN:VEVENT\r\nUID:weekly-1\r\nSUMMARY:Standup\r\n"
        "DTSTART:20260601T090000Z\r\nDTEND:20260601T093000Z\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=MO\r\nEXDATE:20260608T090000Z\r\nEND:VEVENT\r\n"
    )
    client = _make_client(_recurring_handler(_ics(vevent)))
    events = await client.list_events(
        "Work", datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 30, tzinfo=UTC)
    )
    # The June 8th occurrence is excluded via EXDATE.
    assert [e.start.day for e in events] == [1, 15, 22, 29]
    await client.close()


async def test_list_events_applies_recurrence_override() -> None:
    master = (
        "BEGIN:VEVENT\r\nUID:weekly-1\r\nSUMMARY:Standup\r\n"
        "DTSTART:20260601T090000Z\r\nDTEND:20260601T093000Z\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=MO\r\nEND:VEVENT\r\n"
    )
    override = (
        "BEGIN:VEVENT\r\nUID:weekly-1\r\nSUMMARY:Standup (moved room)\r\n"
        "RECURRENCE-ID:20260608T090000Z\r\n"
        "DTSTART:20260608T100000Z\r\nDTEND:20260608T103000Z\r\nEND:VEVENT\r\n"
    )
    client = _make_client(_recurring_handler(_ics(master, override)))
    events = await client.list_events(
        "Work", datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 30, tzinfo=UTC)
    )
    moved = next(e for e in events if e.start.day == 8)
    assert moved.summary == "Standup (moved room)"
    assert moved.start.hour == 10
    await client.close()


async def test_get_event_preserves_rrule_without_expanding() -> None:
    client = _make_client(_recurring_handler(_ics(WEEKLY_VEVENT)))
    event = await client.get_event("Work", "weekly-1")
    # get_event returns the series master, not an expanded occurrence.
    assert event.is_recurring is True
    assert event.rrule == "FREQ=WEEKLY;BYDAY=MO"
    assert event.recurrence_id is None
    assert event.start == datetime(2026, 6, 1, 9, tzinfo=UTC)
    await client.close()


async def test_non_recurring_event_has_no_recurrence_fields() -> None:
    client = _make_client(_default_handler)
    events = await client.list_events(
        "Work", datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 2, tzinfo=UTC)
    )
    assert events  # default handler returns two simple events
    assert all(e.is_recurring is False for e in events)
    assert all(e.rrule is None for e in events)
    assert all(e.recurrence_id is None for e in events)
    await client.close()


# -- recurrence: writing ---------------------------------------------------


async def test_create_recurring_event_puts_rrule() -> None:
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            captured["body"] = request.content
        return _default_handler(request)

    client = _make_client(handler)
    event = await client.create_event(
        "Work",
        summary="Standup",
        start=datetime(2026, 6, 1, 9, tzinfo=UTC),
        end=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
        rrule="FREQ=WEEKLY;BYDAY=MO",
    )
    assert event.is_recurring is True
    assert event.rrule == "FREQ=WEEKLY;BYDAY=MO"
    assert b"RRULE:FREQ=WEEKLY;BYDAY=MO" in captured["body"]
    await client.close()


async def test_create_event_invalid_rrule_raises_before_put() -> None:
    put_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            put_calls["n"] += 1
        return _default_handler(request)

    client = _make_client(handler)
    with pytest.raises(CalDAVError, match="RRULE inválida"):
        await client.create_event(
            "Work",
            summary="Bad",
            start=datetime(2026, 6, 1, 9, tzinfo=UTC),
            end=datetime(2026, 6, 1, 10, tzinfo=UTC),
            rrule="this is not a rule",
        )
    assert put_calls["n"] == 0  # no write attempted
    await client.close()


async def test_update_event_removes_recurrence_with_empty_rrule() -> None:
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            captured["body"] = request.content
        if request.method == "REPORT":
            return httpx.Response(
                207, content=_events_multistatus(_raw_event_response(_ics(WEEKLY_VEVENT)))
            )
        return _default_handler(request)

    client = _make_client(handler)
    event = await client.update_event("Work", "weekly-1", rrule="")
    assert event.is_recurring is False
    assert event.rrule is None
    assert b"RRULE" not in captured["body"]
    await client.close()


async def test_update_event_keeps_recurrence_when_rrule_omitted() -> None:
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            captured["body"] = request.content
        if request.method == "REPORT":
            return httpx.Response(
                207, content=_events_multistatus(_raw_event_response(_ics(WEEKLY_VEVENT)))
            )
        return _default_handler(request)

    client = _make_client(handler)
    event = await client.update_event("Work", "weekly-1", summary="Renamed standup")
    assert event.summary == "Renamed standup"
    assert event.is_recurring is True
    assert b"RRULE:FREQ=WEEKLY;BYDAY=MO" in captured["body"]
    await client.close()


# -- recurrence: editing/deleting a single occurrence ----------------------


def _occurrence_handler(ics: str, captured: dict[str, str]) -> Handler:
    """Serve ``ics`` for any REPORT and capture the PUT body."""

    def handler(request: httpx.Request) -> httpx.Response:
        disc = _discovery(request)
        if disc is not None:
            return disc
        if request.method == "REPORT":
            return httpx.Response(207, content=_events_multistatus(_raw_event_response(ics)))
        if request.method == "PUT":
            captured["body"] = request.content.decode()
            return httpx.Response(201, headers={"ETag": '"etag-new"'})
        return httpx.Response(500, text="unexpected")

    return handler


_OVERRIDE_0615 = (
    "BEGIN:VEVENT\r\nUID:weekly-1\r\nSUMMARY:Standup (old override)\r\n"
    "RECURRENCE-ID:20260615T090000Z\r\n"
    "DTSTART:20260615T093000Z\r\nDTEND:20260615T100000Z\r\nEND:VEVENT\r\n"
)
_ALLDAY_WEEKLY = (
    "BEGIN:VEVENT\r\nUID:weekly-1\r\nSUMMARY:Daily walk\r\n"
    "DTSTART;VALUE=DATE:20260601\r\nDTEND;VALUE=DATE:20260602\r\n"
    "RRULE:FREQ=WEEKLY;BYDAY=MO\r\nEND:VEVENT\r\n"
)
_SIMPLE_VEVENT = (
    "BEGIN:VEVENT\r\nUID:simple-1\r\nSUMMARY:One off\r\n"
    "DTSTART:20260610T120000Z\r\nDTEND:20260610T130000Z\r\nEND:VEVENT\r\n"
)


async def test_update_occurrence_adds_override() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_occurrence_handler(_ics(WEEKLY_VEVENT), captured))
    event = await client.update_occurrence(
        "Work",
        "weekly-1",
        datetime(2026, 6, 15, 9, tzinfo=UTC),
        summary="Moved",
        start=datetime(2026, 6, 15, 11, tzinfo=UTC),
    )
    assert event.summary == "Moved"
    assert event.start == datetime(2026, 6, 15, 11, tzinfo=UTC)
    assert event.is_recurring is True
    assert event.recurrence_id == datetime(2026, 6, 15, 9, tzinfo=UTC)
    body = captured["body"]
    assert "RECURRENCE-ID:20260615T090000Z" in body
    assert "SUMMARY:Moved" in body
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in body  # master rule untouched
    await client.close()


async def test_update_occurrence_updates_existing_override() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_occurrence_handler(_ics(WEEKLY_VEVENT, _OVERRIDE_0615), captured))
    await client.update_occurrence(
        "Work", "weekly-1", datetime(2026, 6, 15, 9, tzinfo=UTC), summary="Renamed once"
    )
    body = captured["body"]
    # Existing override is updated in place — not duplicated.
    assert body.count("RECURRENCE-ID:20260615T090000Z") == 1
    assert "SUMMARY:Renamed once" in body
    assert "Standup (old override)" not in body
    await client.close()


async def test_delete_occurrence_adds_exdate() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_occurrence_handler(_ics(WEEKLY_VEVENT), captured))
    result = await client.delete_occurrence("Work", "weekly-1", datetime(2026, 6, 8, 9, tzinfo=UTC))
    assert result["status"] == "deleted_occurrence"
    assert result["uid"] == "weekly-1"
    assert "EXDATE:20260608T090000Z" in captured["body"]
    await client.close()


async def test_delete_occurrence_drops_existing_override() -> None:
    override_0608 = (
        "BEGIN:VEVENT\r\nUID:weekly-1\r\nSUMMARY:Standup (override)\r\n"
        "RECURRENCE-ID:20260608T090000Z\r\n"
        "DTSTART:20260608T100000Z\r\nDTEND:20260608T103000Z\r\nEND:VEVENT\r\n"
    )
    captured: dict[str, str] = {}
    client = _make_client(_occurrence_handler(_ics(WEEKLY_VEVENT, override_0608), captured))
    await client.delete_occurrence("Work", "weekly-1", datetime(2026, 6, 8, 9, tzinfo=UTC))
    body = captured["body"]
    assert "EXDATE:20260608T090000Z" in body
    assert "RECURRENCE-ID:20260608T090000Z" not in body  # override removed
    await client.close()


async def test_update_occurrence_all_day_uses_date() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_occurrence_handler(_ics(_ALLDAY_WEEKLY), captured))
    await client.update_occurrence(
        "Work", "weekly-1", datetime(2026, 6, 15, tzinfo=UTC), summary="Long walk"
    )
    body = captured["body"]
    assert "RECURRENCE-ID;VALUE=DATE:20260615" in body
    assert "DTSTART;VALUE=DATE:20260615" in body
    await client.close()


async def test_update_occurrence_on_non_recurring_raises() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_occurrence_handler(_ics(_SIMPLE_VEVENT), captured))
    with pytest.raises(CalDAVError, match="não é uma série recorrente"):
        await client.update_occurrence(
            "Work", "simple-1", datetime(2026, 6, 10, 12, tzinfo=UTC), summary="x"
        )
    assert "body" not in captured  # nothing written
    await client.close()


async def test_update_occurrence_unknown_slot_raises() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_occurrence_handler(_ics(WEEKLY_VEVENT), captured))
    # 2026-06-10 is a Wednesday — not a slot of a Monday weekly series.
    with pytest.raises(CalDAVError, match="não existe na série"):
        await client.update_occurrence(
            "Work", "weekly-1", datetime(2026, 6, 10, 9, tzinfo=UTC), summary="x"
        )
    assert "body" not in captured
    await client.close()


# -- reminders (VTODO): fixtures & helpers ----------------------------------


def _vtodo(
    uid: str,
    summary: str,
    *,
    status: str = "NEEDS-ACTION",
    due: str | None = None,
    completed: str | None = None,
    priority: int | None = None,
    description: str | None = None,
    url: str | None = None,
    created: str | None = None,
    modified: str | None = None,
    extra: str | None = None,
) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//test//EN",
        "BEGIN:VTODO",
        f"UID:{uid}",
        f"SUMMARY:{summary}",
        f"STATUS:{status}",
    ]
    if due is not None:
        lines.append(f"DUE:{due}")
    if completed is not None:
        lines.append(f"COMPLETED:{completed}")
    if priority is not None:
        lines.append(f"PRIORITY:{priority}")
    if description is not None:
        lines.append(f"DESCRIPTION:{description}")
    if url is not None:
        lines.append(f"URL:{url}")
    if created is not None:
        lines.append(f"CREATED:{created}")
    if modified is not None:
        lines.append(f"LAST-MODIFIED:{modified}")
    if extra is not None:
        lines.append(extra)
    lines += ["END:VTODO", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


def _todo_response(uid: str, ics: str, etag: str = "etag-old") -> str:
    return (
        f"  <response>\n"
        f"    <href>/123456/calendars/reminders/{uid}.ics</href>\n"
        f"    <propstat><prop>\n"
        f'      <getetag>"{etag}"</getetag>\n'
        f"      <c:calendar-data>{ics}</c:calendar-data>\n"
        f"    </prop><status>HTTP/1.1 200 OK</status></propstat>\n"
        f"  </response>\n"
    )


# Two tasks for list tests: one pending (with due), one completed (no due).
_PENDING = _vtodo("todo-1", "Buy milk", due="20260620T100000Z", priority=5)
_DONE = _vtodo("todo-2", "Old task", status="COMPLETED", completed="20260601T120000Z")
_UNDATED = _vtodo("todo-3", "Someday")


def _reminders_list_handler(request: httpx.Request) -> httpx.Response:
    """Handler for read-only reminders list/get tests."""
    disc = _discovery(request)
    if disc is not None:
        return disc
    if request.method == "REPORT":
        body = request.content.decode("utf-8")
        if "text-match" not in body:  # list query (all VTODOs)
            return httpx.Response(
                207,
                content=_events_multistatus(
                    _todo_response("todo-3", _UNDATED, etag="e3"),
                    _todo_response("todo-1", _PENDING, etag="e1"),
                    _todo_response("todo-2", _DONE, etag="e2"),
                ),
            )
        match = re.search(r"<c:text-match[^>]*>([^<]+)</c:text-match>", body)
        uid = match.group(1) if match else ""
        if uid == "missing-uid":
            return httpx.Response(207, content=_events_multistatus())
        return httpx.Response(
            207, content=_events_multistatus(_todo_response(uid, _PENDING, etag="e1"))
        )
    if request.method == "DELETE":
        return httpx.Response(204)
    return httpx.Response(500, text="unexpected")


def _todo_write_handler(ics: str, captured: dict[str, str], uid: str = "todo-1") -> Handler:
    """Serve ``ics`` for any REPORT and capture the PUT/DELETE request."""

    def handler(request: httpx.Request) -> httpx.Response:
        disc = _discovery(request)
        if disc is not None:
            return disc
        if request.method == "REPORT":
            return httpx.Response(207, content=_events_multistatus(_todo_response(uid, ics)))
        if request.method == "PUT":
            captured["body"] = request.content.decode()
            captured["if_match"] = request.headers.get("If-Match") or ""
            captured["if_none_match"] = request.headers.get("If-None-Match") or ""
            return httpx.Response(201, headers={"ETag": '"etag-new"'})
        if request.method == "DELETE":
            captured["if_match"] = request.headers.get("If-Match") or ""
            captured["url"] = str(request.url)
            return httpx.Response(204)
        return httpx.Response(500, text="unexpected")

    return handler


# -- reminders: lists ------------------------------------------------------


async def test_list_reminder_lists_filters_vtodo() -> None:
    client = _make_client(_default_handler)
    lists = await client.list_reminder_lists()
    # Only the VTODO collection ('Tasks') — event calendars are excluded.
    assert {rl.name for rl in lists} == {"Tasks"}
    tasks = lists[0]
    assert tasks.url == "https://p99-caldav.icloud.com/123456/calendars/reminders/"
    await client.close()


async def test_resolve_unknown_reminder_list_raises() -> None:
    client = _make_client(_default_handler)
    with pytest.raises(CalDAVError, match="não encontrada"):
        await client.list_reminders("Nope")
    await client.close()


# -- reminders: read -------------------------------------------------------


async def test_list_reminders_hides_completed_by_default() -> None:
    client = _make_client(_reminders_list_handler)
    reminders = await client.list_reminders("Tasks")
    # Completed 'todo-2' is filtered; dated 'todo-1' sorts before undated 'todo-3'.
    assert [r.uid for r in reminders] == ["todo-1", "todo-3"]
    assert all(not r.completed for r in reminders)
    milk = reminders[0]
    assert milk.summary == "Buy milk"
    assert milk.due == datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
    assert milk.priority == 5
    assert reminders[1].due is None  # undated reminder
    await client.close()


async def test_list_reminders_include_completed() -> None:
    client = _make_client(_reminders_list_handler)
    reminders = await client.list_reminders("Tasks", include_completed=True)
    assert {r.uid for r in reminders} == {"todo-1", "todo-2", "todo-3"}
    done = next(r for r in reminders if r.uid == "todo-2")
    assert done.completed is True
    assert done.completed_at == datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    await client.close()


async def test_get_reminder_found() -> None:
    client = _make_client(_reminders_list_handler)
    reminder = await client.get_reminder("Tasks", "todo-1")
    assert reminder.uid == "todo-1"
    assert reminder.list == "Tasks"
    assert reminder.href.endswith("/reminders/todo-1.ics")  # type: ignore[union-attr]
    await client.close()


async def test_get_reminder_missing_raises() -> None:
    client = _make_client(_reminders_list_handler)
    with pytest.raises(CalDAVError, match="não encontrado"):
        await client.get_reminder("Tasks", "missing-uid")
    await client.close()


# -- reminders: write ------------------------------------------------------


async def test_create_reminder_puts_vtodo() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(_PENDING, captured))
    reminder = await client.create_reminder(
        "Tasks",
        summary="Pay rent",
        due=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        priority=1,
        description="via app",
    )
    assert reminder.summary == "Pay rent"
    assert reminder.completed is False
    assert reminder.etag == "etag-new"
    assert reminder.href is not None and reminder.href.endswith(".ics")
    assert captured["if_none_match"] == "*"
    assert "SUMMARY:Pay rent" in captured["body"]
    assert "STATUS:NEEDS-ACTION" in captured["body"]
    assert "DUE:20260701T090000Z" in captured["body"]
    assert "PRIORITY:1" in captured["body"]
    await client.close()


async def test_create_reminder_without_due() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(_UNDATED, captured))
    reminder = await client.create_reminder("Tasks", summary="Someday maybe")
    assert reminder.due is None
    assert "DUE" not in captured["body"]
    assert "SUMMARY:Someday maybe" in captured["body"]
    await client.close()


async def test_update_reminder_merges_and_sends_if_match() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(_PENDING, captured))
    reminder = await client.update_reminder("Tasks", "todo-1", summary="Buy oat milk")
    assert reminder.summary == "Buy oat milk"
    assert reminder.etag == "etag-new"
    assert captured["if_match"] == "etag-old"
    assert "SUMMARY:Buy oat milk" in captured["body"]
    # Untouched properties survive the round-trip.
    assert "DUE:20260620T100000Z" in captured["body"]
    await client.close()


async def test_complete_reminder_sets_completed() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(_PENDING, captured))
    reminder = await client.complete_reminder("Tasks", "todo-1")
    assert reminder.completed is True
    assert reminder.completed_at is not None
    body = captured["body"]
    assert "STATUS:COMPLETED" in body
    assert "PERCENT-COMPLETE:100" in body
    assert "COMPLETED:" in body
    await client.close()


async def test_reopen_reminder_clears_completion() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(_DONE, captured, uid="todo-2"))
    reminder = await client.reopen_reminder("Tasks", "todo-2")
    assert reminder.completed is False
    body = captured["body"]
    assert "STATUS:NEEDS-ACTION" in body
    assert "COMPLETED:" not in body
    await client.close()


async def test_delete_reminder_sends_if_match() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(_PENDING, captured))
    result = await client.delete_reminder("Tasks", "todo-1")
    assert result == {"status": "deleted", "uid": "todo-1"}
    assert captured["if_match"] == "etag-old"
    assert captured["url"].endswith("/reminders/todo-1.ics")
    await client.close()


# -- reminders: Phase 1 (clear, metadata, move, list management) ------------


async def test_update_reminder_clears_due() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(_PENDING, captured))
    reminder = await client.update_reminder("Tasks", "todo-1", clear=["due"])
    assert reminder.due is None
    assert "DUE" not in captured["body"]
    await client.close()


async def test_update_reminder_clear_then_set_wins() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(_PENDING, captured))
    # Clearing and setting the same field in one call ends up set.
    reminder = await client.update_reminder(
        "Tasks", "todo-1", due=datetime(2026, 8, 1, 9, tzinfo=UTC), clear=["due"]
    )
    assert reminder.due == datetime(2026, 8, 1, 9, tzinfo=UTC)
    assert "DUE:20260801T090000Z" in captured["body"]
    await client.close()


async def test_update_reminder_clear_unknown_field_raises() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(_PENDING, captured))
    with pytest.raises(CalDAVError, match="não pode ser limpo"):
        await client.update_reminder("Tasks", "todo-1", clear=["nope"])
    assert "body" not in captured  # nothing written
    await client.close()


async def test_reminder_parses_created_and_modified() -> None:
    ics = _vtodo("todo-1", "Buy milk", created="20260101T080000Z", modified="20260102T090000Z")
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(ics, captured))
    reminder = await client.get_reminder("Tasks", "todo-1")
    assert reminder.created == datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    assert reminder.modified == datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    await client.close()


# Discovery XML exposing two VTODO collections for move tests.
TWO_LISTS_XML = b"""<?xml version="1.0"?>
<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <response>
    <href>/123456/calendars/reminders/</href>
    <propstat><prop>
      <displayname>Tasks</displayname>
      <resourcetype><collection/><c:calendar/></resourcetype>
      <c:supported-calendar-component-set><c:comp name="VTODO"/>
      </c:supported-calendar-component-set>
    </prop><status>HTTP/1.1 200 OK</status></propstat>
  </response>
  <response>
    <href>/123456/calendars/personal/</href>
    <propstat><prop>
      <displayname>Personal</displayname>
      <resourcetype><collection/><c:calendar/></resourcetype>
      <c:supported-calendar-component-set><c:comp name="VTODO"/>
      </c:supported-calendar-component-set>
    </prop><status>HTTP/1.1 200 OK</status></propstat>
  </response>
</multistatus>"""


def _two_lists_discovery(request: httpx.Request) -> httpx.Response | None:
    if request.method == "PROPFIND" and request.url.path == "/":
        return httpx.Response(207, content=PRINCIPAL_XML)
    if request.method == "PROPFIND" and request.url.path.endswith("/principal/"):
        return httpx.Response(207, content=HOME_XML)
    if request.method == "PROPFIND" and request.url.path.endswith("/calendars/"):
        return httpx.Response(207, content=TWO_LISTS_XML)
    return None


async def test_move_reminder_copies_then_deletes() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        disc = _two_lists_discovery(request)
        if disc is not None:
            return disc
        if request.method == "REPORT":
            return httpx.Response(
                207, content=_events_multistatus(_todo_response("todo-1", _PENDING))
            )
        if request.method == "PUT":
            captured["put_url"] = str(request.url)
            captured["if_none_match"] = request.headers.get("If-None-Match") or ""
            return httpx.Response(201, headers={"ETag": '"etag-new"'})
        if request.method == "DELETE":
            captured["del_url"] = str(request.url)
            captured["if_match"] = request.headers.get("If-Match") or ""
            return httpx.Response(204)
        return httpx.Response(500, text="unexpected")

    client = _make_client(handler)
    reminder = await client.move_reminder("todo-1", "Tasks", "Personal")
    assert reminder.list == "Personal"
    assert reminder.etag == "etag-new"
    assert captured["put_url"].endswith("/personal/todo-1.ics")
    assert captured["if_none_match"] == "*"
    assert captured["del_url"].endswith("/reminders/todo-1.ics")
    assert captured["if_match"] == "etag-old"
    await client.close()


async def test_create_reminder_list_mkcalendar() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        disc = _discovery(request)
        if disc is not None:
            return disc
        if request.method == "MKCALENDAR":
            captured["url"] = str(request.url)
            captured["body"] = request.content.decode()
            return httpx.Response(201)
        return httpx.Response(500, text="unexpected")

    client = _make_client(handler)
    rlist = await client.create_reminder_list("Groceries", color="#00FF00")
    assert rlist.name == "Groceries"
    assert rlist.color == "#00FF00"
    assert rlist.url.startswith("https://p99-caldav.icloud.com/123456/calendars/")
    assert '<c:comp name="VTODO"/>' in captured["body"]
    assert "<d:displayname>Groceries</d:displayname>" in captured["body"]
    assert "#00FF00" in captured["body"]
    await client.close()


async def test_rename_reminder_list_proppatch() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        disc = _discovery(request)
        if disc is not None:
            return disc
        if request.method == "PROPPATCH":
            captured["url"] = str(request.url)
            captured["body"] = request.content.decode()
            return httpx.Response(207, content=_events_multistatus())
        return httpx.Response(500, text="unexpected")

    client = _make_client(handler)
    rlist = await client.rename_reminder_list("Tasks", "To Do")
    assert rlist.name == "To Do"
    assert captured["url"].endswith("/reminders/")
    assert "<d:displayname>To Do</d:displayname>" in captured["body"]
    await client.close()


async def test_delete_reminder_list_requires_confirm() -> None:
    client = _make_client(_default_handler)
    with pytest.raises(CalDAVError, match="confirm=True"):
        await client.delete_reminder_list("Tasks")
    await client.close()


async def test_delete_reminder_list_with_confirm() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        disc = _discovery(request)
        if disc is not None:
            return disc
        if request.method == "DELETE":
            captured["url"] = str(request.url)
            return httpx.Response(204)
        return httpx.Response(500, text="unexpected")

    client = _make_client(handler)
    result = await client.delete_reminder_list("Tasks", confirm=True)
    assert result == {"status": "deleted_list", "list": "Tasks"}
    assert captured["url"].endswith("/reminders/")
    await client.close()


# -- reminders: Phase 2 (cross-list search) --------------------------------


def _todo_response_at(slug: str, uid: str, ics: str, etag: str = "e") -> str:
    return (
        f"  <response>\n"
        f"    <href>/123456/calendars/{slug}/{uid}.ics</href>\n"
        f"    <propstat><prop>\n"
        f'      <getetag>"{etag}"</getetag>\n'
        f"      <c:calendar-data>{ics}</c:calendar-data>\n"
        f"    </prop><status>HTTP/1.1 200 OK</status></propstat>\n"
        f"  </response>\n"
    )


def _search_handler(request: httpx.Request) -> httpx.Response:
    """Two lists with dated/undated/completed tasks for search tests."""
    disc = _two_lists_discovery(request)
    if disc is not None:
        return disc
    if request.method == "REPORT":
        if "/reminders/" in request.url.path:  # 'Tasks'
            return httpx.Response(
                207,
                content=_events_multistatus(
                    _todo_response_at(
                        "reminders",
                        "t-today",
                        _vtodo("t-today", "Today task", due="20260618T100000Z"),
                    ),
                    _todo_response_at(
                        "reminders",
                        "t-future",
                        _vtodo("t-future", "Future task", due="20260625T090000Z"),
                    ),
                    _todo_response_at(
                        "reminders",
                        "t-done",
                        _vtodo(
                            "t-done", "Done task", status="COMPLETED", completed="20260601T120000Z"
                        ),
                    ),
                ),
            )
        if "/personal/" in request.url.path:  # 'Personal'
            return httpx.Response(
                207,
                content=_events_multistatus(
                    _todo_response_at(
                        "personal",
                        "p-overdue",
                        _vtodo("p-overdue", "Overdue task", due="20260610T090000Z"),
                    ),
                    _todo_response_at("personal", "p-someday", _vtodo("p-someday", "Someday plan")),
                ),
            )
    return httpx.Response(500, text="unexpected")


async def test_search_reminders_all_lists_default() -> None:
    client = _make_client(_search_handler)
    reminders = await client.search_reminders()
    # Completed 't-done' hidden; sorted by due (undated last).
    assert [r.uid for r in reminders] == ["p-overdue", "t-today", "t-future", "p-someday"]
    assert {r.list for r in reminders} == {"Tasks", "Personal"}
    await client.close()


async def test_search_reminders_overdue_preset() -> None:
    client = _make_client(_search_handler)
    reminders = await client.search_reminders(
        due_before=datetime(2026, 6, 18, tzinfo=UTC), undated=False
    )
    assert [r.uid for r in reminders] == ["p-overdue"]
    await client.close()


async def test_search_reminders_due_window() -> None:
    client = _make_client(_search_handler)
    reminders = await client.search_reminders(
        due_after=datetime(2026, 6, 18, tzinfo=UTC),
        due_before=datetime(2026, 6, 19, tzinfo=UTC),
        undated=False,
    )
    assert [r.uid for r in reminders] == ["t-today"]
    await client.close()


async def test_search_reminders_query_matches_title() -> None:
    client = _make_client(_search_handler)
    reminders = await client.search_reminders(query="someday")
    assert [r.uid for r in reminders] == ["p-someday"]
    await client.close()


async def test_search_reminders_include_completed() -> None:
    client = _make_client(_search_handler)
    reminders = await client.search_reminders(include_completed=True)
    assert "t-done" in {r.uid for r in reminders}
    await client.close()


async def test_search_reminders_restrict_lists() -> None:
    client = _make_client(_search_handler)
    reminders = await client.search_reminders(lists=["Personal"])
    assert {r.list for r in reminders} == {"Personal"}
    assert {r.uid for r in reminders} == {"p-overdue", "p-someday"}
    await client.close()


async def test_search_reminders_unknown_list_raises() -> None:
    client = _make_client(_search_handler)
    with pytest.raises(CalDAVError, match="não encontrada"):
        await client.search_reminders(lists=["Nope"])
    await client.close()


# -- reminders: Phase 3 (recurrence) ---------------------------------------


_WEEKLY_TODO = _vtodo(
    "todo-r", "Weekly chore", due="20260608T090000Z", extra="RRULE:FREQ=WEEKLY;BYDAY=MO"
)


async def test_create_recurring_reminder_puts_rrule() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(_PENDING, captured))
    reminder = await client.create_reminder(
        "Tasks",
        summary="Weekly chore",
        due=datetime(2026, 6, 8, 9, tzinfo=UTC),
        rrule="FREQ=WEEKLY;BYDAY=MO",
    )
    assert reminder.is_recurring is True
    assert reminder.rrule == "FREQ=WEEKLY;BYDAY=MO"
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in captured["body"]
    await client.close()


async def test_create_reminder_invalid_rrule_raises_before_put() -> None:
    put_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            put_calls["n"] += 1
        return _default_handler(request)

    client = _make_client(handler)
    with pytest.raises(CalDAVError, match="RRULE inválida"):
        await client.create_reminder("Tasks", summary="Bad", rrule="not a rule")
    assert put_calls["n"] == 0
    await client.close()


async def test_reminder_parses_rrule() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(_WEEKLY_TODO, captured, uid="todo-r"))
    reminder = await client.get_reminder("Tasks", "todo-r")
    assert reminder.is_recurring is True
    assert reminder.rrule == "FREQ=WEEKLY;BYDAY=MO"
    await client.close()


async def test_update_reminder_removes_recurrence_with_empty_rrule() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(_WEEKLY_TODO, captured, uid="todo-r"))
    reminder = await client.update_reminder("Tasks", "todo-r", rrule="")
    assert reminder.is_recurring is False
    assert "RRULE" not in captured["body"]
    await client.close()


async def test_complete_recurring_reminder_advances_to_next_occurrence() -> None:
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(_WEEKLY_TODO, captured, uid="todo-r"))
    reminder = await client.complete_reminder("Tasks", "todo-r")
    body = captured["body"]
    # Advanced from 2026-06-08 to the next Monday; stays needs-action.
    assert "DUE:20260615T090000Z" in body
    assert "STATUS:NEEDS-ACTION" in body
    assert "COMPLETED:" not in body
    assert reminder.completed is False
    assert reminder.due == datetime(2026, 6, 15, 9, tzinfo=UTC)
    await client.close()


async def test_complete_exhausted_recurring_reminder_marks_done() -> None:
    once = _vtodo(
        "todo-once", "One shot", due="20260608T090000Z", extra="RRULE:FREQ=WEEKLY;COUNT=1"
    )
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(once, captured, uid="todo-once"))
    reminder = await client.complete_reminder("Tasks", "todo-once")
    body = captured["body"]
    # Series exhausted (COUNT=1) → the whole task is completed.
    assert "STATUS:COMPLETED" in body
    assert reminder.completed is True
    await client.close()


async def test_list_reminders_rolls_recurring_due_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "icloud_mcp.caldav_client._utcnow", lambda: datetime(2026, 6, 17, tzinfo=UTC)
    )
    past_weekly = _vtodo(
        "todo-r", "Weekly chore", due="20260601T090000Z", extra="RRULE:FREQ=WEEKLY;BYDAY=MO"
    )
    captured: dict[str, str] = {}
    client = _make_client(_todo_write_handler(past_weekly, captured, uid="todo-r"))
    reminders = await client.list_reminders("Tasks")
    # Master due 2026-06-01 rolled forward to the next Monday on/after 2026-06-17.
    assert reminders[0].due == datetime(2026, 6, 22, 9, tzinfo=UTC)
    await client.close()


# -- recurrence helpers (pure) ---------------------------------------------


def test_next_occurrence_weekly() -> None:
    nxt = _next_occurrence(
        datetime(2026, 6, 1, 9, tzinfo=UTC),
        "FREQ=WEEKLY;BYDAY=MO",
        after=datetime(2026, 6, 17, tzinfo=UTC),
    )
    assert nxt == datetime(2026, 6, 22, 9, tzinfo=UTC)


def test_next_occurrence_exhausted_returns_none() -> None:
    nxt = _next_occurrence(
        datetime(2026, 6, 1, tzinfo=UTC),
        "FREQ=DAILY;COUNT=1",
        after=datetime(2026, 6, 17, tzinfo=UTC),
    )
    assert nxt is None


def test_apply_recurring_due_skips_completed_and_undated() -> None:
    now = datetime(2026, 6, 17, tzinfo=UTC)
    completed = Reminder(
        uid="c",
        list="Tasks",
        completed=True,
        is_recurring=True,
        rrule="FREQ=WEEKLY",
        due=datetime(2026, 6, 1, tzinfo=UTC),
    )
    undated = Reminder(uid="u", list="Tasks", is_recurring=True, rrule="FREQ=WEEKLY")
    _apply_recurring_due([completed, undated], now)
    assert completed.due == datetime(2026, 6, 1, tzinfo=UTC)  # untouched
    assert undated.due is None
