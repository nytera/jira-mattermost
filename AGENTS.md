# Repository Guidelines

Guidance for AI agents and contributors working in this repo. User-facing
setup, workflow, and operations live in `README.md` (Russian); this file is the
technical/architecture reference and the style/PR conventions.

A `Mattermost alert → Jira incident` bridge bot. Python 3.11+, FastAPI + async
httpx, SQLAlchemy 2.0.

## Build, Test, and Development Commands

- `python -m venv .venv && source .venv/bin/activate`: create and enter a local virtual environment.
- `pip install -e ".[test]"`: editable install with pytest dependencies.
- `python -m mm_jira_bot`: run the bot locally on `0.0.0.0:8080`, reads `.env`.
- `curl http://localhost:8080/healthz`: check the local FastAPI health endpoint.
- `pytest`: full suite (`asyncio_mode=auto`, `pythonpath=src`).
- `pytest tests/test_service.py::test_<name>`: run a single test.
- `docker compose up --build`: build and run the bot with Postgres.

No linter/formatter is configured.

## Architecture

Single process. `web.py:create_app()` builds the FastAPI app and, in its
lifespan, launches two background asyncio tasks against one
`IncidentBotService`:

- **`websocket_loop`** — connects to Mattermost WS (`websocket_events()`), feeds every event to `service.handle_websocket_event`. Reconnects on failure.
- **`pending_work_loop`** — every `PENDING_WORK_INTERVAL_SECONDS` calls `process_pending_work()` to retry failed Jira creates and pending confirmations from the DB. This is the durability backbone: any partial failure is recovered here.

HTTP endpoints: `GET /healthz` and `POST /mattermost/slash/incident` (the
`/incident` slash command; validates `MATTERMOST_SLASH_TOKEN` if set). When
`DEBUG_ADMIN_ENABLED`, `register_debug_admin` also mounts the SPA at
`GET /debug/admin` plus its JSON API: `summary`, `alerts`, `alerts/{post_id}`,
`POST alerts/{post_id}/jira/recreate`, `POST alerts/create-from-link` (create a
Jira issue from a pasted Band link/post id), and `GET api/logs` (reads the
in-memory `LogRingBuffer` installed by `configure_logging`).

`create_app(service=...)` accepts an injected service — tests pass fakes and a
temp SQLite DB instead of live clients.

### Module responsibilities (`src/mm_jira_bot/`)

Runnable entry point is `src/mm_jira_bot/__main__.py`.

| Module | Role |
|--------|------|
| `service.py` | Orchestration. The only place that coordinates Mattermost + Jira + repository. Read this first. |
| `http.py` | `AsyncApiClient` base for both REST clients: owns the httpx client (`aclose`) and folds per-request retry/HTTP boilerplate into `_retry` / `_request`. |
| `mattermost.py` | Mattermost REST + WebSocket client (extends `AsyncApiClient`); event parsers (`parse_posted_event`, `parse_reaction_event`). |
| `jira.py` | Jira **9.x Data Center / on-prem** REST v2 client (Bearer auth, extends `AsyncApiClient`). Field-name→id resolution, option-value resolution via createmeta. |
| `jira_payload.py` | Pure (I/O-free) Jira payload/description builders + shared option constants; unit-tested directly without a client. |
| `repository.py` | SQLAlchemy model `AlertTicket` + `AlertTicketRepository` (all DB access; mutators go through `_mutate`). |
| `domain.py` | Frozen dataclasses, enums, and timezone helpers. |
| `formatting.py` | Incident-message and Jira-summary text. |
| `retry.py` | `ApiError` + `retry_async` (exponential backoff on 429/5xx only). |
| `logging.py` | `JsonFormatter` / `TextFormatter` + `EventLogger` (`get_logger(__name__)` → `log.info(event, **fields)`); format chosen by `LOG_FORMAT`. |
| `web.py` | FastAPI app factory (`create_app`), background loops, `/healthz` + `/incident` slash. |
| `debug_admin.py` | Optional debug-admin UI/API (`register_debug_admin`), gated by `DEBUG_ADMIN_ENABLED`. |
| `config.py` | `.env` loader + `Settings.from_env()`. |

### Two flows, both idempotent

1. **Alert → Jira issue** (`handle_alert_post`): skip non-alert-channel and own-bot posts → `create_or_get_alert` inserts an `alert_tickets` row (unique `mattermost_post_id`) → `_ensure_jira_issue` creates the Jira issue, stores `jira_issue_key`, and replies in the alert thread with the issue link. The DB row is created *before* the Jira call so a crash mid-create is retried later.
2. **Confirmation → valid incident** (`confirm_incident`, triggered by the `:incident:` reaction or `/incident <permalink>`): posts to the incidents channel, sets Jira `Valid Incident = Валидный`, adds a comment, optional transition, and replies in the alert thread about the status change. If the Jira issue does not exist yet, it is saved as `pending_confirmation` and completed by `pending_work_loop`.

There is also a **lightweight validity path** (`apply_validity_label`, triggered by the two configurable reactions `MATTERMOST_FALSE_INCIDENT_REACTION_NAME` → `Ложный` and `MATTERMOST_EXPECTED_INCIDENT_REACTION_NAME` → `Ожидаемый`). It only sets Jira's `Валидность` field (`JiraClient.set_validity`) and replies in the alert thread — no incidents-channel post, comment, or transition. Last reaction wins: each distinct label overwrites the field; the `validity_label` column guards against re-applying the same label (no duplicate replies). It does **not** touch the `valid_incident` confirmation state machine and is best-effort (no `pending_work_loop` retry) — if the Jira issue is not ready, the update is skipped.

Alert-thread replies (`_post_alert_thread_reply`) are best-effort: they reuse
the alert `post_id` as `root_id`, are guarded once-only by the same early
returns that protect issue creation / confirmation (no extra DB flag), and
swallow `ApiError` so a failed notification never breaks the main flow.

Idempotency keys live in `AlertTicket`: `jira_issue_key`, `incident_post_id`,
`jira_confirmation_comment_added`, plus `creation_status` /
`confirmation_status` state machines. Re-delivered events are no-ops.

### Jira field/option resolution (the non-obvious part)

`JIRA_*_FIELD` settings accept a **human field name** (incl. Russian, e.g.
`Валидность`) or a `customfield_NNNNN` id. The name-based path exists so
operators configure fields by their readable Jira name instead of hunting for
`customfield_*` ids: `JiraClient` resolves names to ids once via
`GET /rest/api/2/field` and caches them. For `select`/`radiobuttons` fields,
allowed option values are likewise fetched from issue-type **createmeta**
(`issue/createmeta/{projectKey}/issuetypes[/{id}]`) and matched
case-insensitively, so option ids don't have to be configured by hand; a
missing option raises a non-retryable `ApiError`. `Valid Incident` is
deliberately **not** sent on create — the intent is for a freshly created issue
to carry Jira's own default value, and the bot only sets it to `Валидный`
later, on confirmation.

`JIRA_START_FIELD` (optional) is a **date-time picker** field set to the alert
arrival time on create via `format_jira_datetime()`. Jira 9.x REST v2 wants ISO
8601 with a `[+-]hhmm` offset (no colon) and mandatory fractional seconds, e.g.
`2026-06-16T14:30:00.000+0300`; the `dd.MM.yyyy HH:mm` seen in the UI is only a
display format. It is not an option field, so createmeta option resolution does
not apply.

### Persistence & timezone

`init_db()` runs `Base.metadata.create_all` at startup, so no migration step is
needed locally; `migrations/001_create_alert_tickets.sql` is the reference
schema, kept aligned with the model by hand. `normalize_database_url` rewrites
`postgres://`/`postgresql://` to `postgresql+psycopg://`. All persisted/displayed
times go through `domain.backend_now()` / `backend_datetime()`, which use the
`INCIDENT_TIMEZONE` (default `Europe/Moscow`) configured once in
`Settings.__post_init__`.

## Coding Style & Naming Conventions

Match nearby style: four-space indentation, type hints,
`from __future__ import annotations`, frozen dataclasses for value objects, and
small modules with explicit responsibilities. Use snake_case for functions,
variables, and module names; PascalCase for classes. Keep async boundaries clear
for Mattermost, Jira, and service methods. No formatter or linter is configured,
so keep diffs focused and consistent with nearby code.

## Testing Guidelines

pytest + pytest-asyncio (`asyncio_mode = "auto"` — no `@pytest.mark.asyncio`
needed). Name test files `test_*.py` and test functions `test_<behavior>`. Use
fake Mattermost/Jira clients and a temp SQLite DB (see `tests/test_service.py`)
to avoid live Mattermost, Jira, or Postgres dependencies. Add/extend tests for
any behavior change, especially idempotency, retry/recovery, slash commands, and
Jira payload/option formatting.

## Commit & Pull Request Guidelines

Git history uses concise, imperative commit subjects (e.g.
`Add debug admin for alert ticket operations`). Keep commits small and focused.
Pull requests should describe the behavior change, list verification commands and
results, note configuration or migration impact, and link related issues.
Include screenshots or request/response examples only when changing user-visible
Mattermost messages or HTTP behavior.

## Security & Configuration

All config comes from env vars (`config.py`, loaded from `.env`). Required and
optional vars are listed in `README.md` / `.env.example`. Copy `.env.example` to
`.env` for local development and never commit real tokens; treat Jira API tokens,
Mattermost tokens, channel IDs, and database URLs as secrets. Notable defaults:
`ENABLE_BACKFILL_ON_STARTUP=false` (bot only processes new WS events, not channel
history), `ENABLE_WEBSOCKET=true`. When changing schema behavior, keep
`migrations/001_create_alert_tickets.sql`, the SQLAlchemy model, and startup
initialization expectations aligned.
