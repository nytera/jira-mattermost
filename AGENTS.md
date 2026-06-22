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
- `pytest --cov=mm_jira_bot --cov-report=term-missing`: suite with coverage report (baseline ~78%).
- `ruff check src tests`: lint (rules `E,F,I,UP,B,SIM`; config in `pyproject.toml`).
- `ruff format src tests`: apply formatting (`--check` to verify without writing).
- `docker compose up --build`: build and run the bot with Postgres.

Lint/format via `ruff`, coverage via `pytest-cov` (both in the `[test]` extra). `ruff`
config lives in `pyproject.toml`; `debug_admin.py`, `jira_payload.py`, and
`postmortem.py` ignore `E501` because they hold long unbreakable literals
(embedded SPA CSS/HTML/JS and Russian PM templates).

## Architecture

Single process. `web.py:create_app()` builds the FastAPI app and, in its
lifespan, launches two background asyncio tasks against one
`IncidentBotService`:

- **`run_startup_preflight`** — logs sanitized startup configuration and runs
  non-fatal dependency checks before background work starts. It checks DB
  access, Mattermost `/users/me` + configured channels, Jira field/issue-type
  metadata and required option values, and the optional LLM chat-completions
  smoke request. Successful Jira checks warm field/createmeta caches.
- **`websocket_loop`** — connects to Mattermost WS (`websocket_events()`), feeds every event to `service.handle_websocket_event`. Reconnects on failure.
- **`pending_work_loop`** — every `PENDING_WORK_INTERVAL_SECONDS` calls `process_pending_work()` to retry failed Jira creates and pending confirmations from the DB. This is the durability backbone: any partial failure is recovered here.

HTTP endpoints: `GET /healthz`, `POST /mattermost/slash/incident` (the
`/incident` slash command; validates `MATTERMOST_SLASH_TOKEN` if set), and
`POST /mattermost/actions/alert` (interactive-button callback → calls
`service.handle_alert_action`; Mattermost does not sign button callbacks, so this
endpoint relies on network isolation rather than a token), and
`POST /mattermost/dialogs/feedback` (interactive-dialog submission → stores
feedback and replies in the alert thread). When
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
| `actions.py` | Pure builders/constants for the interactive alert attachments/controls (`context`, menu options, button labels); no I/O. |
| `summary.py` | Pure builders for the LLM thread-summary prompt and the thread reply. |
| `retry.py` | `ApiError` + `retry_async` (exponential backoff on 429/5xx only). |
| `logging.py` | `JsonFormatter` / `TextFormatter` + text-only INFO filtering + `EventLogger` (`get_logger(__name__)` → `log.info(event, **fields)`); format chosen by `LOG_FORMAT`. |
| `web.py` | FastAPI app factory (`create_app`), background loops, `/healthz` + `/incident` slash. |
| `debug_admin.py` | Optional debug-admin UI/API (`register_debug_admin`), gated by `DEBUG_ADMIN_ENABLED`. |
| `config.py` | `.env` loader + `Settings.from_env()`. |

### Two flows, both idempotent

1. **Alert → Jira issue** (`handle_alert_post`): skip non-alert-channel and own-bot posts → `create_or_get_alert` inserts an `alert_tickets` row (unique `mattermost_post_id`) → `_ensure_jira_issue` creates the Jira issue, stores `jira_issue_key`, and replies in the alert thread with the issue link. The DB row is created *before* the Jira call so a crash mid-create is retried later.
2. **Confirmation → valid incident** (`confirm_incident`, triggered by the `:incident:` reaction or `/incident <permalink>`): posts to the incidents channel, sets Jira `Valid Incident = Валидный`, adds a comment, optional transition, and replies in the alert thread about the status change. If the Jira issue does not exist yet, it is saved as `pending_confirmation` and completed by `pending_work_loop`.

When `JIRA_CREATE_ENABLED=false` (test mode), the `JiraClient` makes **no Jira
calls for issue-key operations**: `create_issue`/`create_postmortem_issue` return
a stub `JiraIssue` (`stub_jira_issue` — `JIRA_STUB_ISSUE_KEY` plus a
Mattermost-post-id suffix for DB uniqueness, or a generated
`{JIRA_PROJECT_KEY}-12345`-style key), and `get_valid_incident` / `set_validity`
/ `set_valid_incident` / `set_end_time` / `set_description` / `add_comment` /
`transition_issue` are no-ops. This matters because the stub key does not exist in
Jira — without the no-op, those calls would 404 and abort `confirm_incident`
(after the incident-channel post but before the alert-thread reply). Mattermost
issue-created replies display the clean configured `JIRA_STUB_ISSUE_KEY` via
`_display_jira_issue`. Field/option metadata reads (global, not issue-scoped) are
not stubbed.

There is also a **lightweight validity path** (`apply_validity_label`, triggered by the two configurable reactions `MATTERMOST_FALSE_INCIDENT_REACTION_NAME` → `Ложный` and `MATTERMOST_EXPECTED_INCIDENT_REACTION_NAME` → `Ожидаемый`). It sets Jira's `Валидность` field (`JiraClient.set_validity`), optionally sets `JIRA_END_FIELD` to the reaction time, and replies in the alert thread — no incidents-channel post, comment, or transition. Last reaction wins: each distinct label overwrites the field; the `validity_label` column guards against re-applying the same label (no duplicate replies). It does **not** touch the `valid_incident` confirmation state machine and is best-effort (no `pending_work_loop` retry) — if the Jira issue is not ready, the update is skipped.

Alert-thread replies (`_post_alert_thread_reply`) are best-effort: they reuse
the alert `post_id` as `root_id`, are guarded once-only by the same early
returns that protect issue creation / confirmation (no extra DB flag), and
swallow `ApiError` so a failed notification never breaks the main flow.

**Interactive buttons/menu** (`handle_alert_action`) are an alternative entry
point to the same two flows plus a thread summary. The bot can't attach controls
to the alert (a Grafana/user post), so it hangs them on its own issue-created
reply via `_alert_action_attachments` (only when `_interactive_controls_enabled()`
— `SERVICE_PUBLIC_URL` set and `INTERACTIVE_BUTTONS_ENABLED` not `false`; emoji
reactions stay as the fallback, and `INTERACTIVE_BUTTONS_ENABLED=false` forces that
emoji-only mode for every card). Current UI is a single thread reply with
two stacked attachment blocks: a blue (`#3B82F6`) main card with bold
`Создана задача`, the `Выбрать валидность ▼` menu, and the `🚨 Инцидент` /
`📝 Summary` buttons under it, then a separate gray (`#4B5563`) card below with
`💬 Обратная связь по алерту`. Each control posts to
`/mattermost/actions/alert` with a `context` identifying the action and the alert
`post_id`. Dispatch: the `validity` message menu carries `selected_option`
`false` / `expected` / `valid` → `apply_validity_label` (`Ложный` /
`Ожидаемый` / `Валидный`); `incident` → `confirm_incident`; `summary` →
`generate_thread_summary` (LLM, posts a visible thread reply; no-op when LLM is
unconfigured); `feedback` → `open_feedback_dialog`. The dialog submit stores a
row in `alert_feedback` and posts `Получили обратную связь от username` in the
alert thread. The action endpoint returns `ephemeral_text` feedback to the
clicker.

**Authorized-user allowlist** (optional). `MATTERMOST_AUTHORIZED_USERNAMES`
(comma-separated logins) restricts which users' reactions/clicks the bot acts
on. `resolve_authorized_users` (called in the lifespan after preflight) resolves
the logins to ids via `MattermostClient.get_user_ids_by_usernames`
(`POST /api/v4/users/usernames`) and enables `_is_authorized`, which gates
`handle_reaction` (covers the checkmark/postmortem path too), `handle_alert_action`
(except `feedback`, which stays open to all), and `handle_slash_command`. Empty
list -> gate disabled (act on everyone). Partial resolution logs the unresolved
logins; total resolution failure is **fail-open** (the endpoint already relies on
network isolation, so a Mattermost hiccup must not brick incident tooling).

For a per-channel/per-control behavior matrix (which reaction/button does what,
where, and whether it is gated), see the "Сводка: что на что влияет" table in
`README.md` — keep it as the single source of truth instead of duplicating it.

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

`JIRA_END_FIELD` (optional) is a **date-time picker** field set to the reaction
time when the lightweight `Ложный` / `Ожидаемый` validity path runs. For valid
incidents, it is not updated by the confirmation flow; it is updated later when
someone adds a Mattermost checkmark reaction (`white_check_mark`,
`heavy_check_mark`, or `ballot_box_with_check`) on the incident thread root post.
When `LLM_API_TOKEN` is configured, the same checkmark also generates a
postmortem from the full incident thread through the OpenAI-compatible
`LLM_BASE_URL`, keeps Jira description as a PM template with the incident root
link, postmortem author, and participants, adds the generated report as a Jira
comment, and posts a short summary back to the incident thread. A checkmark on
an unmapped manual incident thread root post creates a Jira issue with a
PM-template description, but it does not set the alert-only source/is-crit-alert
fields. Checkmarks on incident thread replies are ignored.

### Manual incidents: button card (incident channel)

Alongside the checkmark, a button-driven flow handles incidents typed **directly**
in `MATTERMOST_INCIDENT_CHANNEL_ID` (needs `SERVICE_PUBLIC_URL`). The WS posted
handler routes incident-channel posts to `handle_manual_incident_post`: for every
**root** post by a real user (not a bot — filtered by `_is_bot_post`, which checks
`props.from_bot` / `props.from_webhook` and `MATTERMOST_BOT_USER_ID`) it pre-creates
the ticket row via `create_or_get_incident_thread` (idempotent) and posts a
"➕ Создать задачу" card. No Jira issue yet. The card's controls carry
`context.source = "incident"` + `incident_post_id`, so `handle_alert_action` branches
early to `handle_incident_action` (keyed by `incident_post_id`, skips the alert-channel
checks). Actions: `create_task` → `create_postmortem_issue` (no alert fields) and the
action response's `update` payload swaps the card for the controls (validity menu,
"🏁 Завершить", "📝 Саммари"); `validity` → `apply_validity_label`;
`end_incident` → reuses `handle_incident_checkmark` (full PM); `summary` →
`generate_thread_summary` (light). The checkmark flow stays available in parallel.

The incident details (title, Jira link, alert link, confirmer `@mention`, time)
render in a **gray attachment block** (`FEEDBACK_ATTACHMENT_COLOR`) placed *above*
the forwarded alert attachment(s); the post `message` itself is empty. The title
is `##### 🔴 Инцидент открыт` while open; when the incident is ended (button or
checkmark, `INCIDENT_ENDED`), `_mark_incident_post_completed` swaps it to
`##### 🟢 Инцидент закрыт` in the first attachment's text via
`MattermostClient.update_post` (`PUT /api/v4/posts/{id}/patch`, props). Only the bot-authored message is edited —
for a manual incident the "incident post" is the human's own message
(`incident_post_id == mattermost_post_id`), so it is skipped.

Alert-originated incidents get the **same controls** card: when `confirm_incident`
publishes the incident-channel post, `_publish_incident_message_if_needed` also
posts the controls reply (no "Создать задачу" — the issue exists). Here the
incident post id differs from the ticket's `mattermost_post_id` (the alert post),
so the `validity` branch resolves the ticket via `get_by_incident_post_id` and
calls `apply_validity_label` with the ticket's `mattermost_post_id`; the other
actions already look up by incident post id.

Validity and confirmation are **independent axes**: `_ensure_postmortem_jira_issue`
only stamps `Валидный` as a default when `ticket.validity_label is None`, so an
explicit `Ложный`/`Ожидаемый` survives the postmortem/end step (manual *and*
confirmed paths).

### Persistence & timezone

`init_db()` runs `Base.metadata.create_all` at startup and applies small
backward-compatible `ALTER TABLE` additions, so no migration step is needed
locally; files in `migrations/` are the reference schema, kept aligned with the
model by hand. `normalize_database_url` rewrites
`postgres://`/`postgresql://` to `postgresql+psycopg://`. All persisted/displayed
times go through `domain.backend_now()` / `backend_datetime()`, which use the
`INCIDENT_TIMEZONE` (default `Europe/Moscow`) configured once in
`Settings.__post_init__`.

## Coding Style & Naming Conventions

Match nearby style: four-space indentation, type hints,
`from __future__ import annotations`, frozen dataclasses for value objects, and
small modules with explicit responsibilities. Use snake_case for functions,
variables, and module names; PascalCase for classes. Keep async boundaries clear
for Mattermost, Jira, and service methods. Formatting/linting is enforced by
`ruff` (`ruff format` + `ruff check`, config in `pyproject.toml`, line length
100); run both before committing and keep diffs focused.

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
`migrations/`, the SQLAlchemy model, and startup initialization expectations
aligned.
