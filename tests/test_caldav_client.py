"""Tests for caldav_client.py — async iCloud CalDAV client over httpx.

All HTTP traffic is intercepted with httpx.MockTransport; no real network.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from icloud_mcp.caldav_client import CalDAVClient
from icloud_mcp.config import ICloudMailSettings
from icloud_mcp.exceptions import (
    CalDAVAuthenticationError,
    CalDAVConnectionError,
    CalDAVError,
)

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
