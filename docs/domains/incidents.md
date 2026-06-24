# Incidents (IncidentMixin)

`IncidentMixin` owns the **incident-channel** lifecycle: manual incident posts and
their duty ping / "Создать задачу" card, the interactive controls card and its
buttons, validity + END-time stamping, finalize-with-postmortem, and publishing
the incident-channel message when an alert is confirmed. Method shapes live in
[../reference/service-map.md](../reference/service-map.md); this doc covers the
invariants and the WHY.

## Two entry points, one controls card

There are two kinds of incident, and they converge on the **same controls card**:

- **Manual** — a human types a root post directly in
  `MATTERMOST_INCIDENT_CHANNEL_ID`. `handle_manual_incident_post` pre-creates the
  ticket row (`create_or_get_incident_thread`, idempotent) and offers a
  **➕ Создать задачу** card. No Jira issue exists yet — it is created on the
  button click (or the checkmark).
- **Alert-originated** — confirmed via the `incident` reaction / 🚨 button (see
  [../domains/alerts.md](../domains/alerts.md)). `confirm_incident` publishes the
  incident-channel message and `_publish_incident_message_if_needed` posts the
  **same controls card minus "Создать задачу"** (the issue already exists).

Both cards then expose the validity menu (`Ложный`/`Ожидаемый`/`Валидный`),
**🏁 Завершить**, and **📝 Саммари**. `_incident_controls_attachment` decides the
task header automatically: shown for alert-originated incidents (where
`incident_post_id != mattermost_post_id`), omitted for manual ones.

## handle_manual_incident_post

Only **root** posts from real users qualify — thread replies, system messages,
and bot/webhook posts (`_is_bot_post`) are skipped. Behavior by mode:

- **Interactive** (`SERVICE_PUBLIC_URL` + `INTERACTIVE_BUTTONS_ENABLED` ≠ false):
  post the card with `MATTERMOST_DUTY_MENTION` as the message **text above** it.
  The mention must be in the post text, not the attachment — attachment text does
  not fire an `@group` ping.
- **Emoji-only**: no card, but still post a bare duty mention
  (`_post_incident_thread_mention`, unboxed so the ping fires) so the manual
  incident gets noticed; the checkmark flow is the action path.
- A single **duty cheat-sheet** (`DUTY_HELP_ENABLED`, default true) is posted
  after the create guard, common to every branch.

The handler short-circuits **before** the ticket row only when there is genuinely
nothing to post: no card, no mention, and help disabled. Idempotency rests on
`create_or_get_incident_thread` returning `created=False` on redelivery.

## Buttons: handle_incident_action

Dispatched by `incident_post_id` (the manual ticket's own `mattermost_post_id`),
so it never touches alert-channel paths. Actions:

- `create_task` → `_incident_create_task`: creates the Jira issue with **no
  alert-only fields** (`create_postmortem_issue`), announces it to ops, and the
  action response swaps the card for the controls card.
- `validity` → `apply_validity_label` (lightweight Jira-field write, see
  [../jira.md](../jira.md)). The label is keyed by the ticket's
  `mattermost_post_id`; for alert-originated incidents that differs from
  `incident_post_id`, so the ticket is resolved via `get_by_incident_post_id`
  first.
- `end_incident` → reuses `handle_incident_checkmark` (full finalize).
- `summary` → `generate_thread_summary` (thread-only, no Jira).

## Finalize: handle_incident_checkmark

The checkmark emojis (`white_check_mark`/`heavy_check_mark`/`ballot_box_with_check`,
`INCIDENT_END_REACTION_NAMES`) are the **`Валидный` shortcut**: they finalize the
incident — set END-time, generate the postmortem, and turn the title green.

Key invariants:

- **Validity reactions finalize too.** Unlike the alert channel (where
  `Ложный`/`Ожидаемый` are label-only — see [../domains/alerts.md](../domains/alerts.md)),
  the same reactions on an **incident-thread root** route here with
  `validity_label=...` and **both** stamp `Валидность` **and** finalize with a
  postmortem, exactly like the checkmark.
- **Postmortem is idempotent.** Once `postmortem_comment_added` is set, a second
  checkmark/validity reaction returns early ("postmortem left unchanged") and only
  updates the `Валидность` field via `_set_incident_validity` (which also posts the
  templated "Валидность обновлена" notice). The PM comment is additive, so it is
  never re-posted — this holds for **both** manual and alert-originated incidents.
  (Some prose claims a repeated checkmark on a manual incident regenerates the PM;
  that is stale — trust the flag-keyed early return.)
- **Validity and confirmation are independent axes.** PM generation only stamps
  `Валидный` as a *default* when `ticket.validity_label is None`, so an explicit
  earlier `Ложный`/`Ожидаемый` survives the finalize step.
- **No-LLM branch.** When `self.llm is None`, the postmortem is skipped, but the
  checkmark still writes `Валидность` and still applies the END-time — this is the
  non-obvious half of "🏁 needs `LLM_API_TOKEN`": only the PM text requires it.
- **Title goes green even on PM failure** — once the END-time is in Jira, leaving
  the title red would misrepresent a closed incident, so
  `_mark_incident_post_completed` runs whenever the incident ended.

Postmortem **generation** itself (template, author/participants, LLM title, the
report comment) lives in [../domains/postmortem.md](../domains/postmortem.md) —
this mixin only orchestrates; do not duplicate it here.

## apply_incident_end_time

Sets `JIRA_END_FIELD` to the reaction/button time and best-effort `set_time_to_fix`.
Ignored (no error) when the post is unknown, the incident is not confirmed
(`valid_incident` false), or no Jira issue exists. Returns `INCIDENT_ENDED` on
success, `ERROR` on a failed Jira write (recorded as `last_error`, retried).

## _mark_incident_post_completed

Edits the incident-channel message title from `🔴 Инцидент открыт` to
`🟢 Инцидент закрыт` (first attachment's text + `INCIDENT_DONE_COLOR`) via
`update_post`. **Manual incidents are skipped**: the "incident post" is the human's
own message (`incident_post_id == mattermost_post_id`), which the bot must not
rewrite. Only the bot-authored alert-originated message carries the editable title.
Best-effort — a failed edit never breaks finalize.

## confirm_incident / _publish_incident_message_if_needed

`confirm_incident` is the alert→valid-incident bridge (also reachable via
`/incident <permalink>`): it publishes the incident message, writes Jira
(`_update_jira_for_confirmation`, see [../jira.md](../jira.md)), and replies in the
alert thread. If the Jira issue is not ready it is saved `pending_confirmation`
(`PENDING_JIRA`) and completed by the pending-work loop. Already-confirmed posts
short-circuit (`ALREADY_CONFIRMED`).

`_publish_incident_message_if_needed` renders incident details (title, Jira/alert
links, confirmer `@mention`, time) in a **gray attachment block**
(`INCIDENT_OPEN_COLOR`) placed *above* the forwarded alert attachment(s); the post
`message` is empty. It is guarded by `incident_post_id` so it publishes once. After
publishing it posts the controls card (no "Создать задачу") and, when
`DUTY_HELP_ENABLED`, the incident-thread cheat-sheet.

## See also

- Reactions / allowlist / channel routing: [../domains/alerts.md](../domains/alerts.md)
- Postmortem generation: [../domains/postmortem.md](../domains/postmortem.md)
- Jira field writes & test-mode stubs: [../jira.md](../jira.md)
- Env vars (`MATTERMOST_*`, `INTERACTIVE_BUTTONS_ENABLED`, `DUTY_HELP_ENABLED`,
  `LLM_API_TOKEN`): [../config.md](../config.md)
- Method signatures: [../reference/service-map.md](../reference/service-map.md)
