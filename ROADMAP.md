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

---

## iCloud Mail MCP — Planned Features

Features planejadas para evolucao do servidor MCP. Cada phase implementa uma feature completa (codigo + testes + verificacao), seguindo o mesmo padrao das phases 0–7.

---

## Phase 8: mark_as_read / mark_as_unread

**Objetivo**: Marcar mensagens como lidas ou nao-lidas pelo UID via IMAP STORE flags.

**Arquivos modificados**:
- `src/icloud_mail_mcp/imap_client.py` — novos metodos `mark_as_read(folder, uid)` e `mark_as_unread(folder, uid)` no `IMAPClient`. Usam `STORE +FLAGS (\Seen)` e `STORE -FLAGS (\Seen)`.
- `src/icloud_mail_mcp/server.py` — 2 novos tools: `mark_as_read(ctx, folder, uid)` e `mark_as_unread(ctx, folder, uid)`.
- `tests/test_imap_client.py` — testes para ambos os metodos (sucesso, UID inexistente).
- `tests/test_server.py` — testes para os 2 tool handlers.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## Phase 9: flag_email / unflag_email

**Objetivo**: Adicionar ou remover a flag de destaque (star) em mensagens pelo UID.

**Arquivos modificados**:
- `src/icloud_mail_mcp/imap_client.py` — novos metodos `flag_email(folder, uid)` e `unflag_email(folder, uid)`. Usam `STORE +FLAGS (\Flagged)` e `STORE -FLAGS (\Flagged)`.
- `src/icloud_mail_mcp/server.py` — 2 novos tools: `flag_email(ctx, folder, uid)` e `unflag_email(ctx, folder, uid)`.
- `tests/test_imap_client.py` — testes para ambos os metodos.
- `tests/test_server.py` — testes para os 2 tool handlers.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## Phase 10: bulk_action

**Objetivo**: Aplicar acoes em lote (move, delete, mark_read, mark_unread, flag, unflag) a multiplos UIDs de uma vez.

**Arquivos modificados**:
- `src/icloud_mail_mcp/imap_client.py` — novo metodo `bulk_action(folder, uids: list[str], action: str, destination: str | None = None) -> dict`. Executa a acao usando UID set no IMAP (e.g., `1,2,3`). Retorna `{"success_count": int, "fail_count": int}`.
- `src/icloud_mail_mcp/server.py` — novo tool `bulk_action(ctx, folder, uids, action, destination?)`.
- `tests/test_imap_client.py` — testes para cada tipo de acao, UIDs invalidos, e lista vazia.
- `tests/test_server.py` — teste para o tool handler.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## Phase 11: sort_order em list_emails

**Objetivo**: Parametro `sort_order` para controlar ordem de listagem (ascendente/descendente por data).

**Arquivos modificados**:
- `src/icloud_mail_mcp/imap_client.py` — parametro `sort_order: str = "desc"` em `list_emails()`. `"desc"` = mais recentes primeiro (atual), `"asc"` = mais antigos primeiro.
- `src/icloud_mail_mcp/server.py` — parametro `sort_order` propagado no tool `list_emails`.
- `tests/test_imap_client.py` — testes para ambas as direcoes de ordenacao.
- `tests/test_server.py` — teste do handler com sort_order.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## ~~Phase 12: total_count em list_emails~~ ✓

**Objetivo**: Incluir contagem total de emails na resposta de `list_emails` para suporte a paginacao.

**Arquivos modificados**:
- `src/icloud_mail_mcp/models.py` — novo modelo `EmailListResult` com campos `emails: list[Email]` e `total_count: int`.
- `src/icloud_mail_mcp/imap_client.py` — `list_emails()` retorna `EmailListResult` em vez de `list[Email]`. `total_count` reflete o total de mensagens na folder, independente de limit/offset.
- `src/icloud_mail_mcp/server.py` — tool `list_emails` adapta retorno para incluir `total_count`.
- `tests/test_imap_client.py` — testes verificando `total_count` correto com diferentes limit/offset.
- `tests/test_server.py` — teste do handler verificando campo `total_count`.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## Phase 13: Filtros adicionais em search_emails

**Objetivo**: Novos parametros de busca: status de leitura, flag, tamanho minimo e presenca de anexos.

**Arquivos modificados**:
- `src/icloud_mail_mcp/models.py` — novos campos opcionais no `SearchQuery`: `is_read: bool | None`, `is_flagged: bool | None`, `min_size: int | None`, `has_attachments: bool | None`.
- `src/icloud_mail_mcp/imap_client.py` — `_build_search_criteria()` mapeia: `is_read` → SEEN/UNSEEN, `is_flagged` → FLAGGED/UNFLAGGED, `min_size` → LARGER, `has_attachments` → heuristica via Content-Type. Combinados com AND.
- `src/icloud_mail_mcp/server.py` — parametros propagados no tool `search_emails`.
- `tests/test_imap_client.py` — testes para cada filtro isolado e em combinacao.
- `tests/test_server.py` — teste do handler com novos parametros.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## Phase 14: download_attachment

**Objetivo**: Download de um anexo especifico pelo UID da mensagem e nome do arquivo.

**Arquivos modificados**:
- `src/icloud_mail_mcp/imap_client.py` — novo metodo `download_attachment(folder, uid, filename) -> dict`. Faz FETCH RFC822, localiza o attachment pelo filename, retorna `{"filename": str, "content_type": str, "data": str}` com conteudo em base64. Raises erro se o anexo nao existir.
- `src/icloud_mail_mcp/server.py` — novo tool `download_attachment(ctx, folder, uid, filename)`.
- `tests/test_imap_client.py` — testes com mock de mensagem multipart (anexo encontrado, anexo inexistente).
- `tests/test_server.py` — teste para o tool handler.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## Phase 15: list_attachments

**Objetivo**: Listar anexos de uma mensagem sem buscar o corpo completo do email.

**Arquivos modificados**:
- `src/icloud_mail_mcp/imap_client.py` — novo metodo `list_attachments(folder, uid) -> list[Attachment]`. Usa FETCH BODYSTRUCTURE para obter metadata (filename, content_type, size) sem download do conteudo.
- `src/icloud_mail_mcp/server.py` — novo tool `list_attachments(ctx, folder, uid)`.
- `tests/test_imap_client.py` — testes com mock de BODYSTRUCTURE response (com e sem anexos).
- `tests/test_server.py` — teste para o tool handler.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## Phase 16: rename_folder

**Objetivo**: Renomear uma pasta existente no mailbox via IMAP RENAME.

**Arquivos modificados**:
- `src/icloud_mail_mcp/imap_client.py` — novo metodo `rename_folder(old_name, new_name) -> Folder`. Usa comando IMAP RENAME. Raises `IMAPConnectionError` se a pasta nao existir.
- `src/icloud_mail_mcp/server.py` — novo tool `rename_folder(ctx, old_name, new_name)`.
- `tests/test_imap_client.py` — testes cobrindo sucesso e pasta inexistente.
- `tests/test_server.py` — teste para o tool handler.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## Phase 17: delete_folder

**Objetivo**: Remover uma pasta do mailbox via IMAP DELETE.

**Arquivos modificados**:
- `src/icloud_mail_mcp/imap_client.py` — novo metodo `delete_folder(name) -> dict`. Usa comando IMAP DELETE. Raises erro se a pasta nao existir ou contiver mensagens.
- `src/icloud_mail_mcp/server.py` — novo tool `delete_folder(ctx, name)`.
- `tests/test_imap_client.py` — testes cobrindo sucesso, pasta inexistente e pasta nao-vazia.
- `tests/test_server.py` — teste para o tool handler.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## Phase 18: get_folder_stats

**Objetivo**: Retornar contagem total e de mensagens nao-lidas por pasta via IMAP STATUS.

**Arquivos modificados**:
- `src/icloud_mail_mcp/imap_client.py` — novo metodo `get_folder_stats(folder) -> dict`. Usa `STATUS folder (MESSAGES UNSEEN)`. Retorna `{"total": int, "unread": int}`.
- `src/icloud_mail_mcp/server.py` — novo tool `get_folder_stats(ctx, folder)`.
- `tests/test_imap_client.py` — testes com mock de STATUS response.
- `tests/test_server.py` — teste para o tool handler.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## Phase 19: save_draft

**Objetivo**: Salvar um rascunho no servidor sem enviar, usando IMAP APPEND.

**Arquivos modificados**:
- `src/icloud_mail_mcp/imap_client.py` — novo metodo `save_draft(to, subject, body, cc?) -> dict`. Constroi `EmailMessage` e usa IMAP APPEND na pasta `"Drafts"` com flag `\Draft`. Retorna `{"status": "saved", "uid": str}`.
- `src/icloud_mail_mcp/server.py` — novo tool `save_draft(ctx, to, subject, body, cc?)`.
- `tests/test_imap_client.py` — testes com mock de APPEND (sucesso e falha).
- `tests/test_server.py` — teste para o tool handler.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## Phase 20: reply_email / forward_email

**Objetivo**: Responder ou encaminhar emails existentes, referenciando o UID original via headers In-Reply-To/References.

**Arquivos modificados**:
- `src/icloud_mail_mcp/imap_client.py` — helper para buscar Message-ID e headers do email original.
- `src/icloud_mail_mcp/smtp_client.py` — novos metodos `reply_email(original_email, body, reply_all)` e `forward_email(original_email, to, body?)`. Constroem headers In-Reply-To/References. Reply preenche To/Cc automaticamente a partir do original; forward anexa corpo original como citacao.
- `src/icloud_mail_mcp/server.py` — 2 novos tools: `reply_email(ctx, folder, uid, body, reply_all?)` e `forward_email(ctx, folder, uid, to, body?)`.
- `tests/test_smtp_client.py` — testes para reply, reply_all e forward (headers corretos, destinatarios).
- `tests/test_server.py` — testes para os 2 tool handlers.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## Phase 21: list_rules / create_rule

**Objetivo**: Gerenciar regras de filtragem automatica de emails armazenadas localmente.

**Nota**: iCloud IMAP nao suporta SIEVE nativamente. Regras sao armazenadas em arquivo JSON local e aplicadas via execucao manual.

**Arquivos criados/modificados**:
- `src/icloud_mail_mcp/rules.py` — novo modulo. Classe `RulesEngine` que gerencia regras em JSON file (`~/.icloud_mail_mcp/rules.json`). Metodos: `list_rules()`, `create_rule(name, conditions, actions)`, `delete_rule(name)`, `apply_rules(folder, imap_client)`.
- `src/icloud_mail_mcp/models.py` — novos modelos `Rule` e `RuleCondition`.
- `src/icloud_mail_mcp/server.py` — 3 novos tools: `list_rules(ctx)`, `create_rule(ctx, name, conditions, actions)`, `apply_rules(ctx, folder)`.
- `tests/test_rules.py` — novo arquivo de testes. CRUD de regras e aplicacao a mensagens mock.
- `tests/test_server.py` — testes para os 3 tool handlers.

**Verificacao**: `uv run ruff check . && uv run mypy src/ && uv run pytest -v`

---

## Grafo de Dependencias (Phases 8–21)

```
Phase 8  (mark_as_read/unread)     ← IMAP STORE flags basico
Phase 9  (flag/unflag)             ← mesmo padrao da Phase 8
Phase 10 (bulk_action)             ← depende de 8 e 9 (reutiliza logica de flags)
  │
Phase 11 (sort_order)              ← independente
Phase 12 (total_count)             ← independente
Phase 13 (filtros search)          ← independente
  │
Phase 14 (download_attachment)     ← independente
Phase 15 (list_attachments)        ← independente (pode ser paralela com 14)
  │
Phase 16 (rename_folder)           ← independente
Phase 17 (delete_folder)           ← independente
Phase 18 (get_folder_stats)        ← independente
  │
Phase 19 (save_draft)              ← independente
Phase 20 (reply/forward)           ← depende de SMTP + IMAP existentes
  │
Phase 21 (rules/filters)           ← depende de todas as acoes anteriores (aplica move, flag, etc.)
```

---

## Phase 22: iCloud Calendar (CalDAV)

**Objetivo**: Expandir o servidor de "iCloud Mail MCP" para "iCloud MCP", adicionando suporte a Calendar via CalDAV. Visualizar, criar, editar e deletar eventos. Mesma credencial (App-Specific Password) ja usada pelo Mail.

**Decisoes-chave**:
- **Rename completo**: pacote `icloud_mail_mcp` → `icloud_mcp`; servidor FastMCP `"icloud-mcp"`; base de excecoes `ICloudMailError` → `ICloudError` (alias mantido).
- **Cliente async hand-rolled** sobre `httpx.AsyncClient` (sem pool — CalDAV e stateless), no estilo do `imap_client`. `icalendar` para build/parse de `VEVENT`.
- **Discovery em 2 passos** no startup: `current-user-principal` → `calendar-home-set` (resolve o partition host `pXX-caldav.icloud.com`), cacheado.
- **Escopo v1**: campos essenciais (summary, start/end, all_day, location, description). Recorrencia, convidados e alarmes ficam fora de escopo.

**Arquivos criados/modificados**:
- `src/icloud_mcp/exceptions.py` — base `ICloudError` + `CalDAVError`/`CalDAVConnectionError`/`CalDAVAuthenticationError`.
- `src/icloud_mcp/config.py` — `caldav_url`, `caldav_timeout`.
- `src/icloud_mcp/models.py` — novos modelos `Calendar` e `CalendarEvent`.
- `src/icloud_mcp/caldav_client.py` — NOVO. `CalDAVClient` async: `connect()` (discovery), `list_calendars`, `list_events` (REPORT `calendar-query` + `time-range`), `get_event` (REPORT por UID, contornando o `get_object_by_uid` quebrado do iCloud), `create_event`/`update_event` (PUT iCalendar, `If-None-Match`/`If-Match`), `delete_event`. Retry simples + mapeamento de excecoes.
- `src/icloud_mcp/server.py` — `caldav_client` no `AppContext`/lifespan + 6 tools: `list_calendars`, `list_events`, `get_event`, `create_event`, `update_event`, `delete_event`.
- `tests/test_caldav_client.py` — NOVO. Mocks via `httpx.MockTransport` (PROPFIND/REPORT/PUT/DELETE): discovery, partition host, list/get/create/update/delete, 401→auth error, retry. Sem rede real.
- `tests/test_server.py` — fixture com `caldav_client` mockado + testes dos 6 tool handlers; lifespan cobrindo `connect`/`close`.
- `pyproject.toml` — deps `httpx` + `icalendar`; override mypy tratando `icalendar` como modulo opaco.

**Verificacao**: `uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest -v`

---

## Phase 23: Recorrencia de eventos (serie inteira)

**Objetivo**: Suporte a eventos recorrentes no Calendar. iCloud expoe a `RRULE` mas seu `expand` server-side e instavel — entao a expansao e feita **client-side**.

**Resposta a "falha em ver ou em criar/editar?"**: a escrita (PUT de `VEVENT` com `RRULE`) funciona; o problema do iCloud e na **leitura/expansao** (`<C:expand>` erratico, time-range inconsistente). Alem disso, o codigo antigo descartava a `RRULE` no parse — mostrando o recorrente uma unica vez.

**Decisoes-chave**:
- **Expansao client-side** com `recurring-ical-events` (sobre `icalendar` + `python-dateutil`, ja no lock). Nunca usar o `expand` do iCloud.
- **Deteccao de recorrencia vem do VCALENDAR mestre** (VEVENT com `RRULE`/`RDATE`), pois ocorrencias expandidas nao mantem `RRULE` e ate eventos simples recebem `RECURRENCE-ID` da lib.
- **Escopo**: serie inteira. Editar/excluir ocorrencia unica (`RECURRENCE-ID`/`EXDATE`) fica para a Phase 24.

**Arquivos modificados**:
- `src/icloud_mcp/models.py` — `CalendarEvent` ganha `rrule`, `is_recurring`, `recurrence_id`.
- `src/icloud_mcp/caldav_client.py` — `list_events` expande ocorrencias na janela; `get_event` preserva o mestre sem expandir; novos helpers `_recurrence_info`/`_master_component`/`_component_to_event`/`_parse_ics_master`/`_expand_ics`; `create_event`/`update_event` aceitam `rrule` (com `_build_rrule` validando antes do PUT; `rrule=""` no update remove a recorrencia).
- `src/icloud_mcp/server.py` — tools `create_event`/`update_event` propagam `rrule`; docstrings de `list_events`/`update_event` atualizadas.
- `tests/test_caldav_client.py` — expansao semanal, `EXDATE`, override `RECURRENCE-ID`, `get_event` preserva `RRULE`, nao-recorrente sem campos, create com `RRULE` valida/invalida, update remove/mantem recorrencia.
- `tests/test_server.py` — handler `create_event` com `rrule`; asserts ajustados para o novo kwarg.
- `pyproject.toml` — dep `recurring-ical-events`; override mypy para `recurring_ical_events`/`x_wr_timezone`.

**Verificacao**: `uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest -v` (209 passed)

---

## Phase 24: Editar/excluir uma unica ocorrencia

**Objetivo**: Operar em uma ocorrencia isolada de uma serie recorrente, sem afetar as demais. Continuacao da Phase 23 (escopo "serie inteira").

**Modelo CalDAV**: a serie e um unico recurso (.ics) = VEVENT mestre (com RRULE) + zero ou mais VEVENTs override (cada um com RECURRENCE-ID apontando o slot original). Operacoes nunca criam recurso novo — fazem PUT do recurso inteiro com If-Match.
- **Editar 1 ocorrencia** → insere/atualiza um override com RECURRENCE-ID = slot original + campos novos.
- **Excluir 1 ocorrencia** → adiciona EXDATE = slot original ao mestre (e remove o override daquele slot, se houver).

**Decisoes-chave**:
- **Tools dedicadas** `update_occurrence`/`delete_occurrence` (mais explicito que um parametro opcional; evita editar a serie inteira por engano).
- A ocorrencia e endereçada por **`uid` + `recurrence_id`** (o slot original, ja retornado por `list_events` na Phase 23).
- O **tipo** do RECURRENCE-ID/EXDATE (date vs datetime) e derivado do DTSTART do mestre (all-day → VALUE=DATE).
- **Validacao** antes de escrever: serie e recorrente? slot existe (expansao de janela estreita)? Senao, erro claro.
- **Fora de escopo**: "esta e futuras" (THISANDFUTURE).

**Arquivos modificados**:
- `src/icloud_mcp/caldav_client.py` — `_fetch_resource` (REPORT por UID → VCALENDAR bruto + href + etag); metodos `update_occurrence` (upsert override) e `delete_occurrence` (EXDATE + remove override); helper `_put_resource`; helpers puros `_require_recurring_master`/`_recurrence_id_value`/`_find_override`/`_slot_in_series`/`_new_override`/`_apply_occurrence_fields`/`_as_dt`/`_same_moment`/`_master_duration`/`_set_prop`; `_uid_query_body` extraido (DRY com `_find_event`).
- `src/icloud_mcp/server.py` — tools `update_occurrence`/`delete_occurrence` (parse ISO do recurrence_id).
- `tests/test_caldav_client.py` — adicionar override, upsert override existente, EXDATE, EXDATE removendo override, all-day VALUE=DATE, erro nao-recorrente, erro slot inexistente.
- `tests/test_server.py` — handlers `update_occurrence`/`delete_occurrence`.

**Verificacao**: `uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest -v` (218 passed; 33 tools)
