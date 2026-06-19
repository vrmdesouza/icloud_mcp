# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`icloud_mcp` (the "iCloud MCP" server) is a Python MCP (Model Context Protocol) server that connects Claude to iCloud. It exposes tools for **Mail** (reading, searching, sending emails, managing folders) over IMAP/SMTP using a persistent IMAP connection pool, for **Calendar** (viewing, creating, editing, deleting events) over CalDAV (`VEVENT`), and for **Reminders** (viewing, creating, editing, completing, deleting tasks) via the **native macOS Reminders app** through EventKit (PyObjC).

> **Why EventKit for Reminders, not CalDAV?** Since iOS 13 / macOS Catalina, the Reminders app migrates tasks off CalDAV into a private store that only the local Reminders app (and EventKit) can read. On upgraded accounts CalDAV `VTODO` only ever sees the empty "Reminders ⚠️" placeholder list — so Reminders are served locally via EventKit instead. This requires the server to run on macOS with the Reminders privacy permission granted. Mail and Calendar are unaffected.

## Development Commands

This project uses `uv` for all package and environment management.

```bash
# Install dependencies
uv sync

# Run the MCP server (stdio transport — used by Claude Desktop)
uv run python -m icloud_mcp

# Linting and formatting (Ruff)
uv run ruff check .
uv run ruff format .
uv run ruff check --fix .

# Type checking
uv run mypy src/

# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_imap_client.py

# Run a single test by name
uv run pytest tests/test_imap_client.py::test_fetch_email -v

# Run async tests (pytest-asyncio is configured in pyproject.toml)
uv run pytest -v --asyncio-mode=auto
```

## iCloud Configuration

Credentials are provided exclusively via environment variables. Create a `.env` file (never commit it):

```
ICLOUD_EMAIL=you@icloud.com
ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
```

The **same** App-Specific Password is used for Mail (IMAP/SMTP) and Calendar (CalDAV) — Apple shares the credential across both services.

Optional configuration variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `IMAP_POOL_SIZE` | `3` | Number of persistent IMAP connections in the pool |
| `IMAP_TIMEOUT` | `30` | Timeout in seconds for IMAP operations |
| `CALDAV_TIMEOUT` | `30` | Timeout in seconds for CalDAV (Calendar) operations |
| `EVENTKIT_TIMEOUT` | `30` | Timeout in seconds for EventKit (Reminders) fetch/authorization |

> An **App-Specific Password** must be generated at appleid.apple.com (requires 2FA) — the regular Apple ID password does not work with IMAP/SMTP/CalDAV.

A `.env.example` file should be maintained in the repo with all variables (without real values) for onboarding reference.

iCloud server endpoints:
- **IMAP**: `imap.mail.me.com:993` (SSL/TLS)
- **SMTP**: `smtp.mail.me.com:587` (STARTTLS)
- **CalDAV**: `caldav.icloud.com:443` (HTTPS) — the per-account calendar-home-set is discovered at runtime on a partition host (e.g. `p67-caldav.icloud.com`).
- **Reminders**: no network endpoint — served locally by the macOS Reminders app via EventKit. Requires the Reminders privacy permission (Ajustes do Sistema → Privacidade e Segurança → Lembretes) for the host process (Claude Desktop, or the terminal when running via `uv run`). The first call triggers the system prompt.

## Code Conventions

- **Language**: Code, variable names, docstrings, and comments in English. Log messages and user-facing error messages in Portuguese (PT-BR).
- **Docstrings**: Google-style for all public functions and classes.
- **Type hints**: Required on all functions (mypy strict is enforced).
- **Custom exceptions**: Use a hierarchy of custom exceptions:
  - `IMAPConnectionError` — connection or pool failures
  - `IMAPAuthenticationError` — login/credential failures
  - `SMTPSendError` — send failures
  - `CalDAVError` / `CalDAVConnectionError` / `CalDAVAuthenticationError` — Calendar errors
  - `EventKitError` / `EventKitAuthorizationError` / `EventKitNotAvailableError` — Reminders errors
  - All inherit from a base `ICloudError` (`ICloudMailError` remains as a backward-compatible alias)

## Architecture

```
src/icloud_mcp/
├── __main__.py       # Entry point: runs the MCP server via stdio
├── server.py         # Tool/resource registration with @mcp.tool() decorators
├── config.py         # Loads and validates env vars (ICLOUD_EMAIL, ICLOUD_APP_PASSWORD)
├── imap_client.py    # Persistent IMAP connection pool — all read/search/folder ops
├── smtp_client.py    # SMTP client — creates connection per send operation
├── caldav_client.py  # Async CalDAV client (httpx) — Calendar discovery + event CRUD
├── eventkit_store.py # Native Reminders backend (PyObjC/EventKit) — the only PyObjC layer
├── eventkit_client.py# Reminders orchestration over a ReminderStore (pure Python)
├── rules.py          # Local JSON-backed mail filtering rules engine
└── models.py         # Pydantic models: Email, Folder, Calendar, CalendarEvent, ReminderList, Reminder, etc.

tests/
├── conftest.py       # Shared fixtures (mock IMAP/SMTP connections)
├── test_imap_client.py
├── test_smtp_client.py
├── test_caldav_client.py    # CalDAV client tests (httpx.MockTransport, no network)
├── test_eventkit_client.py  # Reminders orchestration vs an in-memory FakeReminderStore
├── test_eventkit_store.py   # Live EventKit round-trip (gated: macOS + ICLOUD_MCP_LIVE_EVENTKIT=1)
└── test_server.py    # Tests for MCP tool handlers
```

### Suggested Implementation Order

`config` → `models` → `imap_client` → `smtp_client` → `server` → `__main__` → `tests`

### Key Architectural Decisions

**Persistent IMAP connection pool** (`imap_client.py`): The pool maintains open IMAP connections and reuses them across tool calls. It must handle automatic reconnection on idle timeouts (iCloud disconnects after ~30 minutes of inactivity). All IMAP operations are `async` using `aioimaplib`.

**SMTP is stateless**: `smtp_client.py` opens a fresh `aiosmtplib` connection per send operation. No pooling needed.

**Server entry point** (`server.py`): All MCP tools are registered here using the `@mcp.tool()` decorator from the official `mcp` SDK. Tools call into `imap_client` or `smtp_client` — no IMAP/SMTP logic belongs in `server.py`.

**CalDAV is stateless** (`caldav_client.py`): No connection pool. A single `httpx.AsyncClient` carries Basic Auth across requests. At startup, `connect()` runs the two-step iCloud discovery (`current-user-principal` → `calendar-home-set`) and caches the resulting partition-host URL. Events are addressed by `href`/`ETag` we track ourselves, sidestepping iCloud's broken `get_object_by_uid`. Attendees and alarms are intentionally out of scope for v1.

**Reminders use a native EventKit backend** (`eventkit_store.py` + `eventkit_client.py`): Reminders are served by the local macOS Reminders app through EventKit (PyObjC), split into two layers behind a `ReminderStore` protocol:

- **`eventkit_store.py` — `EventKitStore`**: the *only* place that touches PyObjC / `EKEventStore`. It requests Reminders authorization on `connect()` (bridging EventKit's completion-handler APIs to a synchronous result via `threading.Event`, all run off the event loop with `asyncio.to_thread`), lists `EKCalendar`s of the reminders entity type, fetches via `predicateForRemindersInCalendars:`/`fetchRemindersMatchingPredicate:completion:`, and converts `EKReminder ↔ Reminder` (dates ↔ `NSDateComponents`, `RRULE ↔ EKRecurrenceRule`, alarms ↔ `EKAlarm`). The reminder UID is `calendarItemExternalIdentifier` (the stable iCalendar UID); lookups scan the lists (small) to match it.
- **`eventkit_client.py` — `EventKitClient`**: pure-Python orchestration over a `ReminderStore` — list resolution by display name, completed filtering, due-date sorting, cross-list search, partial-update merging, and `clear` semantics. It holds no PyObjC, so it is unit-tested against an in-memory `FakeReminderStore` on any platform. `server.py` delegates the reminder tools here.

A reminder *with* a `due` is a deadline task; *without* it is a plain task — both are read/written identically. **Update** merges partial changes onto the current reminder and writes the full desired state back; the store mutates the fetched `EKReminder` in place, so unmodeled native properties (location, `X-APPLE-…`) survive. **Completion** only toggles `isCompleted` so EventKit's native advance of recurring tasks is preserved (see below). **Move** sets the reminder's `calendar` and saves, preserving identity. **Priority** uses the same 0–9 iCalendar scale EventKit exposes. **Out of scope:** subtasks and iCloud-proprietary features EventKit doesn't surface (tags, smart lists).

**Config validation** (`config.py`): Reads env vars at startup and fails fast with a clear error if required vars are missing. Use `pydantic-settings` for this.

## Error Handling & Resilience

### IMAP Pool

- **Retry**: Exponential backoff — 3 attempts with delays of 1s, 2s, 4s.
- **Reconnect**: Automatic reconnection when a connection is lost due to idle timeout (~30 min on iCloud). The pool should detect stale connections before reuse (e.g., via NOOP) and replace them transparently.
- **Exceptions**: Raise `IMAPConnectionError` or `IMAPAuthenticationError` after retries are exhausted.

### SMTP

- **Retry**: Simple retry — 2 attempts (connection is ephemeral, failures are usually transient).
- **Exceptions**: Raise `SMTPSendError` with the original error context.

### CalDAV

- **Retry**: Simple retry — 3 attempts with delays of 1s, 2s on transport errors and HTTP 5xx.
- **Auth**: HTTP 401 raises `CalDAVAuthenticationError` immediately (no retry) — usually a regular password used where an App-Specific Password belongs.
- **Exceptions**: Other 4xx raise `CalDAVError`; exhausted retries raise `CalDAVConnectionError`.

### EventKit (Reminders)

- **Authorization**: `EventKitStore.connect()` requests Reminders access; a denial/restriction raises `EventKitAuthorizationError` (with PT-BR guidance on granting it). The lifespan logs a warning and keeps Mail/Calendar working if Reminders are unavailable.
- **Availability**: constructing `EventKitStore` off macOS (or without the PyObjC bindings) raises `EventKitNotAvailableError`.
- **Exceptions**: a save/remove the store rejects, or a missing reminder/list, raises `EventKitError`.

### Exception Hierarchy

```python
class ICloudError(Exception): ...
ICloudMailError = ICloudError  # backward-compatible alias
class IMAPConnectionError(ICloudError): ...
class IMAPAuthenticationError(ICloudError): ...
class SMTPSendError(ICloudError): ...
class CalDAVError(ICloudError): ...
class CalDAVConnectionError(CalDAVError): ...
class CalDAVAuthenticationError(CalDAVError): ...
class EventKitError(ICloudError): ...
class EventKitAuthorizationError(EventKitError): ...
class EventKitNotAvailableError(EventKitError): ...
```

## MCP Tools

| Tool | Transport | Parameters | Return | Description |
|------|-----------|------------|--------|-------------|
| `list_folders` | IMAP | — | `list[Folder]` | List all mailbox folders |
| `list_emails` | IMAP | `folder: str`, `limit: int = 20`, `offset: int = 0` | `list[Email]` | List emails in a folder with offset-based pagination |
| `get_email` | IMAP | `folder: str`, `uid: str` | `Email` | Fetch full email by UID (headers + body + attachments metadata) |
| `search_emails` | IMAP | `folder: str`, `sender: str?`, `subject: str?`, `since: date?`, `before: date?`, `body: str?`, `limit: int = 20` | `list[Email]` | Search using IMAP SEARCH criteria. Parameters are combined with AND. |
| `send_email` | SMTP | `to: list[str]`, `subject: str`, `body: str`, `cc: list[str]?`, `bcc: list[str]?` | `dict` | Send a new email |
| `move_email` | IMAP | `folder: str`, `uid: str`, `destination: str` | `dict` | Move email between folders (COPY + delete original) |
| `delete_email` | IMAP | `folder: str`, `uid: str` | `dict` | Move email to Trash |
| `create_folder` | IMAP | `name: str` | `Folder` | Create a new mailbox folder |

> Mail also exposes additional tools (`mark_as_read`/`mark_as_unread`, `flag_email`/`unflag_email`, `bulk_action`, `rename_folder`, `delete_folder`, `get_folder_stats`, `list_attachments`, `download_attachment`, `save_draft`, `reply_email`, `forward_email`, and the rules tools). See `server.py`.

### Calendar Tools (CalDAV)

| Tool | Parameters | Return | Description |
|------|------------|--------|-------------|
| `list_calendars` | — | `list[Calendar]` | List calendars that support events |
| `list_events` | `calendar: str`, `start: str`, `end: str` | `list[CalendarEvent]` | Events overlapping the `[start, end)` time range (ISO 8601). Recurring series are expanded into one entry per occurrence. |
| `get_event` | `calendar: str`, `uid: str` | `CalendarEvent` | Fetch a single event by iCalendar UID (series master, `RRULE` preserved, not expanded) |
| `create_event` | `calendar: str`, `summary: str`, `start: str`, `end: str`, `all_day: bool = False`, `location: str?`, `description: str?`, `rrule: str?` | `CalendarEvent` | Create a new event; pass `rrule` for a recurring series |
| `update_event` | `calendar: str`, `uid: str`, + optional `summary`/`start`/`end`/`all_day`/`location`/`description`/`rrule` | `CalendarEvent` | Update the provided fields (whole series). `rrule=""` removes recurrence |
| `delete_event` | `calendar: str`, `uid: str` | `dict` | Delete an event/series by UID |
| `update_occurrence` | `calendar: str`, `uid: str`, `recurrence_id: str`, + optional `summary`/`start`/`end`/`location`/`description` | `CalendarEvent` | Edit a **single occurrence** of a series (adds a `RECURRENCE-ID` override) |
| `delete_occurrence` | `calendar: str`, `uid: str`, `recurrence_id: str` | `dict` | Delete a **single occurrence** of a series (adds an `EXDATE`) |

#### Recurrence

iCloud's server-side `expand` is unreliable, so recurrence is expanded **client-side** with `recurring-ical-events`:
- `list_events` expands `RRULE`/`RDATE`/`EXDATE`/`RECURRENCE-ID` into concrete occurrences within the requested window (each carries `recurrence_id`, `is_recurring=True`). Expansion always requires a finite window.
- `get_event` returns the **series master** with its `rrule` preserved — it does not expand.
- `create_event`/`update_event` accept a raw `rrule` (e.g. `"FREQ=WEEKLY;BYDAY=MO"`), validated before the `PUT`. They operate on the **whole series**.
- **Single occurrence**: `update_occurrence` adds/updates a `RECURRENCE-ID` override inside the same resource; `delete_occurrence` adds an `EXDATE` to the master (and drops any override for that slot). Both address the occurrence by `recurrence_id` (the original slot, as returned by `list_events`), validate it against the series, and PUT the whole resource with `If-Match`. The `RECURRENCE-ID`/`EXDATE` value type (date vs datetime) is derived from the master's `DTSTART`. **Out of scope:** "this and future" (`THISANDFUTURE`).

### Reminder Tools (native macOS EventKit)

| Tool | Parameters | Return | Description |
|------|------------|--------|-------------|
| `list_reminder_lists` | — | `list[ReminderList]` | List Reminders lists (`EKCalendar`s of the reminders entity type) |
| `list_reminders` | `list: str`, `include_completed: bool = False` | `list[Reminder]` | Tasks in a list, ordered by `due` (undated last). Hides completed by default |
| `search_reminders` | `query: str?`, `due_before: str?`, `due_after: str?`, `include_completed: bool = False`, `undated: bool = True`, `lists: list[str]?` | `list[Reminder]` | Search **across all lists** (or a subset), ordered by `due`. Presets: overdue (`due_before=now`, `undated=False`), due today (`due_before`=end of today, `undated=False`), free-text (`query`). Lists fetched concurrently |
| `get_reminder` | `list: str`, `uid: str` | `Reminder` | Fetch a single reminder by UID (`calendarItemExternalIdentifier`) |
| `create_reminder` | `list: str`, `summary: str`, `due: str?`, `start: str?`, `all_day: bool = False`, `priority: int?`, `description: str?`, `url: str?`, `rrule: str?`, `alarms: list[dict]?` | `Reminder` | Create a task; omit `due` for a task without a deadline. Pass `rrule` for a recurring task, `alarms` for `EKAlarm`s |
| `update_reminder` | `list: str`, `uid: str`, + optional `summary`/`due`/`start`/`all_day`/`priority`/`description`/`url`/`rrule`/`alarms`, `clear: list[str]?` | `Reminder` | Update the provided fields. `rrule=""` removes recurrence; `alarms` (any list, incl. `[]`) replaces all alarms. `clear` unsets fields entirely (`due`/`start`/`description`/`url`/`priority`) |
| `complete_reminder` | `list: str`, `uid: str` | `Reminder` | Mark a task completed (sets `isCompleted`; recurring tasks advance natively) |
| `reopen_reminder` | `list: str`, `uid: str` | `Reminder` | Reopen a completed task (clears `isCompleted`) |
| `delete_reminder` | `list: str`, `uid: str` | `dict` | Delete a task by UID |
| `move_reminder` | `uid: str`, `from_list: str`, `to_list: str` | `Reminder` | Move a task between lists (sets its `calendar`; identity preserved) |
| `create_reminder_list` | `name: str`, `color: str?` | `ReminderList` | Create a new list (a reminders-type `EKCalendar`) |
| `rename_reminder_list` | `name: str`, `new_name: str` | `ReminderList` | Rename a list (`EKCalendar.title`) |
| `delete_reminder_list` | `name: str`, `confirm: bool = False` | `dict` | Delete a list **and all its tasks**; requires `confirm=True` |

`priority` follows iCalendar (EventKit uses the same scale): 0 = none, 1–4 = high, 5 = medium, 6–9 = low. `created`/`modified` (from `creationDate`/`lastModifiedDate`) are exposed read-only on `Reminder`.

#### Recurring reminders

A recurring task is one `Reminder` carrying its `rrule`/`is_recurring` — never expanded into many rows. With EventKit the stored `due` already reflects the **current** occurrence (the system advances it natively), so `list_reminders`/`search_reminders`/`get_reminder` all return `due` as-is; there is no client-side roll-forward.
- `complete_reminder` on a recurring task only sets `isCompleted` and saves — **EventKit/Reminders.app advances the task to its next occurrence natively** (matching task-app behavior). We do not simulate the advance.
- `rrule` is converted to/from `EKRecurrenceRule` for the common subset (`FREQ`/`INTERVAL`/`BYDAY`/`COUNT`/`UNTIL`); exotic rules (e.g. `BYSETPOS`) are best-effort and may be lossy.
- **Caveat (validate against a real account):** the native advance-on-complete behavior is the Reminders app's, which is the reference — confirm with the gated live test (`test_eventkit_store.py`).

#### Alarms

`Reminder.alarms` is a list of `ReminderAlarm`, each carrying exactly one of `minutes_before` (a relative `EKAlarm`, fired before the due) or `trigger` (an absolute `EKAlarm`). `create_reminder`/`update_reminder` accept `alarms` as a list of dicts (`{"minutes_before": 30}` or `{"trigger": "2026-07-01T08:00:00"}`); on `update_reminder`, `alarms=None` keeps the existing alarms while any list (including `[]`) replaces them all.

### Pagination (`list_emails`)

Uses offset-based pagination with `limit` (number of emails to return, default 20) and `offset` (number of emails to skip, default 0). Emails are ordered by date descending (most recent first).

### Search (`search_emails`)

All search parameters are optional and combined with AND logic. Maps to IMAP SEARCH commands:
- `sender` → `FROM`
- `subject` → `SUBJECT`
- `since` → `SINCE` (inclusive)
- `before` → `BEFORE` (exclusive)
- `body` → `BODY`

## Testing Strategy

- **Mocking**: All IMAP and SMTP connections are mocked in `conftest.py` shared fixtures. No real network calls in tests.
- **Async**: All tests use `pytest-asyncio` with `asyncio_mode = "auto"` — just write `async def test_*` functions.
- **Coverage focus**: All public functions in `imap_client.py` and `smtp_client.py` must have tests. Server tool handlers should be tested with mocked client calls.
- **Edge cases**: Test retry/reconnect logic, invalid credentials, malformed emails, empty folders, and pagination boundaries.

## Git Workflow

- **Conventional commits**: `feat:`, `fix:`, `chore:`, `refactor:`, `test:`, `docs:`
- Run `uv run ruff check .` and `uv run ruff format .` before every commit
- Run `uv run pytest` before significant commits (new features, refactors)

## Tooling Configuration

All tool config lives in `pyproject.toml`:

```toml
[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]  # includes flake8-async rules

[tool.mypy]
strict = true
python_version = "3.12"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

## Key Dependencies

- `mcp` — Anthropic official MCP Python SDK
- `aioimaplib` — async IMAP4 client
- `aiosmtplib` — async SMTP client
- `httpx` — async HTTP client (CalDAV transport)
- `icalendar` — build/parse iCalendar (`VEVENT`) documents; also parses `RRULE` strings for EventKit recurrence conversion
- `recurring-ical-events` — client-side expansion of recurring events (`RRULE`/`EXDATE`/`RECURRENCE-ID`)
- `pyobjc-framework-EventKit` — native macOS Reminders access (macOS only; `sys_platform == 'darwin'`)
- `pydantic-settings` — env var loading with validation
- `python-dotenv` — `.env` file support in development
- `ruff`, `mypy`, `pytest`, `pytest-asyncio` — dev dependencies
