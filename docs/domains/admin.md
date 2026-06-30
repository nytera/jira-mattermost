# Admin backend (AdminMixin + admin_api)

The admin backend lets the admin UI drive the bot over HTTP: a JSON API
(`/admin/api/*`), a static SPA mount (`/admin`), and the service-side operations
behind them (`AdminMixin`). It replaces the old debug panel. Method shapes live in
[../reference/service-map.md](../reference/service-map.md); this doc covers the WHY.
The frontend itself (Vite/React SPA, pages, login) lives in
[../admin-ui.md](../admin-ui.md).

> `AdminMixin` is a domain mixin of `IncidentBotService` — see [architecture.md](../architecture.md) for how the service is assembled.

## AdminMixin: thin wrappers over the normal flows

UI actions reuse the existing domain paths instead of forking them; the mixin only
resolves the post and adds explicit feedback for the UI.

- **`admin_create_from_link`** — parses a permalink / post id, fetches the post, and
  runs the normal `handle_alert_post`. Guards before that (wrong channel, resolved
  alert) return a `skipped` status; a pre-existing issue returns `exists` vs `created`.
- **`admin_recreate_jira_issue(post_id, *, force=False)`** — one flag, two cases.
  With **no** existing key it retries a previously failed creation; with an existing
  key it **requires `force`** (without it → `conflict`/409). On force-replace it writes
  the new issue, re-announces it to ops, and — if the incident was already valid
  (`valid_incident` + `incident_post_id`) — **re-applies confirmation** to the new key
  (`confirmation_error` on a failed Jira write).
- **Lifecycle wrappers** delegate to the sibling flows with `source="admin_ui"`:
  - `admin_confirm_incident` → `confirm_incident`
  - `admin_set_validity` → `apply_validity_label` (lightweight Jira-field write)
  - `admin_generate_summary` → `generate_thread_summary` (thread-only, alert-keyed)
  - `admin_end_incident` / `admin_generate_postmortem` → `_run_incident_checkmark`

**End & postmortem share one idempotent path.** `_run_incident_checkmark` is
**incident-keyed** (it resolves `incident_post_id`, not the alert `post_id`) and runs
the full checkmark finalize (END-time + postmortem). `admin_end_incident` passes an
optional `ended_at`; `admin_generate_postmortem` passes `None`; both enter the same
path. A second call on a finalized incident leaves the postmortem untouched —
generation rules live in [postmortem.md](postmortem.md).

**Attribution.** UI-driven lifecycle actions are attributed by `_admin_actor_id()` →
`ADMIN_MM_USER_ID` so confirmations/ends carry a real identity in Jira/Mattermost;
it falls back to the `admin-ui` label when unset.

## admin_api: routes + auth

`register_admin_api(app, service)` attaches the routes **inline on `app`** so the AST
scanner in `scripts/gen_service_map.py` keeps picking them up. They group into:

- **Reads** — `GET /admin/api/stats` (rich dashboard), `summary` (lean), `alerts`
  (list + filters), `alerts/{post_id}`, `logs`, `settings`. All
  repository reads run via `asyncio.to_thread`.
- **Settings writes** — `POST settings/{key}` and `settings/{key}/reset` (prompt
  overrides, validated against `_EDITABLE_PROMPTS`).
- **Actions** — `POST alerts/create-from-link`, `alerts/{post_id}/jira/recreate`,
  and the lifecycle endpoints `confirm` / `end` / `validity` / `postmortem` /
  `summary`, each calling the matching `AdminMixin` method.

**Auth.** Every route carries a Bearer-token dependency (`Authorization: Bearer
<ADMIN_UI_TOKEN>`, compared with `secrets.compare_digest`). No token configured →
**503** (misconfiguration, not a client error); missing or wrong header → **401**.
There is a single shared token and no per-user identity, so front the service with a
reverse proxy / firewall ([../admin-ui.md](../admin-ui.md)).

## mount_admin_ui: serving the SPA

`mount_admin_ui(app)` serves the built React bundle from `admin_static/` with a
client-side catch-all (`GET /admin`, `GET /admin/{path:path}`). It is registered
**last**, after the JSON API and the bot's own routes, so `/admin/api/*`, `/healthz`,
`/metrics` and the Mattermost endpoints keep precedence. The catch-all is served
**unauthenticated by design** — the browser must load the bundle before it has the
token; the API behind it enforces auth. **No-op (warning only) when the build is
absent**, so the API and tests run without a Node build.

## Repository: two stats functions

- **`stats_summary`** — lean single-pass SQL aggregates (totals, status counts,
  pending/failed/confirmed). Feeds the Prometheus collector and the `/healthz`
  preflight.
- **`admin_stats`** — rich dashboard aggregate. Loads all rows and computes
  **MTTA/MTTR** and the **90-day daily timeseries in Python** so the math is identical
  on SQLite and Postgres (no dialect-specific date SQL), plus `top_channels` and the
  `by_creation_status` / `by_confirmation_status` / `by_validity_label` distributions.
  MTTA/MTTR are anchored at `mattermost_message_created_at` (alert fire time, matching
  the Jira Time-to-Fix definition). It **scans the table** → call it off the event loop
  via `asyncio.to_thread`.

## See also

- Frontend SPA, login, token storage, deployment: [../admin-ui.md](../admin-ui.md)
- Alert handling / validity reactions: [alerts.md](alerts.md)
- Incident lifecycle & checkmark finalize: [incidents.md](incidents.md)
- Postmortem generation: [postmortem.md](postmortem.md)
- Jira field writes & test-mode stubs: [../jira.md](../jira.md)
- Env vars (`ADMIN_UI_ENABLED`, `ADMIN_UI_TOKEN`, `ADMIN_MM_USER_ID`): [../config.md](../config.md)
- Route/method signatures: [../reference/service-map.md](../reference/service-map.md)
