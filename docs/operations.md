# Operations

How to run and observe the bot: startup preflight, the ops channel, Prometheus
metrics, recovery/retry, and logs. Background loops are described in
[`architecture.md`](architecture.md); env vars in [`config.md`](config.md).

## Startup preflight

On start the bot logs sanitized config (no secrets) and runs **non-fatal**
dependency checks before background work begins:

- `database` — DB access + ticket counters.
- `mattermost` — `/users/me`, `MATTERMOST_ALERT_CHANNEL_ID`, `MATTERMOST_INCIDENT_CHANNEL_ID`.
- `jira` — resolves field ids, issue type, createmeta and the options `Валидный`,
  `Ложный`, `Ожидаемый`, `Crit alert`, `Да` (warms caches).
- `llm` — if `LLM_API_TOKEN` is set, a small smoke request to `chat/completions`.

Events: `startup.configuration`, `startup.preflight.check_started`/`check_ok`/
`check_failed`/`completed`. In `LOG_FORMAT=text` the noisy `check_started`/`check_ok`
are hidden (short summary + errors remain). A failed check does **not** stop the app —
it surfaces access/token/model/metadata problems immediately.

## Ops channel (`MATTERMOST_OPS_CHANNEL_ID`, default off)

A channel for the **bot's own health** — not the Grafana alert channel. Two streams:

1. **Errors** — every `ERROR` event (WS drop, background `pending_work` failure,
   Jira/LLM errors, preflight failure) posts as a red box. Anti-storm: the same event
   is muted within `MATTERMOST_OPS_COOLDOWN_SECONDS` (default 300). Delivery is
   best-effort with a `_posting` contextvar recursion guard and a **bounded queue**
   (overflow counted by `bot_ops_alerts_dropped_total`).
2. **Issue-created feed** — on every Jira issue create (firing alert, manual incident,
   postmortem incident, admin UI recreate) a blue "Создана задача" box with the key and a
   link to the source. Posted by `_announce_issue_to_ops` as a normal message (no
   cooldown/recursion guard); **not** throttled; skipped when `jira_create_enabled=false`.

The bot must be a channel member with write access. With no channel configured, errors
are still counted (`bot_errors_total`) and logged.

## Prometheus metrics (`/metrics`, default on)

`GET /metrics` is enabled unless `METRICS_ENABLED=false`. Series:

- `bot_http_requests_total{client,method,status}` and
  `bot_http_request_duration_seconds{client,method}` — outbound HTTP
  (`client` = `jira`/`mattermost`/`llm`). **Streaming LLM calls bypass the shared
  instrumentation point** and are not counted here.
- `bot_errors_total{event}` — `ERROR` events by event name.
- `bot_ops_alerts_dropped_total` — ops alerts dropped on queue overflow.
- `bot_tickets_total`, `bot_tickets_pending_jira`, `bot_tickets_failed`,
  `bot_tickets_confirmed`, `bot_tickets_by_creation_status{status}`,
  `bot_tickets_by_confirmation_status{status}` — sampled lazily on scrape via
  `repository.stats_summary()`. A sampling failure logs `metrics.collect_failed`
  (WARNING, with stack) and returns no gauges instead of blanking `/metrics`.

`/metrics` has **no auth** and shares port `8080` — rely on network isolation /
reverse proxy. (The admin API `/admin/api/*` is the exception: it requires a Bearer
token, see [`domains/admin.md`](domains/admin.md).)

## Recovery & retry

Transient Mattermost/Jira errors use exponential backoff on **429/5xx only**
(`retry.py`). The durability backbone is `pending_work_loop`:

- A failed Jira create leaves the row at `creation_status=failed_jira`; the loop
  retries it every `PENDING_WORK_INTERVAL_SECONDS`.
- A confirmation that arrived before the Jira issue existed is saved as
  `pending_confirmation` and completed once the issue exists.
- On restart the pending worker recovers unfinished creation/confirmation from
  `alert_tickets`.
- Backfill of channel history is **off by default**; enable with
  `ENABLE_BACKFILL_ON_STARTUP=true` + `BACKFILL_RECENT_POSTS_LIMIT`.

Old rows without `jira_issue_key` are retried forever by the loop. To stop retrying
stale alerts, inspect and clean them manually:

```sql
SELECT id, mattermost_post_id, creation_status, confirmation_status, created_at, last_error
FROM alert_tickets
WHERE jira_issue_key IS NULL
ORDER BY created_at;

DELETE FROM alert_tickets
WHERE jira_issue_key IS NULL
  AND creation_status IN ('pending_jira', 'failed_jira');
```

See [`persistence.md`](persistence.md) and [`domains/jira-sync.md`](domains/jira-sync.md).

## Logs

Logs go to stdout; `LOG_FORMAT` selects the shape:

- `json` (default) — one JSON object per event; full detail (tracebacks under
  `exception`). Good for Loki/ELK.
- `text` — compact `time LEVEL event key=value …`. On `INFO`, only business events
  show; noisy `check_ok`, skip/no-op, Jira metadata/cache and low-level Mattermost
  notices are hidden. `WARNING`/`ERROR` always pass.

`LOG_LEVEL` defaults to `INFO`. Unexpected exceptions (background loops, the WS event
handler, startup backfill, HTTP endpoints) log at `ERROR` with `exc_info` and an
`error_type` field; expected integration errors (`ApiError`) log compactly without a
stack. uvicorn is unified via `log_config=None`, so its access/error/lifecycle lines
use the same `LOG_FORMAT` and feed the in-memory ring buffer behind the admin UI
(`GET /admin/api/logs`, Bearer auth, see [`domains/admin.md`](domains/admin.md)).

A few high-signal events: `mattermost.alert.received`, `jira.issue.created`,
`incident.confirmed`, `postmortem.completed`, `summary.completed`,
`http.request.failed`. The full set is in the code.
