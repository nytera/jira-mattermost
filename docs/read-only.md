# Read-only / shadow mode

Run a new version of the bot **in parallel with prod** on the same real Mattermost
events, where the shadow **changes nothing** in any external system (Jira, the real
Mattermost channels) and has **zero impact on prod**. Everything the bot *would* do
is reproduced in a dedicated **audit channel**, so that channel reads as "what the
bot would do right now if it were prod."

Enable with `READ_ONLY_MODE=true` (see [config.md](config.md) for the full env list).

## The invariant

> In read-only mode the **only** permitted write is to the audit channel. Every
> other write (Jira, the real Mattermost channels, the ops channel) is suppressed
> and mirrored there. Reads against shadow-minted ids (`readonly-…` posts, the
> `ADS-TEST` Jira key) never hit the real APIs; real ids are read normally.

## How writes are suppressed

Two layers, primary + backstop:

1. **Client-method level (primary).**
   - **Jira** (`jira.py`): every mutator (`create_issue`, `set_end_time`,
     `set_time_to_fix`, `set_validity`, `set_description`, `add_comment`,
     `link_child_of`) is a no-op in read-only; `create_*` return an `ADS-TEST-<postid>`
     stub. `get_valid_incident` returns `None`. See [jira.md](jira.md).
   - **Mattermost** (`mattermost.py`): `create_post` / `add_reaction` / `update_post`
     are redirected to the `AuditMirror`; `open_dialog` (deprecated) is dropped.
2. **`_request` backstop (`http.py`).** Any write method (POST/PUT/PATCH/DELETE) that
   reaches the HTTP layer in read-only **raises** unless it is explicitly marked
   `allow_in_read_only=True`. Only the audit post (and pure POST-reads like
   `/users/usernames`) carry that flag. So a write that slips past the primary layer
   fails loudly instead of mutating prod.

## The audit channel is a full mirror

No Jira-operation log is needed: every prod-visible bot action that writes to Jira
is accompanied by a Mattermost thread reply (e.g. "Создана задача", "🟢 Инцидент
закрыт", "Валидность обновлена"), and those replies are auto-mirrored. So the audit
channel shows the real content for free.

`AuditMirror` (`audit.py`):

- Posts into `MATTERMOST_AUDIT_CHANNEL_ID` via the one permitted write.
- **Reproduces threads.** An in-memory map (bounded LRU) links each original thread
  root — a real post id, or a `readonly-` stub the shadow minted — to its audit
  post, so replies land under the right audit root and reactions/updates target the
  right audit post. A reply to a root not yet mirrored gets an anchor post first.
- **Strips correlation props** (`mattermost_alert_post_id`/`mattermost_incident_post_id`)
  from audit copies, so the shadow can never treat its own audit post as a prod
  artifact.
- Is **best-effort**: a failed audit write is logged at WARNING and never breaks the
  shadow's event processing.

The audit channel **must be dedicated** — distinct from every alert/incident/test/ops
channel. Startup refuses to boot on a collision (`_assert_audit_channel_isolated` in
`web.py`), because the one permitted write would otherwise land in a real channel.

## Test channels vs real channels

The shadow treats configured **test channels** (`MATTERMOST_TEST_ALERT_CHANNEL_ID`,
`MATTERMOST_TEST_INCIDENT_CHANNEL_ID`) as first-class alert/incident channels, so you
can push test traffic through the same path as prod traffic. The channel predicates
`_is_alert_channel` / `_is_incident_channel` / `_is_test_channel` (in `SharedMixin`)
encode "real ∪ test".

## Deploying a shadow

- **Separate `DATABASE_URL`.** The shadow keeps its own idempotency state; it must
  not share prod's DB.
- **Its own bot account / token**, joined to every channel it reads (alert, incident,
  test-alert, test-incident) and the audit channel. The shadow opens its own
  websocket connection.
- **Its own `PORT`** (and `HOST` if needed) to run next to prod on one host.
- Leave `ENABLE_BACKFILL_ON_STARTUP=false` (the default) so the shadow processes only
  new events.

## Limitations

- After a restart the thread map is lost, so older threads mirror flat (new replies
  start fresh audit roots) until they are seen again.
- The shadow stores `readonly-` stub ids as its own incident post id, so any
  permalink it builds for that post is a dead link — visible only inside mirrored
  audit content.
- Mirrored text is copied verbatim, so a duty mention (`MATTERMOST_DUTY_MENTION`,
  posted as literal `@group` text) appears in the audit copy too. Mattermost only
  notifies mentioned users who can see the channel, so keep the audit channel
  private to the operator to avoid pinging the on-call from shadow traffic — a
  deployment precondition for "zero prod impact."
