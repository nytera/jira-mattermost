# Configuration (env vars)

All configuration comes from environment variables, loaded from `.env` by
`load_dotenv_file` and parsed in `config.py` (`Settings.from_env`). **This matrix is
derived from `config.py` and is the authoritative reference** — the generated
[`reference/service-map.md`](reference/service-map.md) intentionally does **not**
cover env vars (their meaning is authored, not mechanical).

Copy `.env.example` to `.env` and fill it in. `*_FILE` variants (for prompts) take a
path whose contents become the value; the `.env` loader is line-based and cannot
carry a multi-line value inline, so use `*_FILE` for large prompts.

Booleans below follow `config.py` exactly: some default-on flags are
`!= "false"` (anything but `false` is on), some default-off flags are `== "true"`
(only `true` turns them on).

## Required

The bot refuses to start without these (`_required` / `_first_required`):

| Var | Meaning |
|---|---|
| `MATTERMOST_URL` | Mattermost base URL (trailing `/` stripped) |
| `MATTERMOST_TOKEN` | Bot personal access token |
| `MATTERMOST_ALERT_CHANNEL_ID` | Channel the bot watches for alerts |
| `MATTERMOST_INCIDENT_CHANNEL_ID` | Channel incidents are published to |
| `MATTERMOST_BOT_USER_ID` | Bot's own user id (so it ignores its own posts) |
| `JIRA_BASE_URL` | Jira base URL (trailing `/` stripped) |
| `JIRA_API_TOKEN` | Jira personal access token (Bearer) |
| `JIRA_PROJECT_KEY` | Target project key |
| `JIRA_ISSUE_TYPE` | Issue type name or numeric id |
| `JIRA_VALID_INCIDENT_FIELD` | Validity field name or id (`Валидность`). Aliases accepted: `JIRA_VALID_INCIDENT_FIELD_NAME` / `_ID` |
| `JIRA_SOURCE_FIELD` | Source field (must have option `Crit alert`) |
| `JIRA_IS_CRIT_ALERT_FIELD` | "is crit alert" field (must have option `Да`) |
| `DATABASE_URL` | SQLAlchemy URL; `postgres://`/`postgresql://` are rewritten to `postgresql+psycopg://` (see [`persistence.md`](persistence.md)) |

`MATTERMOST_INCIDENT_REACTION_NAME` is **optional** (defaulted), not required.

## Mattermost (optional)

| Var | Default | Meaning |
|---|---|---|
| `MATTERMOST_INCIDENT_REACTION_NAME` | `incident` | Reaction that confirms a valid incident |
| `MATTERMOST_FALSE_INCIDENT_REACTION_NAME` | `man_gesturing_no` | Lightweight `Ложный` validity reaction |
| `MATTERMOST_EXPECTED_INCIDENT_REACTION_NAME` | `arrows_counterclockwise` | `Ожидаемый` reaction; also the bot's self-added repeat marker |
| `MATTERMOST_SUMMARY_REACTION_NAME` | `memo` | Triggers a thread summary in any channel |
| `MATTERMOST_SLASH_TOKEN` | — | If set, validates the `/incident` slash token |
| `MATTERMOST_AUTHORIZED_USERNAMES` | empty | Comma/`;`-separated logins **and** group names; leading `@` stripped. Empty = act on everyone |
| `MATTERMOST_AUTHORIZED_REFRESH_SECONDS` | `300` | Allowlist re-resolve interval |
| `MATTERMOST_DUTY_MENTION` | — | On-call mention posted as bare text (e.g. `:look: @sre-duty`) |
| `MATTERMOST_OPS_CHANNEL_ID` | — | Ops channel for bot health (off when unset); see [`operations.md`](operations.md) |
| `MATTERMOST_OPS_COOLDOWN_SECONDS` | `300` | Per-event cooldown for ops error posts |

## Jira (optional)

| Var | Default | Meaning |
|---|---|---|
| `JIRA_START_FIELD` | — | Date-time field set to alert arrival time on create |
| `JIRA_END_FIELD` | — | Date-time field set on validity/checkmark close |
| `JIRA_TIME_TO_FIX_FIELD` | — | Numeric field, incident duration in minutes (best-effort) |
| `JIRA_REPEAT_LINK_INWARD` | `is child of` | Link type for expected-repeat → root |
| `JIRA_CREATE_ENABLED` | `true` | `false` = test mode, no Jira issue-key calls |
| `JIRA_STUB_ISSUE_KEY` | — | Key shown in Mattermost in test mode |

Field/option resolution mechanics and the date-time format are in [`jira.md`](jira.md).

## LLM (optional)

| Var | Default | Meaning |
|---|---|---|
| `LLM_BASE_URL` | `https://corellm.wb.ru/deepseek/v1` | OpenAI-compatible endpoint |
| `LLM_API_TOKEN` | — | Enables postmortems/summaries; aliases `CORELLM_API_TOKEN`, `OPENAI_API_KEY` |
| `LLM_MODEL` | `deepseek-chat` | Model id |
| `LLM_MAX_TOKENS` | `4000` | Response length cap |
| `LLM_THREAD_MAX_CHARS` | `24000` | Input thread char limit (head+tail trimmed) |
| `LLM_STREAM` | `true` | Stream the response via SSE into the thread |
| `LLM_STREAM_EDIT_INTERVAL_SECONDS` | `1.5` | Min interval between live edits |
| `LLM_STREAM_EDIT_MIN_CHARS` | `80` | Or every N new chars (whichever first) |
| `LLM_READ_TIMEOUT` | `120.0` | LLM read timeout (seconds) |
| `LLM_POSTMORTEM_PROMPT` / `_FILE` | — | Override the postmortem prompt |
| `LLM_SUMMARY_PROMPT` / `_FILE` | — | Override the summary prompt |

Prompt resolution order (DB override → env → built-in default) and placeholders are in
[`domains/postmortem.md`](domains/postmortem.md). Runtime overrides via the debug panel.

## Service / behavior (optional)

| Var | Default | Meaning |
|---|---|---|
| `SERVICE_PUBLIC_URL` | — | Public URL for interactive callback URLs (trailing `/` stripped) |
| `INTERACTIVE_BUTTONS_ENABLED` | `false` | Buttons on (needs `SERVICE_PUBLIC_URL` too); default = emoji-only |
| `DUTY_HELP_ENABLED` | `true` | Post the duty cheat-sheet reply |
| `METRICS_ENABLED` | `true` | Expose `GET /metrics` |
| `DEBUG_ADMIN_ENABLED` | `false` | Mount the debug admin SPA/API; see [`domains/debug.md`](domains/debug.md) |
| `ENABLE_WEBSOCKET` | `true` | Connect to the Mattermost WS |
| `ENABLE_BACKFILL_ON_STARTUP` | `false` | Process recent channel history on start |
| `BACKFILL_RECENT_POSTS_LIMIT` | `0` | How many recent posts to backfill |
| `PENDING_WORK_INTERVAL_SECONDS` | `30` | Pending-work retry loop interval |
| `API_RETRY_ATTEMPTS` | `4` | Retry attempts on 429/5xx |
| `API_RETRY_BASE_DELAY_SECONDS` | `0.5` | Exponential backoff base delay |
| `INCIDENT_TIMEZONE` | `Europe/Moscow` | Timezone for backend times (see [`persistence.md`](persistence.md)) |
| `LOG_LEVEL` | `INFO` | Log level |
| `LOG_FORMAT` | `json` | `json` or `text`; see [`operations.md`](operations.md) |

For local SQLite use `DATABASE_URL=sqlite:///./mattermost_jira_bot.db`; for the
bundled Postgres `postgresql://incident_bot:incident_bot@postgres:5432/incident_bot`.
