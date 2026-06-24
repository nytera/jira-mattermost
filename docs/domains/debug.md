# Debug admin (DebugMixin)

`DebugMixin` (`src/mm_jira_bot/service/_debug.py`) backs two operator entry points that the optional debug-admin panel exposes: create a Jira issue for an alert from a pasted link/post id, and recreate (force-replace) the Jira link of an existing ticket. The panel itself — a single-page UI plus its JSON API — lives in `src/mm_jira_bot/debug_admin.py` (`register_debug_admin`). The mixin is one member of the assembled `IncidentBotService` (see `coordinator.py`); shared state (`settings` / `repository` / `mattermost`) is wired by the coordinator, and sibling domains supply the methods it calls into. For the exact route table and method signatures, see [../reference/service-map.md](../reference/service-map.md).

## Safety (read first)

The panel has **no auth beyond the `DEBUG_ADMIN_ENABLED` flag** and shares the bot's HTTP port (`8080` in the current `uvicorn.run`). Anyone who can reach the port can create/recreate Jira issues and read alert data + logs. **Never expose it without a firewall or reverse proxy.** It is off by default; `register_debug_admin` is only called when the flag is set. See [../operations.md](../operations.md) and [../config.md](../config.md).

## Scope and boundaries

- **Owned here:** `debug_create_from_link`, `debug_recreate_jira_issue`, and the result dataclasses `DebugCreateFromLinkResult` / `DebugJiraRecreateResult`.
- **Delegated out** (called via `TYPE_CHECKING` stubs): the alert→Jira flow `handle_alert_post` (see [../domains/alerts.md](../domains/alerts.md)), plus issue creation `_create_jira_issue` and confirmation field plumbing `_update_jira_for_confirmation` in `JiraSyncMixin`; the ops announcement `_announce_issue_to_ops` and `_resolve_user_display` stay in `coordinator`.
- **Read-only API parts** (`summary`, `alerts`, `alerts/{post_id}`, `logs`, `settings` GET) do not touch this mixin — they read `repository` and the in-memory log buffer directly.

## `debug_create_from_link` — reuse, don't reimplement

This deliberately **reuses the normal `handle_alert_post` flow** rather than building a parallel create path, so every alert invariant (episode keying, dedupe, repeat handling) holds. It only adds: resolve the post id from a pasted permalink (`parse_post_id_from_text`), fetch the post, and translate the outcome into explicit UI feedback.

Guard rails before delegating: post must be readable, must be in `mattermost_alert_channel_id`, and must not be a resolved (`✅`) repost (`is_resolved_alert`). It records `already_had_issue` from the existing row **before** the call so it can report `exists` vs `created`. Idempotent: re-running on a post that already has an issue returns `ok=true, status="exists"` without creating a duplicate.

## `debug_recreate_jira_issue` — force-replace the link

Used to repair a ticket whose Jira issue is missing or wrong.

- **No issue yet:** creates one (`status="created"`) — the same as a retry.
- **Issue exists, `force=false`:** refuses with `status="conflict"` (HTTP 409). This guard is the only thing preventing accidental duplicate issues.
- **Issue exists, `force=true`:** creates a **new** issue and points the local link at it via `repository.replace_jira_issue`.

Invariants and safety:
- **Force recreate does NOT delete or close the old Jira issue.** It only mints a new one and rewrites the local `jira_issue_key` / `jira_issue_url`; the previous key/url are returned (`previous_jira_issue_*`) for the operator to clean up manually.
- **No second incident post.** If the alert was already confirmed (`valid_incident` with an `incident_post_id`), the bot re-applies Jira confirmation to the **new** issue (`_update_jira_for_confirmation` + `mark_confirmed`) but does not create another incident post in the incident channel. `replace_jira_issue(reset_confirmation_comment=...)` clears the per-issue confirmation-comment flag so the comment is re-added to the new issue. See [../domains/postmortem.md](../domains/postmortem.md) / incidents for the confirmation state machine.
- **Error handling is status-coded** so the UI can react: create failure → `error` (HTTP 502, persists `last_error` / `mark_jira_create_failed`); confirmation re-apply failure → `confirmation_error` (the new issue still exists and is linked). On a pre-existing issue the create error keeps the old link intact (only `set_last_error`).
- `confirmed_by` defaults to `"debug-admin"` when the original confirmer is unknown.

## The panel (`debug_admin.py`)

`register_debug_admin(app, service)` mounts the SPA at `GET /debug/admin` and a small JSON API under `/debug/admin/api/...` (summary, alerts list + detail, the two action endpoints above, `logs` reading the in-memory `LogRingBuffer`, and the settings endpoints). The full route table is in the generated [../reference/service-map.md](../reference/service-map.md); the HTML is a self-contained string with three tabs (Алерты / Логи / Настройки).

### Настройки tab — runtime LLM prompt overrides

The Настройки tab edits the LLM prompt templates listed in `_EDITABLE_PROMPTS` (summary + postmortem). `GET api/settings` returns each key's **effective** value plus its `source` — `db` (panel override), `env`, or `default` — computed by `_prompt_settings_payload`. `POST api/settings/{key}` stores a DB override; `POST api/settings/{key}/reset` deletes it. Unknown keys return 404.

Edits apply on the **next generation, with no restart**, because prompt resolution reads the DB each run. The override/env/default resolution order is owned by the postmortem domain — see [../domains/postmortem.md](../domains/postmortem.md) (`_resolve_prompt_template` / `_prompt_env_default`).
