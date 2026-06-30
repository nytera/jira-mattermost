# Architecture

`mm_jira_bot` is a single-process FastAPI service that bridges a Mattermost alert
channel to Jira incidents and generates LLM postmortems/summaries. Python 3.11+,
async `httpx`, SQLAlchemy 2.0.

> For exact module signatures, the route table and the file tree, read the generated
> [`reference/service-map.md`](reference/service-map.md). This file is the *why*; that one is the *what*.

## Process model

`web.py:create_app()` builds the FastAPI app and injects one `IncidentBotService`.
In its lifespan it runs a startup preflight and launches background asyncio loops:

- **`run_startup_preflight`** — logs sanitized config and runs non-fatal dependency
  checks (DB, Mattermost `/users/me` + channels, Jira field/createmeta/options,
  optional LLM smoke request). Successful Jira checks warm field/createmeta caches.
- **`websocket_loop`** — connects to the Mattermost WS, pre-filters to
  `posted`/`reaction_added`, and dispatches each event to
  `handle_websocket_event` as its **own** `asyncio.Task`. Off-loading is mandatory:
  handling can take seconds (LLM + Jira), and inline handling stalls the socket read
  → receive buffer fills → keepalive ping times out → `1011` disconnect. The loop
  reconnects on failure.
- **`pending_work_loop`** — every `PENDING_WORK_INTERVAL_SECONDS` calls
  `process_pending_work()` to retry failed Jira creates and pending confirmations.
  This is the durability backbone (see [`operations.md`](operations.md)).
- **`authorized_users_refresh_loop`** — only when `MATTERMOST_AUTHORIZED_USERNAMES`
  is set; re-resolves the allowlist so group-membership changes propagate. A
  transient/empty refresh keeps the last known-good set.

`create_app(service=...)` accepts an injected service — tests pass fakes and a temp
SQLite DB instead of live clients.

## Service assembly (mixins)

The service logic is one class, `IncidentBotService`, assembled from one base mixin
plus six domain mixins in `service/`. Each mixin lives in its own `_<domain>.py`,
inherits only `object`, and gets its state (`settings`, `repository`, `mattermost`,
`jira`, `llm`, auth fields) from `coordinator.__init__`. The MRO is the declaration
order, shown below (the linearized list is in
[`reference/service-map.md`](reference/service-map.md)).

| Mixin | File | Domain doc |
|---|---|---|
| `SharedMixin` | `service/_shared.py` | base — primitives shared by ≥2 domains |
| `AlertMixin` | `service/_alerts.py` | [`domains/alerts.md`](domains/alerts.md) |
| `AdminMixin` | `service/_admin.py` | [`domains/admin.md`](domains/admin.md) |
| `IncidentMixin` | `service/_incidents.py` | [`domains/incidents.md`](domains/incidents.md) |
| `JiraSyncMixin` | `service/_jira_sync.py` | [`domains/jira-sync.md`](domains/jira-sync.md) |
| `PostmortemMixin` | `service/_postmortem.py` | [`domains/postmortem.md`](domains/postmortem.md) |
| `ThreadSummaryMixin` | `service/_thread_summary.py` | [`domains/thread-summary.md`](domains/thread-summary.md) |

`coordinator.py` keeps init/auth and the event routers
(`handle_websocket_event`/`handle_reaction`/`handle_slash_command`) plus the
cross-domain orchestration of who-calls-what.

`AdminMixin` backs the admin UI: its methods are exposed as JSON routes by
`admin_api.py` (`register_admin_api`, `/admin/api/*`, Bearer auth) and consumed by
the React SPA — see [`domains/admin.md`](domains/admin.md) and the frontend overview
in [`admin-ui.md`](admin-ui.md).

### The `_shared.py` import-graph leaf

`_shared.py` deliberately imports **nothing** from `coordinator` or the domain
mixins. It holds cross-domain runtime primitives (`ActionResult`, summary texts,
`_PROMPT_KEY_*`, `parse_post_id_from_text`) **and** `SharedMixin` (the base of the
assembled class: `_resolve_prompt_template`, `_post_alert_thread_reply`,
`_box_thread_reply`). This breaks the cycle "coordinator imports mixin → mixin needs
a name from coordinator": shared names live in the leaf, `coordinator` and every
mixin import *from* it, never the reverse.

**Mixin typing convention.** State attrs are typed only as `__init__` declares them
(`mattermost`/`jira`/`llm` → `Any`); cross-domain sibling calls are declared as
inline `if TYPE_CHECKING:` stubs (each standalone mixin needs a stub for every
sibling method it calls; `SharedMixin` is self-contained). Every file has
`log = get_logger("mm_jira_bot.service")`.

## Two idempotent flows

1. **Alert → Jira issue** (`handle_alert_post`): a DB row (unique
   `mattermost_post_id`) is inserted **before** the Jira call, so a crash mid-create
   is retried by `pending_work_loop`. See [`domains/alerts.md`](domains/alerts.md)
   and [`domains/jira-sync.md`](domains/jira-sync.md), including the episode/
   expected-repeat model.
2. **Confirmation → valid incident** (`confirm_incident`, via the `:incident:`
   reaction or `/incident <permalink>`): posts to the incidents channel, sets Jira
   `Valid Incident = Валидный`, comments, replies in the alert
   thread. If the Jira issue does not exist yet it is saved as `pending_confirmation`
   and completed by the loop. See [`domains/incidents.md`](domains/incidents.md).

Re-delivered events are no-ops, guarded by idempotency keys in `AlertTicket` and the
`creation_status`/`confirmation_status` state machines. Keys and state machines are
documented in [`persistence.md`](persistence.md).

## Read-only / shadow mode (`READ_ONLY_MODE=true`)

A shadow instance runs in parallel with prod, changes nothing externally, and
mirrors every would-be write into an audit channel. Writes are suppressed at the
client-method level (Jira no-ops; Mattermost redirects to the audit channel via
`AuditMirror`) with a last-resort `_request` backstop. Full behavior in
[`read-only.md`](read-only.md); the Jira no-op detail is in [`jira.md`](jira.md).

## Where to look next

Pick the document by task from the navigator table in
[`../CLAUDE.md`](../CLAUDE.md); the generated
[`reference/service-map.md`](reference/service-map.md) has routes, public API, MRO and
the file tree.
