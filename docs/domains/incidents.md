# Incidents (IncidentMixin)

`IncidentMixin` owns the **incident-channel** lifecycle: manual incident posts and
their duty ping, validity + END-time stamping, finalize-with-postmortem, and
publishing the incident-channel message when an alert is confirmed. Method shapes
live in [../reference/service-map.md](../reference/service-map.md); this doc covers
the invariants and the WHY.

> `IncidentMixin` is a domain mixin of `IncidentBotService` — see [architecture.md](../architecture.md) for how the service is assembled.

## Two kinds of incident

There are two kinds of incident, both driven by **emoji reactions** (the validity
reactions, the ✅ checkmark, the 📝 summary emoji):

- **Manual** — a human types a root post directly in
  `MATTERMOST_INCIDENT_CHANNEL_ID`. `handle_manual_incident_post` pre-creates the
  ticket row (`create_or_get_incident_thread`, idempotent) and pings on-call. No
  Jira issue exists yet — it is created when the incident is **closed** (checkmark /
  validity reaction → postmortem path) or from the admin API.
- **Alert-originated** — confirmed via the `incident` reaction (see
  [../domains/alerts.md](../domains/alerts.md)). `confirm_incident` publishes the
  incident-channel message (the issue already exists).

## handle_manual_incident_post

Only **root** posts from real users qualify — thread replies, system messages,
and bot/webhook posts (`_is_bot_post`) are skipped. When `MATTERMOST_DUTY_MENTION`
is set, a bare duty mention is posted (`_post_incident_thread_mention`, unboxed so
the `@group` ping fires) so the manual incident gets noticed; the checkmark flow is
the action path. A single **duty cheat-sheet** (`DUTY_HELP_ENABLED`, default true)
is posted after the create guard.

The handler short-circuits **before** the ticket row only when there is genuinely
nothing to post: no mention and help disabled. Idempotency rests on
`create_or_get_incident_thread` returning `created=False` on redelivery.

## Finalize: handle_incident_checkmark

The checkmark emojis (`white_check_mark`/`heavy_check_mark`/`ballot_box_with_check`,
`INCIDENT_END_REACTION_NAMES`) are the **`Валидный` shortcut**: they finalize the
incident — set END-time, write the Jira description template (no narrative comment),
and turn the title green. The fact-based narrative summary is **not** generated on
close; it is button-only (the `memo` emoji, [../domains/thread-summary.md](../domains/thread-summary.md)).

Key invariants:

- **END-time and title come from one LLM call.** Before any Jira write,
  `handle_incident_checkmark` calls `_resolve_incident_closeout` (PostmortemMixin)
  **once**: it returns both the inferred recovery time and a short `[INC] …` title.
  The END value substitutes the reaction `ended_at` and flows into **both**
  `apply_incident_end_time` and the postmortem; the title flows into the Jira issue
  summary. On no-LLM / `ApiError` / `UNKNOWN` / unparseable / out-of-range END it
  **falls back to the reaction timestamp** (title → `extract_alert_title`). The
  resolution / validation rule lives in [../domains/postmortem.md](../domains/postmortem.md).

- **Validity reactions finalize too.** Unlike the alert channel (where
  `Ложный`/`Ожидаемый` are label-only — see [../domains/alerts.md](../domains/alerts.md)),
  the same reactions on an **incident-thread root** route here with
  `validity_label=...` and **both** stamp `Валидность` **and** finalize with a
  postmortem, exactly like the checkmark.
- **Finalize is idempotent.** Once `postmortem_comment_added` is set (the finalize
  marker; no comment is posted), a second checkmark/validity reaction returns early
  ("postmortem left unchanged") and only updates the `Валидность` field via
  `_set_incident_validity` (which also posts the templated "Валидность обновлена"
  notice). The flag-keyed early return means the close is never re-run — this holds
  for **both** manual and alert-originated incidents.
- **Validity and confirmation are independent axes.** PM generation only stamps
  `Валидный` as a *default* when `ticket.validity_label is None`, so an explicit
  earlier `Ложный`/`Ожидаемый` survives the finalize step.
- **No-LLM branch.** When `self.llm is None`, the closeout/finalize is skipped, but
  the checkmark still writes `Валидность` and still applies the END-time — this is the
  non-obvious half of "🏁 needs `LLM_API_TOKEN`": only the title resolution requires it.
- **Title goes green even on finalize failure** — once the END-time is in Jira, leaving
  the title red would misrepresent a closed incident, so
  `_mark_incident_post_completed` runs whenever the incident ended.

The Jira issue plumbing (description template, author/participants, title, fields)
lives in [../domains/postmortem.md](../domains/postmortem.md) — this mixin only
orchestrates; do not duplicate it here.

## apply_incident_end_time

Sets `JIRA_END_FIELD` to the `ended_at` passed by `handle_incident_checkmark`
(the LLM-resolved recovery time, or the reaction time as fallback — see
above) and best-effort `set_time_to_fix` off the same value.
Ignored (no error) when the post is unknown, the incident is not confirmed
(`valid_incident` false), or no Jira issue exists. Returns `INCIDENT_ENDED` on
success, `ERROR` on a failed Jira write (recorded as `last_error`, retried).

## _mark_incident_post_completed

Recolors every attachment box to `INCIDENT_DONE_COLOR` and swaps the status label
`**Новый инцидент**` → `**Закрытый инцидент**` in whichever box carries it (the
detail box) via `update_post`. Searching by content rather than a fixed index keeps
it working across the box layout, including the no-forwarded-attachment case. The
Jira link on the status line and the rest of the box are preserved. **Manual
incidents are skipped**: the "incident post" is the human's own message
(`incident_post_id == mattermost_post_id`), which the bot must not rewrite. Only the
bot-authored alert-originated message carries the editable status label.
Best-effort — a failed edit never breaks finalize.

## confirm_incident / _publish_incident_message_if_needed

`confirm_incident` is the alert→valid-incident bridge: it publishes the incident
message, writes Jira
(`_update_jira_for_confirmation`, see [../jira.md](../jira.md)), and replies in the
alert thread. If the Jira issue is not ready it is saved `pending_confirmation`
(`PENDING_JIRA`) and completed by the pending-work loop. Already-confirmed posts
short-circuit (`ALREADY_CONFIRMED`).

`_publish_incident_message_if_needed` renders the incident post as **three stacked
attachment boxes**, all `INCIDENT_OPEN_COLOR` (red → green on close):

1. **Title box** — just the alert name as a heading, `##### <название>` via
   `format_incident_title` / `extract_alert_title`, so the incident is identifiable
   at a glance.
2. **Detail box** (`format_incident_message`) — the first line is the status label
   carrying the Jira link, `**Новый инцидент** — [KEY](url)`, which the close swap in
   `_mark_incident_post_completed` above flips to `**Закрытый инцидент**`; then bullets
   for the source-alert link, the author `@mention`, and the alert time. When the alert
   has no forwarded attachment block, its full body is embedded here so it isn't lost.
3. **Forwarded alert box(es)** — a copy of the original alert's attachments.

The post `message` is empty. It is guarded by `incident_post_id` so it publishes once.
After publishing, when `DUTY_HELP_ENABLED`, it posts the incident-thread cheat-sheet.

The target channel is `_incident_channel_for(ticket)`: normally the real incident
channel, but a ticket whose alert originated in the (read-only) test alert channel
routes to the test incident channel so the shadow's test traffic runs a full live
incident thread there — see [../read-only.md](../read-only.md). The validity and
read-only Jira-params replies route through the same helper, so they land in the same
channel as the incident post they thread under.

## See also

- Reactions / allowlist / channel routing: [../domains/alerts.md](../domains/alerts.md)
- Postmortem generation: [../domains/postmortem.md](../domains/postmortem.md)
- Jira field writes & test-mode stubs: [../jira.md](../jira.md)
- Env vars (`MATTERMOST_*`, `DUTY_HELP_ENABLED`, `LLM_API_TOKEN`): [../config.md](../config.md)
- Method signatures: [../reference/service-map.md](../reference/service-map.md)
