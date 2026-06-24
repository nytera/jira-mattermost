# Persistence & timezone

All DB access goes through `repository.py` (`AlertTicketRepository`, mutators via
`_mutate`). For the exact model fields and method list see
[`reference/service-map.md`](reference/service-map.md); this file covers the
non-obvious behavior.

## Schema bootstrap & migrations

`init_db()` runs `Base.metadata.create_all` at startup **and** applies small
backward-compatible `ALTER TABLE` additions, so no separate migration step is needed
locally. The `migrations/*.sql` files are the hand-maintained **reference** schema,
kept aligned with the SQLAlchemy model by hand (there is no Alembic).

When you change schema behavior, keep `migrations/`, the model, and the startup
init expectations aligned.

`normalize_database_url` rewrites `postgres://` / `postgresql://` to
`postgresql+psycopg://`. See `DATABASE_URL` in [`config.md`](config.md).

## Tables

- **`alert_tickets`** — the core table. Notable columns: `mattermost_post_id`
  (unique index), `mattermost_alert_title` (extracted from the alert's first line),
  `jira_issue_key`, `valid_incident`, `incident_post_id`, episode columns
  (`root_post_id`, `resolved_at`, `validity_label`, `expected_repeat_linked`), and
  the `creation_status` / `confirmation_status` state machines.
- **`alert_feedback`** — `mattermost_post_id`, `user_id`, display name, message text,
  created-at.
- **`app_settings`** — a `key`/`value` store for runtime-editable config (the
  debug-panel LLM prompt overrides), managed by `get_setting` / `set_setting` /
  `delete_setting`. See [`domains/debug.md`](domains/debug.md).

## Idempotency keys

Re-delivered Mattermost events must be no-ops. The guards live in `AlertTicket`:

- `jira_issue_key` — a repeated `posted` event sees the existing key and skips create.
- `incident_post_id` — a repeated confirmation returns the existing incident and does
  not publish a second incident post.
- `jira_confirmation_comment_added` — the confirmation comment is added once.
- `postmortem_comment_added` — set after the PM comment is posted, so repeated
  checkmark/validity reactions never duplicate the postmortem.
- `creation_status` / `confirmation_status` — state machines driving retry/recovery
  (see [`operations.md`](operations.md)).

The Jira issue is created only **after** the row with the unique `mattermost_post_id`
is inserted, so a crash mid-create is retried, not lost. If Jira already returned
`Valid Incident = Валидный`, the local `valid_incident` is synced.

### `uq_active_root`

A partial-unique index enforcing **one active root per episode**. It also resolves the
concurrent first-firing race: the loser falls back to being a repeat. Episode and
expected-repeat semantics live in [`domains/jira-sync.md`](domains/jira-sync.md).

## Timezone

All persisted/displayed times go through `domain.backend_now()` /
`backend_datetime()`, which use `INCIDENT_TIMEZONE` (default `Europe/Moscow`),
configured once in `Settings.__post_init__` via `configure_runtime_timezone`. A naive
persisted value is **localized to the runtime timezone (not assumed UTC)** before any
duration math (e.g. `JIRA_TIME_TO_FIX_FIELD`). See
[`domains/incidents.md`](domains/incidents.md).
