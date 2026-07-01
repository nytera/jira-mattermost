# Read-only / shadow mode

Run a new version of the bot **in parallel with prod** on the same real Mattermost
events, where the shadow **changes nothing** in any external system (Jira, the real
Mattermost channels) and has **zero impact on prod**. Everything the bot *would* do
is reproduced in a dedicated **audit channel**, so that channel reads as "what the
bot would do right now if it were prod."

Enable with `READ_ONLY_MODE=true` (see [config.md](config.md) for the full env list).

## The invariant

> In read-only mode the only permitted **Mattermost** writes are to the audit
> channel and to the configured **test channels** (the live sandbox — see below).
> Every other Mattermost write (the real alert/incident/ops channels) is suppressed
> and mirrored to audit; **every Jira write is suppressed** unconditionally. Reads
> against shadow-minted ids (`readonly-…` posts, the `ADS-TEST` Jira key) never hit
> the real APIs; real ids are read normally.

## How writes are suppressed

Two layers, primary + backstop:

1. **Client-method level (primary).**
   - **Jira** (`jira.py`): every mutator (`create_issue`, `set_end_time`,
     `set_time_to_fix`, `set_validity`, `set_description`, `add_comment`,
     `link_child_of`) is a no-op in read-only; `create_*` return an `ADS-TEST-<postid>`
     stub. `get_valid_incident` returns `None`. See [jira.md](jira.md).
   - **Mattermost** (`mattermost.py`): `create_post` / `add_reaction` / `update_post`
     are redirected to the `AuditMirror`.
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

## Computed Jira fields surfaced on close

Some Jira writes carry no Mattermost reply of their own, so suppressing them would
drop the value silently — notably the **end time** and **Time-to-Fix** at incident
close, and Time-to-Fix on an alert validity change. In read-only mode the shadow
still computes these (it even runs the LLM end-time inference) and posts them as a
code block into the audit thread — "the Jira fields prod would have written" —
instead of letting them vanish into the no-op. End time uses the exact Jira wire
format. See `format_readonly_jira_params` (`jira_payload.py`).

## Test channels — a live sandbox

The shadow treats configured **test channels** (`MATTERMOST_TEST_ALERT_CHANNEL_ID`,
`MATTERMOST_TEST_INCIDENT_CHANNEL_ID`) as first-class alert/incident channels, so you
can push test traffic through the same path as prod traffic. The channel predicates
`_is_alert_channel` / `_is_incident_channel` (in `SharedMixin`) encode "real ∪ test"
— but only under `read_only_mode`, so a leftover test-channel env var never routes
real traffic into the live path in a normal deployment.

Unlike the real channels, test-channel Mattermost traffic is **live, not mirrored**:
the bot writes real posts, edits and reactions straight into the test channels
instead of copying them to audit. So a test alert drives a real, self-contained
incident thread you can actually drive — an alert in the test alert channel confirms
into an incident **posted to the test incident channel** (`_incident_channel_for` in
`_incidents.py`), and a ✅ there closes it and flips its title green in place, all
without touching prod. The one thing that stays test is Jira: the ticket keeps the
`ADS-TEST` stub (Jira is suppressed globally by read-only), so no real issue is ever
created.

The live-write bypass lives in `MattermostClient`: `create_post` to a test channel
does the real write and records the post id in `_live_post_ids`; follow-up
`update_post` / `add_reaction` on a remembered id also go live. The set is bounded
and **in-memory** (lost on restart) — after a restart, edits/reactions on an older
test post fall back to the audit mirror until the post is seen again.

Because a test incident channel is a channel the bot *processes* (not audit, which it
never reads), the bot's own live posts echo back over the websocket. That is safe:
`handle_manual_incident_post` ignores bot-authored posts (`_is_bot_post`),
`handle_reaction` ignores the bot's own reactions, and `post_edited` events are not
dispatched — so the green-flip edit never re-triggers anything.

## Adopting prod artifacts

For traffic in the **real** alert/incident channels the shadow doesn't just mint
stubs — it adopts the real artifacts the prod bot produces, so the audit channel
shows real Jira links and the shadow can track real incident closures.

The prod bot tags its own posts with back-references: a reply in an alert thread
or the incident message carries `props.mattermost_alert_post_id` (the source
alert), and bot replies/incident posts carry `props.jira_issue_key`. Humans can't
set props, and the shadow strips these keys from its own audit copies, so **a post
in a real alert/incident channel carrying `mattermost_alert_post_id` is a prod
artifact** — both the correlation key and the prod detector in one.

`_observe_prod_artifact` (`coordinator.py`) runs first for every posted event in
read-only mode and:

- **Gates positively** — only the real alert and incident channels. This excludes
  test, audit, and every other channel, so the shadow can never adopt its own
  audit post (the prop strip is then only defense-in-depth). Test channels keep
  the stub flow.
- **Correlates** `get_by_post_id(mattermost_alert_post_id)` to the shadow's ticket.
- **Adopts the real Jira key** (`props.jira_issue_key`), replacing the
  `ADS-TEST-…` stub. Idempotent and self-healing: it replaces only while the key
  is still a stub, so the first prod notice wins and a missed early adoption is
  retried by the next one. The real link surfaces in the audit channel via a
  one-time adoption note (the original "Создана задача" reply keeps the stub).
- **Adopts the real incident post id** from the incident **root** post into the
  separate `prod_incident_post_id` column, and aliases it to the shadow's own
  incident audit thread (`AuditMirror.adopt_alias`). `get_by_incident_post_id`
  matches `incident_post_id` first, then `prod_incident_post_id`, so a later ✅ on
  the **real** prod incident post resolves to the shadow's ticket. The shadow then
  runs its own LLM postmortem over the real incident thread and mirrors the
  result into the audit incident thread — testing the postmortem/summary
  formatting against real content, with every Jira write still suppressed.

A prod artifact the shadow can't correlate (it never saw the source alert, e.g.
started mid-incident) is consumed without adoption.

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

- The shadow sees an incident **closure** only via the ✅ **emoji reaction** — the
  sole closure path now that interactive buttons are removed.
- Prod's bot adds an "Ожидаемый" reaction on repeat alerts. The shadow's
  bot-reaction ignore keys on the *shadow's own* bot id, so a prod-bot reaction is
  not ignored — in read-only that only produces extra audit noise (the Jira write
  is no-op'd), never prod impact.
- Adoption doubles LLM token spend: the shadow generates its own postmortem/summary
  over the same real thread that prod already processed. Expected.
- The **audit-mirrored** incident message's title does not turn green on close: the
  bot would edit the original message's attachments, but for a real-channel incident
  the shadow stores a `readonly-` stub id for it and the stub read-back carries no
  attachments to re-edit. Closure is still visible in the audit thread (the "🟢
  Инцидент закрыт" notice and the postmortem) — only the root message's colour is not
  flipped. (A **test-channel** incident is a real post, so it *does* flip green.)
- The test-channel sandbox exercises the Mattermost lifecycle, not Jira read-back:
  Jira stays stubbed, and `get_valid_incident` returns `None` in read-only, so
  repeat/episode grouping and expected-repeat linking don't fire there. The core path
  (alert → confirm → incident in the test channel → ✅ close → green) works fully;
  anything that depends on reading a real Jira issue does not.
- A firing alert that never becomes a confirmed incident is announced by prod with a
  single `jira_issue_key`-bearing post. If the shadow observes that post before its
  own ticket/stub exists (a timing inversion possible under load), adoption is
  skipped with no retry and the audit mirror keeps the `ADS-TEST` stub for that alert.
  Audit fidelity only — never prod impact. Confirmed incidents self-heal (prod emits
  several `jira_issue_key`-bearing notices, so a later one re-adopts).
- After a restart the thread map is lost, so older threads mirror flat (new replies
  start fresh audit roots) until they are seen again. The persisted
  `prod_incident_post_id` survives, so a ✅ on an already-adopted incident still
  resolves — only the thread grouping is lost.
- The shadow stores `readonly-` stub ids as its own incident post id, so any
  permalink it builds for that post is a dead link — visible only inside mirrored
  audit content.
- Mirrored text is copied verbatim, so a duty mention (`MATTERMOST_DUTY_MENTION`,
  posted as literal `@group` text) appears in the audit copy too. Mattermost only
  notifies mentioned users who can see the channel, so keep the audit channel
  private to the operator to avoid pinging the on-call from shadow traffic — a
  deployment precondition for "zero prod impact."
