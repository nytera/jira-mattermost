# Alerts (AlertMixin)

`AlertMixin` (`src/mm_jira_bot/service/_alerts.py`) owns the full lifecycle of a post in the **alert channel**: turning a firing alert into a Jira issue, the interactive alert card (buttons + validity menu), the feedback dialog, and the lightweight validity labels (`Ложный` / `Ожидаемый`). It is one mixin of the assembled `IncidentBotService` (see `coordinator.py`); shared state (`settings` / `repository` / `mattermost` / `jira`) is wired by the coordinator, and sibling domains supply the methods it calls into. For method signatures, see [../reference/service-map.md](../reference/service-map.md).

## Scope and boundaries

- **Owned here:** `handle_alert_post`, `handle_alert_action` + `_alert_action_attachments`, `open_feedback_dialog` + `handle_feedback_dialog_submission`, `apply_validity_label`, `_alert_attachments`.
- **Delegated out** (called via `TYPE_CHECKING` stubs): Jira issue creation, episode/repeat handling, and the duty ping/cheat-sheet live in [../domains/jira-sync.md](../domains/jira-sync.md) (`_ensure_jira_issue`, `_handle_expected_repeat`). The valid-incident state machine and incident-channel behavior live in [../domains/incidents.md](../domains/incidents.md) (`confirm_incident`, `handle_incident_action`). Thread summaries come from `generate_thread_summary`. Jira field/option semantics: [../jira.md](../jira.md). Env vars: [../config.md](../config.md).

## `handle_alert_post` — alert → Jira

The entry point for every alert-channel post. Early returns (each logged, all no-ops) skip: wrong channel, the bot's own posts, system messages, posts not from the configured bot user, and thread replies (only roots create tickets).

- **Resolve invariant.** A resolved (`✅`) repost (`is_resolved_alert`) creates **no ticket and no Jira issue** — it only calls `mark_episode_resolved` to stamp the open episode's root, so the next firing of that title becomes a fresh root. The check treats a marker **anywhere on the first non-empty line** as resolved (not a strict prefix), so a markdown-wrapped title like `**✅ …**` is not mistaken for a firing; both the `✅` emoji and the `:white_check_mark:` shortcode count.
- Otherwise `create_or_classify_alert` inserts/looks up the `alert_tickets` row (unique by `mattermost_post_id`) and classifies it within its episode. The DB row exists **before** the Jira call, so a crash mid-create is retried on redelivery.
- If a row already has a `jira_issue_key` and is neither newly created nor a repeat, it returns early (`jira.issue.skipped_existing_mapping`) — idempotent on redelivery.
- Hands off to `_ensure_jira_issue(is_repeat=...)`; if it is a **repeat**, also `_handle_expected_repeat`. Finally, if the ticket is mid-confirmation (`pending_confirmation` / `failed_confirmation` / `confirming`), it completes via `confirm_incident`.

The **episode / expected-repeat model** (signature keyed on title, root vs repeat, child-of linking, duty ping/cheat-sheet suppressed on repeats) belongs to jira-sync — see [../domains/jira-sync.md](../domains/jira-sync.md). Only the resolve-closes-episode call lives here.

## `apply_validity_label` — lightweight validity path

Triggered by the two configurable reactions (`MATTERMOST_FALSE_INCIDENT_REACTION_NAME` → `Ложный`, `MATTERMOST_EXPECTED_INCIDENT_REACTION_NAME` → `Ожидаемый`) and by the validity menu. **Alert channel only** — the same reactions in the incident channel mean "close incident + postmortem" instead (see incidents).

It sets Jira's `Валидность` field via `jira.set_validity`, writes the time-to-fix (`_set_time_to_fix`), and posts a thread reply. It does **not** touch the incidents channel, add a comment, transition the issue, or engage the `valid_incident` confirmation state machine.

Invariants:
- **Last reaction wins** — each distinct label overwrites the Jira field.
- The `validity_label` column guards against re-applying the **same** label (returns `VALIDITY_SET` early, no duplicate thread reply).
- **Best-effort:** if the Jira issue is not ready, the update is skipped (`PENDING_JIRA`) — there is no `pending_work_loop` retry. A Jira `ApiError` records `set_last_error` and returns `ERROR`.

## `handle_alert_action` — interactive card

Alternative entry point for the same flows, plus thread summary. The bot cannot attach controls to the alert itself (a Grafana/user post), so `_alert_action_attachments` hangs them on the bot's own issue-created reply. Controls are emitted **only** when `_interactive_controls_enabled()` — `SERVICE_PUBLIC_URL` set **and** `INTERACTIVE_BUTTONS_ENABLED=true`; the toggle **defaults to false**, so the bot is emoji-only unless explicitly enabled. Emoji reactions are always the fallback.

The card is one reply with two stacked blocks: a blue (`#3B82F6`) main card with `Создана задача`, the `Выбрать валидность ▼` menu, and `🚨 Инцидент` / `📝 Summary` buttons; below it a gray (`#4B5563`) card with `💬 Обратная связь по алерту`.

Dispatch (each control POSTs to `/mattermost/actions/alert` with a `context`):
- `validity` menu → `selected_option` `false` / `expected` / `valid` → `apply_validity_label`.
- `incident` → `confirm_incident` (full incident flow). On success the card is re-rendered with `confirmed=True` and the real issue key/link.
- `summary` → `generate_thread_summary` (LLM; no-op if unconfigured).
- `feedback` → `open_feedback_dialog`.
- A `source=incident` context routes to `handle_incident_action` instead (incidents domain).

Invariants:
- **Authorization:** all actions require `_is_authorized` **except** `ACTION_FEEDBACK`, which is open to everyone. Unauthorized users get a thread notice and an empty result.
- Non-summary actions verify the post is in the alert channel; if no ticket/issue exists yet, it lazily runs `handle_alert_post` first.
- The handler reads the live post via `mattermost.get_post`; a lookup failure returns a user-facing error, not a crash.

## Feedback dialog

`open_feedback_dialog` opens a Mattermost dialog (`callback_id: alert_feedback`, single textarea) via `mattermost.open_dialog`; it needs both `trigger_id` and `SERVICE_PUBLIC_URL` (callback built by `feedback_dialog_callback_url`). `handle_feedback_dialog_submission` ignores cancellations, requires a non-empty comment and a known `alert_post_id` from the dialog `state`, stores a row via `repository.add_feedback`, and posts `Получили обратную связь от <user>` in the alert thread. Feedback is unauthenticated by design (open to all).

## Thread replies and notices

All alert-thread replies go through `_post_alert_thread_reply` (SharedMixin): **best-effort** — they reuse the root `post_id` as `root_id`, swallow `ApiError` so a failed notification never breaks the main flow, and box plain text into a single colored attachment. A failed reply is not retried. The duty cheat-sheet and duty mention ping are emitted by jira-sync during issue creation, not here.
