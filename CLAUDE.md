# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`icloud_mail_mcp` is a Python MCP (Model Context Protocol) server that connects Claude to iCloud Mail via IMAP and SMTP. It exposes tools for reading, searching, sending emails, and managing folders using a persistent IMAP connection pool.

## Development Commands

This project uses `uv` for all package and environment management.

```bash
# Install dependencies
uv sync

# Run the MCP server (stdio transport — used by Claude Desktop)
uv run python -m icloud_mail_mcp

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

## iCloud Mail Configuration

Credentials are provided exclusively via environment variables. Create a `.env` file (never commit it):

```
ICLOUD_EMAIL=you@icloud.com
ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
```

Optional configuration variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `IMAP_POOL_SIZE` | `3` | Number of persistent IMAP connections in the pool |
| `IMAP_TIMEOUT` | `30` | Timeout in seconds for IMAP operations |

> An **App-Specific Password** must be generated at appleid.apple.com — the regular Apple ID password does not work with IMAP/SMTP.

A `.env.example` file should be maintained in the repo with all variables (without real values) for onboarding reference.

iCloud server endpoints:
- **IMAP**: `imap.mail.me.com:993` (SSL/TLS)
- **SMTP**: `smtp.mail.me.com:587` (STARTTLS)

## Code Conventions

- **Language**: Code, variable names, docstrings, and comments in English. Log messages and user-facing error messages in Portuguese (PT-BR).
- **Docstrings**: Google-style for all public functions and classes.
- **Type hints**: Required on all functions (mypy strict is enforced).
- **Custom exceptions**: Use a hierarchy of custom exceptions for IMAP/SMTP errors:
  - `IMAPConnectionError` — connection or pool failures
  - `IMAPAuthenticationError` — login/credential failures
  - `SMTPSendError` — send failures
  - All inherit from a base `ICloudMailError`

## Architecture

```
src/icloud_mail_mcp/
├── __main__.py       # Entry point: runs the MCP server via stdio
├── server.py         # Tool/resource registration with @mcp.tool() decorators
├── config.py         # Loads and validates env vars (ICLOUD_EMAIL, ICLOUD_APP_PASSWORD)
├── imap_client.py    # Persistent IMAP connection pool — all read/search/folder ops
├── smtp_client.py    # SMTP client — creates connection per send operation
└── models.py         # Pydantic models: Email, Folder, SearchQuery, etc.

tests/
├── conftest.py       # Shared fixtures (mock IMAP/SMTP connections)
├── test_imap_client.py
├── test_smtp_client.py
└── test_server.py    # Tests for MCP tool handlers
```

### Suggested Implementation Order

`config` → `models` → `imap_client` → `smtp_client` → `server` → `__main__` → `tests`

### Key Architectural Decisions

**Persistent IMAP connection pool** (`imap_client.py`): The pool maintains open IMAP connections and reuses them across tool calls. It must handle automatic reconnection on idle timeouts (iCloud disconnects after ~30 minutes of inactivity). All IMAP operations are `async` using `aioimaplib`.

**SMTP is stateless**: `smtp_client.py` opens a fresh `aiosmtplib` connection per send operation. No pooling needed.

**Server entry point** (`server.py`): All MCP tools are registered here using the `@mcp.tool()` decorator from the official `mcp` SDK. Tools call into `imap_client` or `smtp_client` — no IMAP/SMTP logic belongs in `server.py`.

**Config validation** (`config.py`): Reads env vars at startup and fails fast with a clear error if required vars are missing. Use `pydantic-settings` for this.

## Error Handling & Resilience

### IMAP Pool

- **Retry**: Exponential backoff — 3 attempts with delays of 1s, 2s, 4s.
- **Reconnect**: Automatic reconnection when a connection is lost due to idle timeout (~30 min on iCloud). The pool should detect stale connections before reuse (e.g., via NOOP) and replace them transparently.
- **Exceptions**: Raise `IMAPConnectionError` or `IMAPAuthenticationError` after retries are exhausted.

### SMTP

- **Retry**: Simple retry — 2 attempts (connection is ephemeral, failures are usually transient).
- **Exceptions**: Raise `SMTPSendError` with the original error context.

### Exception Hierarchy

```python
class ICloudMailError(Exception): ...
class IMAPConnectionError(ICloudMailError): ...
class IMAPAuthenticationError(ICloudMailError): ...
class SMTPSendError(ICloudMailError): ...
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
- `pydantic-settings` — env var loading with validation
- `python-dotenv` — `.env` file support in development
- `ruff`, `mypy`, `pytest`, `pytest-asyncio` — dev dependencies
