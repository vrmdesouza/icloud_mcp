# Roadmap de Implementacao: icloud_mail_mcp

Projeto greenfield — servidor MCP em Python que conecta o Claude ao iCloud Mail via IMAP/SMTP. Todos os arquivos serao criados do zero.

---

## Phase 0: Project Scaffolding

**Objetivo**: Estrutura de diretorios, manifesto de dependencias e tooling. `uv sync` deve funcionar.

**Arquivos**:
- `pyproject.toml` — metadata, dependencias (mcp, aioimaplib, aiosmtplib, pydantic-settings, python-dotenv) + dev (ruff, mypy, pytest, pytest-asyncio), config de ferramentas (ruff py312/line-length 100/select E,F,I,UP,B,ASYNC; mypy strict py312; pytest asyncio_mode=auto). Build system: hatchling com `packages = ["src/icloud_mail_mcp"]`
- `src/icloud_mail_mcp/__init__.py` — docstring + `__version__ = "0.1.0"`
- `src/icloud_mail_mcp/__main__.py` — stub: `print("server not yet implemented")`
- `tests/__init__.py` — vazio
- `.env.example` — ICLOUD_EMAIL, ICLOUD_APP_PASSWORD, IMAP_POOL_SIZE=3, IMAP_TIMEOUT=30
- `.gitignore` — Python + uv + .env
- `.python-version` — `3.12`

**Verificacao**: `uv sync --all-extras && uv run ruff check . && uv run mypy src/ && uv run python -m icloud_mail_mcp`

---

## Phase 1: Config + Exceptions

**Objetivo**: Modulos-folha sem dependencias internas. Validacao de env vars e hierarquia de excecoes.

### `src/icloud_mail_mcp/exceptions.py`
```
ICloudMailError(Exception)
├── IMAPConnectionError
├── IMAPAuthenticationError
└── SMTPSendError
```

### `src/icloud_mail_mcp/config.py`
- Classe `ICloudMailSettings(BaseSettings)` com pydantic-settings
- Campos obrigatorios: `icloud_email: str`, `icloud_app_password: str`
- Campos opcionais: `imap_pool_size: int = 3`, `imap_timeout: int = 30`
- Constantes derivadas: `imap_host`, `imap_port`, `smtp_host`, `smtp_port`
- `SettingsConfigDict(env_file=".env", case_sensitive=False)`
- Funcao `get_settings() -> ICloudMailSettings`

**Verificacao**: `ICLOUD_EMAIL=x ICLOUD_APP_PASSWORD=y uv run python -c "from icloud_mail_mcp.config import get_settings; print(get_settings().icloud_email)"`

---

## Phase 2: Pydantic Models

**Objetivo**: Modelos de dados compartilhados entre todos os modulos.

### `src/icloud_mail_mcp/models.py`

| Modelo | Campos principais |
|--------|------------------|
| `Folder` | name, delimiter="/", flags=[] |
| `Attachment` | filename, content_type, size? |
| `Email` | uid, folder, subject, sender, to[], cc[], date?, body_text, body_html, is_read, attachments[] |
| `SearchQuery` | folder="INBOX", sender?, subject?, since?, before?, body?, limit=20 (1-100) |

**Verificacao**: `uv run mypy src/icloud_mail_mcp/models.py`

---

## Phase 3: IMAP Client (modulo mais complexo)

**Objetivo**: Pool de conexoes IMAP persistente + todas as operacoes de leitura/busca/gerenciamento.

### `src/icloud_mail_mcp/imap_client.py`

**Imports internos**: config, models, exceptions

**Classe `IMAPConnectionPool`**:
- `__init__(settings)` — cria `asyncio.Queue` com `maxsize=pool_size`
- `initialize()` — cria conexoes iniciais e preenche a queue
- `close()` — logout e fecha todas as conexoes
- `_create_connection()` — IMAP4_SSL + wait_hello + login. Auth errors -> IMAPAuthenticationError
- `_health_check(conn)` — NOOP, retorna False se stale
- `acquire()` — context manager async. Pega da queue, health check, reconecta se necessario, devolve ao final
- `_retry_operation(fn)` — exponential backoff 3 tentativas (1s, 2s, 4s)

**Classe `IMAPClient`**:
- `__init__(pool: IMAPConnectionPool)`
- `list_folders() -> list[Folder]` — `conn.list('', '*')`, parse resposta
- `list_emails(folder, limit=20, offset=0) -> list[Email]` — select, search ALL, reverse UIDs (newest first), slice, fetch headers
- `get_email(folder, uid) -> Email` — fetch RFC822 completo, parse com `email` stdlib
- `search_emails(query: SearchQuery) -> list[Email]` — monta string SEARCH (FROM, SUBJECT, SINCE DD-Mon-YYYY, BEFORE, BODY), AND implicito
- `move_email(folder, uid, destination) -> dict` — COPY + store \\Deleted + EXPUNGE
- `delete_email(folder, uid) -> dict` — chama move_email para "Deleted Messages" (nome do Trash no iCloud)
- `create_folder(name) -> Folder` — conn.create

**Helper privado `_parse_email(raw_bytes, uid, folder) -> Email`**:
- `email.message_from_bytes` + decode headers RFC 2047
- Multipart: extrai text/plain e text/html
- Attachments: extrai metadata (filename, content-type, size)
- Tolerante a campos malformados

**Detalhes criticos**:
- Formato de data IMAP SEARCH: `DD-Mon-YYYY` via `strftime("%d-%b-%Y")`
- Trash folder no iCloud = `"Deleted Messages"` (nao "Trash")
- Gerenciamento de estado: conexao fica em SELECTED apos select()

**Verificacao**: `uv run ruff check src/icloud_mail_mcp/imap_client.py && uv run mypy src/icloud_mail_mcp/imap_client.py`

---

## Phase 4: SMTP Client

**Objetivo**: Cliente SMTP stateless — conexao nova por envio.

### `src/icloud_mail_mcp/smtp_client.py`

**Imports internos**: config, exceptions

**Classe `SMTPClient`**:
- `__init__(settings: ICloudMailSettings)`
- `send_email(to, subject, body, cc?, bcc?) -> dict` — 2 tentativas
  - Constroi `EmailMessage` com From/To/Subject/Cc (BCC so no envelope, NAO no header)
  - `SMTP(hostname, port=587, start_tls=True)` — STARTTLS (nao use_tls)
  - Login + send_message
  - Retorna `{"status": "sent", "message_id": "..."}`
  - Raises `SMTPSendError` apos 2 falhas
- `_build_message()` — helper para construir EmailMessage

**Verificacao**: `uv run ruff check src/icloud_mail_mcp/smtp_client.py && uv run mypy src/icloud_mail_mcp/smtp_client.py`

---

## Phase 5: MCP Server + Entry Point

**Objetivo**: Registrar os 8 tools no FastMCP, lifespan para o pool IMAP.

### `src/icloud_mail_mcp/server.py`

**Imports internos**: config, models, imap_client, smtp_client

**Lifespan** (`app_lifespan`):
- Cria settings, pool, inicializa pool
- Yield `AppContext(imap_client=IMAPClient(pool), smtp_client=SMTPClient(settings))`
- Finally: pool.close()

**`AppContext` dataclass**: imap_client + smtp_client

**8 tools** via `@mcp.tool()`:
| Tool | Delega para |
|------|------------|
| `list_folders(ctx)` | imap_client.list_folders() |
| `list_emails(ctx, folder, limit, offset)` | imap_client.list_emails() |
| `get_email(ctx, folder, uid)` | imap_client.get_email() |
| `search_emails(ctx, folder, sender?, subject?, since?, before?, body?, limit)` | imap_client.search_emails() |
| `send_email(ctx, to, subject, body, cc?, bcc?)` | smtp_client.send_email() |
| `move_email(ctx, folder, uid, destination)` | imap_client.move_email() |
| `delete_email(ctx, folder, uid)` | imap_client.delete_email() |
| `create_folder(ctx, name)` | imap_client.create_folder() |

**Decisoes**:
- Retornos sao `dict`/`list[dict]` (`.model_dump()`) para serializacao JSON limpa
- Datas em `search_emails` chegam como `str | None`, parse com `date.fromisoformat()`
- Zero logica IMAP/SMTP no server.py — apenas delegacao

### `src/icloud_mail_mcp/__main__.py`
- Import `mcp` de server.py
- `mcp.run(transport="stdio")`
- Configura logging basico

**Verificacao**: `uv run ruff check . && uv run mypy src/`

---

## Phase 6: Tests

**Objetivo**: Cobertura completa com mocks. Nenhuma conexao real.

### `tests/conftest.py`
Fixtures compartilhados:
- `settings` — ICloudMailSettings com credenciais fake
- `mock_imap_connection` — AsyncMock com todos os metodos IMAP
- `mock_smtp` — AsyncMock com context manager

### `tests/test_config.py` (4 tests)
- Carrega settings com env vars validas
- Falha se ICLOUD_EMAIL ausente
- Falha se ICLOUD_APP_PASSWORD ausente
- Valores default (pool_size=3, timeout=30)

### `tests/test_models.py` (4 tests)
- Email defaults (listas vazias, strings vazias)
- Email serialization roundtrip
- SearchQuery limit validation (1-100)
- Folder model basico

### `tests/test_imap_client.py` (17 tests)
- Pool: initialize, acquire, stale replacement, close
- Retry: sucesso na 2a tentativa, esgotamento raises error
- Auth failure raises IMAPAuthenticationError
- list_folders parse
- list_emails pagination + empty folder
- get_email full parse + malformed graceful
- search_emails criteria building + no results
- move_email (copy + delete)
- delete_email (move to trash)
- create_folder

### `tests/test_smtp_client.py` (6 tests)
- Send success
- Message headers corretos
- BCC nao aparece nos headers
- Retry on first failure
- Raises apos retries
- Recipients incluem cc e bcc

### `tests/test_server.py` (9 tests)
- Um test por tool handler (mocked clients)
- search_emails parse de date strings
- Lifespan inicializa e fecha pool

**Verificacao**: `uv run pytest -v && uv run ruff check tests/ && uv run mypy tests/`

---

## Phase 7: Polish + Integracao Final

**Objetivo**: Verificacao end-to-end, logging, preparacao para Claude Desktop.

**Tarefas**:
1. Logging config em `__main__.py` — `logging.basicConfig(level=INFO)`
2. `src/icloud_mail_mcp/py.typed` — marker PEP 561 (arquivo vazio)
3. `git init` + primeiro commit
4. Suite completa de verificacao:
   ```
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src/
   uv run pytest -v
   uv run python -m icloud_mail_mcp  # deve iniciar e esperar stdin
   ```

---

## Grafo de Dependencias

```
Phase 0 (scaffolding)
  │
  v
Phase 1 (config + exceptions)     ← sem deps internas
  │
  v
Phase 2 (models)                   ← sem deps internas
  │
  v
Phase 3 (imap_client)             ← imports: config, models, exceptions
  │
Phase 4 (smtp_client)             ← imports: config, exceptions (paralelo com Phase 3 possivel)
  │
  v
Phase 5 (server + __main__)       ← imports: tudo
  │
  v
Phase 6 (tests)                   ← testa tudo
  │
  v
Phase 7 (polish)                  ← integracao final
```

## Mapa de Imports (DAG limpo, sem circularidade)

```
exceptions.py  ←  nada
config.py      ←  nada
models.py      ←  nada
imap_client.py ←  config, models, exceptions
smtp_client.py ←  config, exceptions
server.py      ←  config, models, imap_client, smtp_client
__main__.py    ←  server
```
