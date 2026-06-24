# Repository Guidelines

Guidance for AI agents and contributors working in this repo. User-facing
setup, workflow, and operations live in `README.md` (Russian); this file is the
technical/architecture reference and the style/PR conventions.

A `Mattermost alert → Jira incident` bridge bot. Python 3.11+, FastAPI + async
httpx, SQLAlchemy 2.0.

## Build, Test, and Development Commands

- `python -m venv .venv && source .venv/bin/activate`: create and enter a local virtual environment.
- `pip install -e ".[test]"`: editable install with pytest, ruff, and Pyright dependencies.
- `python -m mm_jira_bot`: run the bot locally on `0.0.0.0:8080`, reads `.env`.
- `curl http://localhost:8080/healthz`: check the local FastAPI health endpoint.
- `pytest`: full suite (`asyncio_mode=auto`, `pythonpath=src`).
- `pytest tests/test_service.py::test_<name>`: run a single test.
- `pytest --cov=mm_jira_bot --cov-report=term-missing`: suite with coverage report (baseline ~78%).
- `ruff check src tests`: lint (rules `E,F,I,UP,B,SIM`; config in `pyproject.toml`).
- `ruff format src tests`: apply formatting (`--check` to verify without writing).
- `pyright`: type-check `src/mm_jira_bot` and `tests` (`basic` mode; config in `pyproject.toml`).
- `ruff check src tests && ruff format --check src tests && pyright && pytest`: local full check before PRs.
- `docker compose up --build`: build and run the bot with Postgres.

Lint/format via `ruff`, type-check via Pyright, coverage via `pytest-cov` (all in the
`[test]` extra). Tooling config lives in `pyproject.toml`; `debug_admin.py`, `jira_payload.py`, and
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
- **`websocket_loop`** — connects to Mattermost WS (`websocket_events()`), pre-filters to `posted`/`reaction_added`, and dispatches each to `service.handle_websocket_event` as its own `asyncio.Task` (strong-ref set + `add_done_callback`). Off-loading is mandatory: handling can run for many seconds (postmortem/summary = LLM + Jira), and doing it inline stalls the socket read → the `websockets` receive buffer fills → transport pauses → keepalive ping times out → `1011` disconnect. Per-task errors are logged in `_handle_ws_event`; the loop reconnects on failure.
- **`pending_work_loop`** — every `PENDING_WORK_INTERVAL_SECONDS` calls `process_pending_work()` to retry failed Jira creates and pending confirmations from the DB. This is the durability backbone: any partial failure is recovered here.
- **`authorized_users_refresh_loop`** (only when `MATTERMOST_AUTHORIZED_USERNAMES` is set) — every `MATTERMOST_AUTHORIZED_REFRESH_SECONDS` (default 300) re-runs `resolve_authorized_users()` so Mattermost group-membership changes propagate. Unlike the startup resolve, a transient/empty refresh keeps the last known-good set instead of failing open.

HTTP endpoints: `GET /healthz`, `GET /metrics` (Prometheus exposition, mounted
when `METRICS_ENABLED`, default on; no auth — relies on network isolation like
debug admin), `POST /mattermost/slash/incident` (the
`/incident` slash command; validates `MATTERMOST_SLASH_TOKEN` if set), and
`POST /mattermost/actions/alert` (interactive-button callback → calls
`service.handle_alert_action`; Mattermost does not sign button callbacks, so this
endpoint relies on network isolation rather than a token), and
`POST /mattermost/dialogs/feedback` (interactive-dialog submission → stores
feedback and replies in the alert thread). When
`DEBUG_ADMIN_ENABLED`, `register_debug_admin` also mounts the SPA at
`GET /debug/admin` plus its JSON API: `summary`, `alerts`, `alerts/{post_id}`,
`POST alerts/{post_id}/jira/recreate`, `POST alerts/create-from-link` (create a
Jira issue from a pasted Band link/post id), `GET api/logs` (reads the
in-memory `LogRingBuffer` installed by `configure_logging`), and the
**Настройки** tab: `GET api/settings` (effective LLM prompt per key + its
`source` db/env/default), `POST api/settings/{key}` (save a DB override) and
`POST api/settings/{key}/reset` (drop it). Edits apply on the next generation —
no restart — because `_resolve_prompt_template` reads the DB each run.

`create_app(service=...)` accepts an injected service — tests pass fakes and a
temp SQLite DB instead of live clients.

### Module responsibilities (`src/mm_jira_bot/`)

Runnable entry point is `src/mm_jira_bot/__main__.py`.

| Module | Role |
|--------|------|
| `service/` | Orchestration package. The only place that coordinates Mattermost + Jira + repository. Read this first. `IncidentBotService` is being split into per-domain **mixin** files assembled in `service/coordinator.py` (`class IncidentBotService(SharedMixin, AlertMixin, IncidentMixin, JiraSyncMixin, PostmortemMixin, ThreadSummaryMixin)`); re-exported from `service/__init__.py` so `from mm_jira_bot.service import IncidentBotService` is unchanged. What stays in `coordinator.py` now: init/auth/event routers + debug (not yet extracted) + shared helpers still pending review for `SharedMixin` (`_is_bot_post`/`_is_authorized`/`_resolve_user_display`/`_interactive_controls_enabled`/`_action_callback_url`/`_post_unauthorized_notice`/`_announce_issue_to_ops`/`_post_incident_thread_reply`/`_validity_label_for_emoji`). Done so far: `_thread_summary.py` (ThreadSummaryMixin), `_postmortem.py` (PostmortemMixin — `generate_incident_postmortem` + Jira field plumbing `_set_time_to_fix`/`_apply_postmortem_validity`/`_ensure_postmortem_jira_issue` + `_postmortem_thread_context`), `_jira_sync.py` (JiraSyncMixin — alert→Jira issue `_ensure_jira_issue`/`_create_jira_issue`/`_stub_jira_issue`/`_display_jira_issue`, expected-repeat linking `_handle_expected_repeat`, confirmation field plumbing `_update_jira_for_confirmation`, background `process_pending_work`/`backfill_recent_alerts`), `_incidents.py` (IncidentMixin — полный жизненный цикл инцидента: ручной incident-post `handle_manual_incident_post`/`_incident_duty_help`/`_post_incident_thread_mention`/`_incident_controls_attachment`, кнопки/чекмарк `handle_incident_action`/`_incident_create_task`/`handle_incident_checkmark`, валидность/END-время `_set_incident_validity`/`_mark_incident_post_completed`/`apply_incident_end_time`, подтверждение и публикация в incident-канал `confirm_incident`/`_publish_incident_message_if_needed`), `_alerts.py` (AlertMixin — обработка алерт-поста `handle_alert_post` (создание Jira-задачи через JiraSync / повторы), интерактивные кнопки и меню `handle_alert_action`/`_alert_action_attachments`, feedback-диалог `open_feedback_dialog`/`handle_feedback_dialog_submission`, валидность `apply_validity_label`, вложения исходного поста `_alert_attachments`; module-level `_copy_post_attachments`/`_incident_action_message`). `_shared.py` is the import-graph leaf that breaks coordinator↔mixin cycles: cross-domain runtime primitives (`ActionResult`, summary texts, `_PROMPT_KEY_*`, validity-action text `_validity_action_message` — зовут и оставшийся `handle_alert_action`, и переехавший `handle_incident_action`) **and** `SharedMixin` — the base of the assembled class holding methods proven to be shared by ≥2 domains (`_resolve_prompt_template`/`_prompt_env_default`, `_post_alert_thread_reply`, `_box_thread_reply`). Mixin typing convention: state-attrs only as `__init__` declares them (`mattermost`/`jira`/`llm` → `Any`), cross-domain calls as inline `if TYPE_CHECKING:` stubs (each standalone mixin needs a stub for every sibling method it calls; SharedMixin is self-contained), `log = get_logger("mm_jira_bot.service")` in every file. |
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
| `logging.py` | `JsonFormatter` / `TextFormatter` + text-only INFO filtering (gates only our own `event` records; foreign INFO like uvicorn passes) + `EventLogger` (`get_logger(__name__)` → `log.info(event, **fields)`, optional `exc_info=` for tracebacks); format chosen by `LOG_FORMAT`. `__main__.py` runs uvicorn with `log_config=None` so `uvicorn.*` loggers propagate to root and share these formatters + the ring buffer. |
| `web.py` | FastAPI app factory (`create_app`), HTTP error-boundary middleware (`http.request.failed` with traceback → 500 JSON; `http.request.bad_json`/`bad_body` → 400), background loops, `/healthz` + `/incident` slash. |
| `debug_admin.py` | Optional debug-admin UI/API (`register_debug_admin`), gated by `DEBUG_ADMIN_ENABLED`. |
| `ops.py` | `OpsNotifier` + `OpsLogHandler`: forward `ERROR` events from `mm_jira_bot.*` to the `MATTERMOST_OPS_CHANNEL_ID` channel (best-effort, per-event cooldown, `_posting` contextvar recursion guard, bounded queue). Counts `bot_errors_total` even without a channel. The "issue created" feed to the **same** channel is separate: `coordinator._announce_issue_to_ops` posts a normal Mattermost message (not via logging, so no cooldown/recursion guard) after every `attach_jira_issue`/`replace_jira_issue`; skipped when `jira_create_enabled=false`. |
| `metrics.py` | Prometheus definitions (HTTP counters/histogram, `bot_errors_total`, ticket gauges via `TicketStatsCollector`) on the default `REGISTRY`. HTTP metrics observed in `AsyncApiClient._request`; gauges sampled lazily on scrape (a `debug_summary` failure logs `metrics.collect_failed` at WARNING and returns no gauges instead of blanking `/metrics`). |
| `config.py` | `.env` loader + `Settings.from_env()`. |

### Two flows, both idempotent

1. **Alert → Jira issue** (`handle_alert_post`): skip non-alert-channel, own-bot posts → `create_or_classify_alert` inserts an `alert_tickets` row (unique `mattermost_post_id`) and classifies it within its **episode** → `_ensure_jira_issue` creates the Jira issue, stores `jira_issue_key`, and replies in the alert thread with the issue link. When `MATTERMOST_DUTY_MENTION` is set, that reply carries the on-call mention as bare text above the boxed "Создана задача" notice so the ping fires (resolved alerts never reach this point and repeats suppress it — see below — so only **root** firing alerts ping). The DB row is created *before* the Jira call so a crash mid-create is retried later.
   - **Episodes / expected repeats.** An episode is `(alert_signature, channel)` — `alert_signature` is keyed on the extracted **title** (`extract_alert_title`), *not* the grafana UID, so a firing and its `✅` resolve (which may drop the link) stay symmetric and the episode closes correctly. The first firing is the **root** (`root_post_id IS NULL`); every later firing of the same title in the same channel while the episode is open is a **repeat**: `_handle_expected_repeat` adds the `MATTERMOST_EXPECTED_INCIDENT_REACTION_NAME` reaction (the bot's only self-added reaction, via `MattermostClient.add_reaction`), sets the repeat issue's `Валидность = Ожидаемый`, rewrites its description to append a root-links block (`build_expected_alert_block`), creates a real Jira `is child of` link to the root (`JiraClient.link_child_of`, type name auto-resolved from `JIRA_REPEAT_LINK_INWARD`), and posts a "Прилинковано к" notice. Idempotent steps run every delivery; the non-idempotent link + notice are guarded by `expected_repeat_linked` (set only after the link call returns, so a failed link is retried, not lost). The `uq_active_root` partial-unique index enforces one active root per episode and resolves the concurrent first-firing race (the loser retries as a repeat). On a repeat, `_ensure_jira_issue(is_repeat=True)` **suppresses the on-call duty ping (`MATTERMOST_DUTY_MENTION`) and the duty cheat-sheet** — the repeat is auto-marked expected and needs no on-call action, so both would only be noise (the "Создана задача" box and the "Прилинковано к" notice still post).
   - **Resolve invariant.** A resolved (`✅`) post (`is_resolved_alert`) creates **no ticket and no Jira issue** — `mark_episode_resolved` only stamps `resolved_at` on the open episode's root, so the next firing of that title becomes a fresh root and the cycle repeats.
2. **Confirmation → valid incident** (`confirm_incident`, triggered by the `:incident:` reaction or `/incident <permalink>`): posts to the incidents channel, sets Jira `Valid Incident = Валидный`, adds a comment, optional transition, and replies in the alert thread about the status change. If the Jira issue does not exist yet, it is saved as `pending_confirmation` and completed by `pending_work_loop`.

When `JIRA_CREATE_ENABLED=false` (test mode), the `JiraClient` makes **no Jira
calls for issue-key operations**: `create_issue`/`create_postmortem_issue` return
a stub `JiraIssue` (`stub_jira_issue` — `JIRA_STUB_ISSUE_KEY` plus a
Mattermost-post-id suffix for DB uniqueness, or a generated
`{JIRA_PROJECT_KEY}-12345`-style key), and `get_valid_incident` / `set_validity`
/ `set_valid_incident` / `set_end_time` / `set_time_to_fix` / `set_description` /
`add_comment` / `transition_issue` are no-ops. This matters because the stub key does not exist in
Jira — without the no-op, those calls would 404 and abort `confirm_incident`
(after the incident-channel post but before the alert-thread reply). Mattermost
issue-created replies display the clean configured `JIRA_STUB_ISSUE_KEY` via
`_display_jira_issue`. Field/option metadata reads (global, not issue-scoped) are
not stubbed.

There is also a **lightweight validity path** (`apply_validity_label`, triggered by the two configurable reactions `MATTERMOST_FALSE_INCIDENT_REACTION_NAME` → `Ложный` and `MATTERMOST_EXPECTED_INCIDENT_REACTION_NAME` → `Ожидаемый`) **in the alert channel only**. It sets Jira's `Валидность` field (`JiraClient.set_validity`), optionally sets `JIRA_END_FIELD` to the reaction time, and replies in the alert thread — no incidents-channel post, comment, or transition. Last reaction wins: each distinct label overwrites the field; the `validity_label` column guards against re-applying the same label (no duplicate replies). It does **not** touch the `valid_incident` confirmation state machine and is best-effort (no `pending_work_loop` retry) — if the Jira issue is not ready, the update is skipped.

The **same false/expected reactions in the incident channel** behave differently: on an incident-thread root they route into `handle_incident_checkmark(validity_label=...)`, which both stamps the chosen `Валидность` **and** finalizes the incident (end-time + postmortem) — the checkmark (`INCIDENT_END_REACTION_NAMES`) is the `Валидный` shortcut. Postmortem generation is now idempotent: the `postmortem_comment_added` flag (set after the additive `add_comment`) means a second checkmark/validity reaction on a closed incident only updates the `Валидность` field and never re-posts the PM comment. `_ensure_postmortem_jira_issue` takes `validity_label`: a present value is pushed via `set_validity` (overriding the default-`Валидный`), and an earlier explicit Ложный/Ожидаемый still survives.

A configurable **summary reaction** (`MATTERMOST_SUMMARY_REACTION_NAME`, default `memo`) triggers `generate_thread_summary` in any channel — the emoji analogue of the 📝 summary button, gated by the same allowlist.

The **allowlist** (`resolve_authorized_users`) accepts a mixed comma/semicolon-separated list of logins and Mattermost group names: each entry is resolved as a login first (`get_user_ids_by_usernames`), the remainder as groups (`get_group_ids_by_names` + `get_group_member_ids`, tolerant of a missing groups license). It is re-run by `authorized_users_refresh_loop`.

Alert-thread replies (`_post_alert_thread_reply`) and incident-thread replies
(`_post_incident_thread_reply`) are best-effort: they reuse the root `post_id`
as `root_id`, are guarded once-only by the same early returns that protect issue
creation / confirmation (no extra DB flag), and swallow `ApiError` so a failed
notification never breaks the main flow. Both helpers box a plain bot notice into
a single colored attachment (`_box_thread_reply`, `NOTICE_ATTACHMENT_COLOR`) so
every bot comment renders as an attachment block, not a bare message; callers can
override the bar color (the duty cheat-sheet passes `DUTY_HELP_ATTACHMENT_COLOR`,
neutral slate-400 `#94A3B8`). The wrap is
skipped when the caller already supplies `attachments` (interactive cards keep
their own layout, and a `message` carrying an `@mention` stays plain text so the
ping fires); `fallback` carries the notice text into push/preview.

**Interactive buttons/menu** (`handle_alert_action`) are an alternative entry
point to the same two flows plus a thread summary. The bot can't attach controls
to the alert (a Grafana/user post), so it hangs them on its own issue-created
reply via `_alert_action_attachments` (only when `_interactive_controls_enabled()`
— `SERVICE_PUBLIC_URL` set **and** `INTERACTIVE_BUTTONS_ENABLED=true`; the toggle
**defaults to false**, so the bot is in emoji-only mode unless buttons are
explicitly enabled, and emoji reactions are always the fallback).
Independently, after the issue-created reply the alert thread also gets a boxed
**duty cheat-sheet** (`format_alert_duty_help`, reactions only) when
`DUTY_HELP_ENABLED` (default true); resolved alerts create no issue and get
neither, and repeats (`is_repeat=True`) skip both the duty ping and the
cheat-sheet (auto-marked expected, no on-call action needed). Both incident threads (manual and alert-originated) get their own
`format_incident_duty_help` cheat-sheet, which spells out that validity reactions
there close the incident + postmortem (distinct from the alert thread's
label-only meaning) and lists the summary emoji. Current UI is a single thread reply with
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
`jira_confirmation_comment_added`, `postmortem_comment_added` (set once the PM
comment is posted, so repeated checkmark/validity reactions never duplicate it),
plus `creation_status` / `confirmation_status` state machines. Re-delivered
events are no-ops.

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
link, postmortem author, and participants. The postmortem **and** the in-thread
summary share **one** user-prompt template — `DEFAULT_INCIDENT_REPORT_PROMPT` in
`postmortem.py` (aliased as `DEFAULT_POSTMORTEM_PROMPT`/`DEFAULT_SUMMARY_PROMPT`),
rendered by the single `build_incident_report_prompt` with placeholders
`{thread_url}`/`{participants}`/`{postmortem_author}`/`{transcript}` (ordered
`str.replace`, `{transcript}` last so thread text is never re-scanned). It carries
the rich structure (Мета, Сводка + Описание влияния, Решение, Извлечённые уроки,
Action Items as discussion suggestions, Хронология, Риски рецидива, Открытые
вопросы) and the mandatory `[INC] DD.MM.YYYY - …` first line (read by
`extract_postmortem_summary` for the Jira issue title). The effective template per
channel is resolved at call time by `_resolve_prompt_template`: **DB override
(debug panel) → env (`LLM_POSTMORTEM_PROMPT`/`LLM_SUMMARY_PROMPT`, plus `_FILE`) →
built-in default**. The two `SYSTEM_PROMPT`s in `llm.py` stay in code. The LLM
always emits Markdown; the chronology attributes participants as `@username`. The
**Jira** path converts the Markdown to wiki markup *and* `@username → [~username]`
via `markdown_to_jira_wiki` (the v2 comment endpoint renders wiki, not Markdown);
the **Mattermost** summary path strips the `@` via `summary.neutralize_mentions`
so it never pings. The summary is posted back to the incident thread (same builder,
its own LLM call — not derived from the Jira postmortem — published as a
"Генерация саммари…" placeholder that is then edited into the final reply). During the completion flow the placeholder shows stepwise
status (`_set_summary_status`: "Шаг 1/3 … 3/3"), and the summary text is streamed
into it live (throttled) as the LLM generates. On closure a **standalone green box**
"🟢 Инцидент закрыт" with a "ПМ: [title](url)" line is posted as a *separate* reply
(`format_incident_closed_notice`, `INCIDENT_DONE_COLOR`) — it replaces the old
in-summary footer and only fires on the postmortem path (so the Jira link always
exists). A checkmark on
an unmapped manual incident thread root post creates a Jira issue with a
PM-template description, but it does not set the alert-only source/is-crit-alert
fields. Checkmarks on incident thread replies are ignored.

**LLM streaming into the thread** (`generate_summary(prompt, on_progress=…)` →
`_collect_stream` in `llm.py`; throttle callback `_make_summary_stream_callback` in
`service.py`). Two load-bearing invariants — break either and generation misbehaves:
(1) the callback receives the **cumulative** text of the current attempt, not a
delta — so a retry that restarts the stream replays from an empty buffer and
`update_post` (wholesale replace) overwrites the stale partial cleanly; the callback
also force-renders when the buffer **shrinks** (retry restart). (2) the callback
**never raises** — its `update_post` is wrapped in try/except-log (via
`_edit_summary_reply`); a raised `ApiError` would escape `_collect_stream`, reach
`_retry`, and restart the whole LLM generation over a transient Mattermost edit blip.
Edits are throttled by `LLM_STREAM_EDIT_INTERVAL_SECONDS` / `LLM_STREAM_EDIT_MIN_CHARS`;
`last_edit_time` is seeded at callback creation so the first stream edit respects the
interval after the "Шаг 3/3" status edit. The buffered-JSON fallback and
`LLM_STREAM=false` never invoke the callback (static placeholder, no failure).

`JIRA_TIME_TO_FIX_FIELD` (optional) is a **numeric** field set to the incident
duration in **minutes** at any closing action (`_set_time_to_fix`): the three
incident-end sites (`apply_incident_end_time` and both branches of
`_ensure_postmortem_jira_issue`) **and** the alert-channel validity reaction
(`apply_validity_label`, end = reaction time). Start is `ticket.mattermost_message_created_at`
(same instant as `JIRA_START_FIELD`); a naive persisted value is localized to the
runtime timezone (not assumed UTC) before subtraction. It is best-effort and
**must not break closure**: unlike `set_end_time`, the call is wrapped in
try/except + log, and it skips (with a log line) when the field is unset, the start
is missing, or the duration is non-positive.

### Manual incidents: button card (incident channel)

Alongside the checkmark, a button-driven flow handles incidents typed **directly**
in `MATTERMOST_INCIDENT_CHANNEL_ID` (needs `SERVICE_PUBLIC_URL`). The WS posted
handler routes incident-channel posts to `handle_manual_incident_post`: for every
**root** post by a real user (not a bot — filtered by `_is_bot_post`, which checks
`props.from_bot` / `props.from_webhook` and `MATTERMOST_BOT_USER_ID`) it pre-creates
the ticket row via `create_or_get_incident_thread` (idempotent) and posts a
"➕ Создать задачу" card with the `MATTERMOST_DUTY_MENTION` text above it. In
emoji-only mode (the default — buttons are off unless `SERVICE_PUBLIC_URL` is set
**and** `INTERACTIVE_BUTTONS_ENABLED=true`) there is no card, but if
`MATTERMOST_DUTY_MENTION` is set the handler still posts that mention as a bare
reply (via `_post_incident_thread_mention`, kept unboxed so the ping fires).
After the `if not created: return` guard, a single **duty cheat-sheet** reply
(`format_incident_duty_help`, boxed, reactions only — no button hints) is posted
across all branches when `DUTY_HELP_ENABLED` (default true); this is why the
handler now proceeds even in emoji-only mode with no duty mention (it only
short-circuits before the ticket row when there is genuinely nothing to post:
no card, no mention, and help disabled). No Jira issue yet. The card's controls carry
`context.source = "incident"` + `incident_post_id`, so `handle_alert_action` branches
early to `handle_incident_action` (keyed by `incident_post_id`, skips the alert-channel
checks). Actions: `create_task` → `create_postmortem_issue` (no alert fields) and the
action response's `update` payload swaps the card for the controls (validity menu,
"🏁 Завершить", "📝 Саммари"); `validity` → `apply_validity_label`;
`end_incident` → reuses `handle_incident_checkmark` (full PM); `summary` →
`generate_thread_summary` (thread-only, no Jira; placeholder → edited reply). The
checkmark flow stays available in parallel.

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
model by hand. Besides `alert_tickets`/`alert_feedback`, `app_settings` is a
`key`/`value` store for runtime-editable config (the debug-panel LLM prompt
overrides); `repository.get_setting`/`set_setting`/`delete_setting` manage it. `normalize_database_url` rewrites
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
`ruff` (`ruff format` + `ruff check`, line length 100) and type checking by
Pyright (`basic` mode); run them before committing and keep diffs focused.

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
