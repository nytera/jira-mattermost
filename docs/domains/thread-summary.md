# Thread summary (ThreadSummaryMixin)

`ThreadSummaryMixin` (`src/mm_jira_bot/service/_thread_summary.py`) publishes an
LLM-generated factual summary of a Mattermost thread as a visible reply in that
thread. It is the engine behind both the 📝 Summary button and the configurable
summary emoji (`MATTERMOST_SUMMARY_REACTION_NAME`, default `memo`), working in
**any** channel/thread — alert, incident, or manual. For signatures, see
[../reference/service-map.md](../reference/service-map.md).

> `ThreadSummaryMixin` is a domain mixin of `IncidentBotService` — see [architecture.md](../architecture.md) for how the service is assembled.

## Scope and boundaries

- **Owned here:** `generate_thread_summary` (the public entry) plus the
  placeholder/stream/finalize plumbing (`_publish_thread_summary`,
  `_post_summary_placeholder`, `_make_summary_stream_callback`,
  `_generate_and_finalize_summary`, `_edit_summary_reply`, `_set_summary_status`).
- **Pure helpers** live in `src/mm_jira_bot/summary.py`:
  `format_thread_summary_reply`, `format_thread_summary_streaming`,
  `neutralize_mentions`.
- **NOT touched:** Jira. A summary only reads the thread and posts a reply — no
  issue, comment, transition, or field write. Authorization (the allowlist) is
  enforced by the callers (reaction / button dispatch), not here.

## What it does

`generate_thread_summary` collects the thread via `_postmortem_thread_context`
(the same transcript builder the postmortem uses), then `_publish_thread_summary`:
posts an `⏳ Генерация саммари…` placeholder reply, runs a **single** LLM call,
and edits the placeholder into the final summary (or an error notice on
`ApiError`). It returns an `ActionResult` whose `message` is the ephemeral
feedback shown to the requester.

- Resolves the thread root first: if the reacted/clicked post is a reply, it
  fetches `root_id` (falling back to the original post if that lookup fails) so
  the summary always lands on the root.
- The result is published as a boxed thread reply (`_box_thread_reply`,
  `NOTICE_ATTACHMENT_COLOR`), carrying `summary_requested_by_user_id` and the
  thread-routing key in `props`.

See [../domains/alerts.md](../domains/alerts.md) for the button/reaction dispatch
that invokes this, and [../config.md](../config.md) for the env vars.

## Key invariants

- **Shared prompt template, separate LLM call.** The summary uses the same
  `DEFAULT_INCIDENT_REPORT_PROMPT` / `build_incident_report_prompt` as the
  postmortem (template key `llm_summary_prompt`, resolved via
  `_resolve_prompt_template`: DB override → `LLM_SUMMARY_PROMPT`[`_FILE`] →
  built-in default). It is **not** derived from the Jira postmortem — it is its
  own `llm.generate_summary` call. See [../domains/postmortem.md](../domains/postmortem.md).
- **No LLM ⇒ no-op.** When `self.llm` is `None` (`LLM_API_TOKEN` unset),
  `generate_thread_summary` posts nothing and returns an ephemeral
  "Саммари недоступно: LLM не настроен." — the path is otherwise inert.
- **Placeholder → edit, not delete/repost.** The pending reply is created up
  front for wait feedback, then re-rendered in place. `update_post` replaces
  props wholesale, so every edit re-boxes from `base_props` to keep the routing
  keys. If the placeholder post failed, finalize falls back to a fresh reply so
  the summary still lands.
- **Streaming (`LLM_STREAM=true`).** When a placeholder exists,
  `_make_summary_stream_callback` live-edits it as the LLM streams. The shared
  streaming contract (cumulative-not-delta, the callback never raises, throttle
  knobs) is owned by [../domains/postmortem.md](../domains/postmortem.md). This
  flow honours it concretely: the callback force-renders when the buffer
  **shrinks** (retry restart), and its edit goes through `_edit_summary_reply`,
  which swallows `ApiError` so a transient edit blip can't escape into `_retry`.
  `last_edit_time` is seeded at callback creation so the first stream edit respects
  the interval after any preceding status edit. The final edit always overwrites
  the streaming render with the clean format.
- **Never pings.** The LLM emits `@username` mentions (for the Jira
  `[~username]` rendering); `summary.neutralize_mentions` strips the leading `@`
  to plain text on the Mattermost path so a summary never notifies anyone (emails
  like `user@host` are left intact). The streaming render also carries a
  `_(генерируется…)_` marker so a partial never reads as final.

## Reuse note

`_publish_thread_summary` here is the standalone path (button/emoji), where no
prior work precedes the summary, so it posts its own placeholder. The incident
**completion** flow (see [../domains/postmortem.md](../domains/postmortem.md))
reuses the lower-level helpers directly: it posts the placeholder earlier and
walks it through `_set_summary_status` ("Шаг 1/3 … 3/3") before streaming, so its
summary shares the same callback and finalize logic without going through
`_publish_thread_summary`.
