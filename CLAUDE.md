# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A `Mattermost alert → Jira incident` bridge bot. Python 3.11+, FastAPI + async httpx, SQLAlchemy 2.0. See `README.md` (Russian, full workflow/setup) and `AGENTS.md` (style, PR/commit conventions) for details not repeated here.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"      # editable install + pytest deps
python -m mm_jira_bot         # run locally on 0.0.0.0:8080, reads .env
curl http://localhost:8080/healthz
pytest                        # full suite (asyncio_mode=auto, pythonpath=src)
pytest tests/test_service.py::test_<name>   # single test
docker compose up --build     # bot + Postgres
```

No linter/formatter is configured. Match nearby style: 4-space indent, type hints, `from __future__ import annotations`, frozen dataclasses for value objects.

## Architecture

Single process. `web.py:create_app()` builds the FastAPI app and, in its lifespan, launches two background asyncio tasks against one `IncidentBotService`:

- **`websocket_loop`** — connects to Mattermost WS (`websocket_events()`), feeds every event to `service.handle_websocket_event`. Reconnects on failure.
- **`pending_work_loop`** — every `PENDING_WORK_INTERVAL_SECONDS` calls `process_pending_work()` to retry failed Jira creates and pending confirmations from the DB. This is the durability backbone: any partial failure is recovered here.

HTTP endpoints: `GET /healthz` and `POST /mattermost/slash/incident` (the `/incident` slash command; validates `MATTERMOST_SLASH_TOKEN` if set).

`create_app(service=...)` accepts an injected service — tests pass fakes and a temp SQLite DB instead of live clients.

### Module responsibilities (`src/mm_jira_bot/`)

| Module | Role |
|--------|------|
| `service.py` | Orchestration. The only place that coordinates Mattermost + Jira + repository. Read this first. |
| `mattermost.py` | Mattermost REST + WebSocket client; event parsers (`parse_posted_event`, `parse_reaction_event`). |
| `jira.py` | Jira Data Center REST v2 client (Bearer auth). Field-name→id resolution, option-value resolution via createmeta, issue payload building. |
| `repository.py` | SQLAlchemy model `AlertTicket` + `AlertTicketRepository` (all DB access). |
| `domain.py` | Frozen dataclasses, enums, and timezone helpers. |
| `formatting.py` | Incident-message and Jira-summary text. |
| `retry.py` | `ApiError` + `retry_async` (exponential backoff on 429/5xx only). |
| `config.py` | `.env` loader + `Settings.from_env()`. |

### Two flows, both idempotent

1. **Alert → Jira issue** (`handle_alert_post`): skip non-alert-channel and own-bot posts → `create_or_get_alert` inserts an `alert_tickets` row (unique `mattermost_post_id`) → `_ensure_jira_issue` creates the Jira issue, stores `jira_issue_key`, and replies in the alert thread with the issue link. The DB row is created *before* the Jira call so a crash mid-create is retried later.
2. **Confirmation → valid incident** (`confirm_incident`, triggered by the `:incident:` reaction or `/incident <permalink>`): posts to the incidents channel, sets Jira `Valid Incident = Валидный`, adds a comment, optional transition, and replies in the alert thread about the status change. If the Jira issue does not exist yet, it is saved as `pending_confirmation` and completed by `pending_work_loop`.

Alert-thread replies (`_post_alert_thread_reply`) are best-effort: they reuse the alert `post_id` as `root_id`, are guarded once-only by the same early returns that protect issue creation / confirmation (no extra DB flag), and swallow `ApiError` so a failed notification never breaks the main flow.

Idempotency keys live in `AlertTicket`: `jira_issue_key`, `incident_post_id`, `jira_confirmation_comment_added`, plus `creation_status` / `confirmation_status` state machines. Re-delivered events are no-ops.

### Jira field/option resolution (the non-obvious part)

`JIRA_*_FIELD` settings accept a **human field name** (incl. Russian, e.g. `Валидность`) or a `customfield_NNNNN` id. The name-based path exists so operators configure fields by their readable Jira name instead of hunting for `customfield_*` ids: `JiraClient` resolves names to ids once via `GET /rest/api/2/field` and caches them. For `select`/`radiobuttons` fields, allowed option values are likewise fetched from issue-type **createmeta** (`issue/createmeta/{projectKey}/issuetypes[/{id}]`) and matched case-insensitively, so option ids don't have to be configured by hand; a missing option raises a non-retryable `ApiError`. `Valid Incident` is deliberately **not** sent on create — the intent is for a freshly created issue to carry Jira's own default value, and the bot only sets it to `Валидный` later, on confirmation.

### Persistence & timezone

`init_db()` runs `Base.metadata.create_all` at startup, so no migration step is needed locally; `migrations/001_create_alert_tickets.sql` is the reference schema, kept aligned with the model by hand. `normalize_database_url` rewrites `postgres://`/`postgresql://` to `postgresql+psycopg://`. All persisted/displayed times go through `domain.backend_now()` / `backend_datetime()`, which use the `INCIDENT_TIMEZONE` (default `Europe/Moscow`) configured once in `Settings.__post_init__`.

## Configuration & secrets

All config comes from env vars (`config.py`, loaded from `.env`). Required and optional vars are listed in `README.md` / `.env.example`. Never commit real tokens. Notable defaults: `ENABLE_BACKFILL_ON_STARTUP=false` (bot only processes new WS events, not channel history), `ENABLE_WEBSOCKET=true`.

## Testing

pytest + pytest-asyncio (`asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed). Tests use fake Mattermost/Jira clients and a temp SQLite DB (see `tests/test_service.py`). Add/extend tests for any behavior change, especially idempotency, retry/recovery, slash commands, and Jira payload/option formatting.
