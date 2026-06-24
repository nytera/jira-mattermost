# Jira sync (JiraSyncMixin)

`JiraSyncMixin` (`src/mm_jira_bot/service/_jira_sync.py`) owns the **alert → Jira
issue** flow: it creates the Jira issue for a firing alert, replies "Создана
задача" in the alert thread, annotates expected repeats, plumbs confirmation
fields into Jira, and provides the background-durability entry points. This page
covers WHY and the invariants; for signatures see
[../reference/service-map.md](../reference/service-map.md).

> `JiraSyncMixin` is a domain mixin of `IncidentBotService` — see [architecture.md](../architecture.md) for how the service is assembled.

Field/option **resolution mechanics** (name → `customfield_*`, validity/option
plumbing, `JIRA_CREATE_ENABLED=false` stubbing) live in [../jira.md](../jira.md) —
link, don't duplicate.

## Issue creation (`_ensure_jira_issue`)

The crash-safety contract: the DB row exists **before** any Jira call. Callers in
[../domains/alerts.md](../domains/alerts.md) insert the `alert_tickets` row first,
then call `_ensure_jira_issue`. If the process dies mid-create, the row carries
`creation_status = pending` and `pending_work_loop` retries it via
`process_pending_work`. `_create_jira_issue` → `_stub_jira_issue` when
`JIRA_CREATE_ENABLED=false` (test mode); `_display_jira_issue` shows the clean
configured `JIRA_STUB_ISSUE_KEY` in the thread reply while the DB keeps a
post-id-suffixed unique key.

Idempotency: the method early-returns when `ticket.jira_issue_key` is already set,
so re-delivery and retries never create a second issue. On `ApiError` it stamps
`mark_jira_create_failed` and returns — the loop retries; it does not raise.

After the issue is created it: stores the key (`attach_jira_issue`), announces to
the ops feed (see [../operations.md](../operations.md)), and posts the boxed
"Создана задача" thread reply (with interactive action attachments when enabled,
plain otherwise). The on-call `MATTERMOST_DUTY_MENTION` rides as bare text above
the box so the ping fires. A duty cheat-sheet follows when `DUTY_HELP_ENABLED`.

`is_repeat=True` **suppresses the duty ping and the cheat-sheet**:
`_handle_expected_repeat` runs right after and auto-marks the repeat expected, so
no on-call action is needed and both would be noise.

## Episodes and the root invariant

An **episode** is keyed `(alert_signature, channel)`, where `alert_signature` is
derived from the extracted alert **title** (`extract_alert_title`), *not* the
Grafana rule UID. This keeps a `firing` and its `✅` resolve symmetric even when
the resolve message drops the Grafana link, so the episode closes correctly.

- First firing = **root** (`root_post_id IS NULL`), handled normally.
- The `uq_active_root` partial-unique index enforces **one active root per
  episode** and resolves the concurrent first-firing race: the insert loser
  retries as a repeat. See [../persistence.md](../persistence.md).
- A resolved (`✅`) post creates **no ticket and no Jira issue** —
  `mark_episode_resolved` only stamps `resolved_at` on the open root. The next
  firing of that title becomes a fresh root. (This is enforced in
  [../domains/alerts.md](../domains/alerts.md) before a ticket exists.)

## Expected repeats (`_handle_expected_repeat`)

Every later firing of an open episode is a repeat. The repeat gets its **own**
Jira issue (created via `_ensure_jira_issue(is_repeat=True)`), then this method
annotates it against the `root`:

- Adds the `MATTERMOST_EXPECTED_INCIDENT_REACTION_NAME` reaction — the bot's
  **only** self-added reaction.
- Sets `Валидность = Ожидаемый` (`VALID_INCIDENT_EXPECTED_VALUE`) and mirrors it
  to the `validity_label` column.
- Rewrites the description to append a root-links block
  (`build_expected_alert_block`).
- Creates a real Jira **"is child of"** link to the root (`link_child_of`, type
  name auto-resolved from `JIRA_REPEAT_LINK_INWARD`) and posts a "Прилинковано
  к" notice in the thread.

**Idempotency split.** The reaction / validity / description steps are idempotent
and run on **every** delivery. The non-idempotent link + notice are guarded by
the persisted `expected_repeat_linked` flag, which is set **only after
`link_child_of` returns**. A link failure leaves the flag false so the next
delivery retries — the link is never silently lost. Each step swallows its own
`ApiError` (logged) so one failure does not abort the rest. Returns early if the
repeat or root has no `jira_issue_key` yet (upstream not ready → retried later).

## Confirmation plumbing (`_update_jira_for_confirmation`)

Called by `confirm_incident` (the `:incident:` / valid-incident flow lives in
[../domains/alerts.md](../domains/alerts.md)). It reconciles Jira's
`valid_incident` field (read-then-set, syncing back if already true), and — once
per confirmation, guarded by `jira_confirmation_comment_added` — swaps the alert
description for the postmortem template (`build_postmortem_description`) **before**
adding the confirmation comment, so a comment failure never leaves the issue
without the template (the guard skips both on retry).

## Background durability

- `process_pending_work` drains two repository queues: `list_pending_jira`
  (re-runs `_ensure_jira_issue` for rows whose Jira create never completed) and
  `list_pending_confirmations` (re-runs `confirm_incident` for confirmations that
  arrived before the issue existed — skipped until `jira_issue_key` is set). It is
  the retry engine that makes the "DB row before Jira call" invariant safe; driven
  by `pending_work_loop` (see [../operations.md](../operations.md)).
- `backfill_recent_alerts` re-feeds the last `BACKFILL_RECENT_POSTS_LIMIT` alert-
  channel posts through `handle_alert_post` on startup to catch posts missed while
  the bot was down; idempotency keys make re-processing a no-op. No-op when the
  limit is `≤ 0`. See [../config.md](../config.md) for the env vars referenced here.
