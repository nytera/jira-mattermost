# Postmortem (PostmortemMixin)

`PostmortemMixin` (`src/mm_jira_bot/service/_postmortem.py`) finalizes an incident when it is closed: it creates / plumbs the Jira issue (description template, validity, END time, time-to-fix) and posts a standalone green "incident closed" notice in the thread. The **title** and **END time** come from a single closeout LLM call (`_resolve_incident_closeout`); there is **no** LLM narrative comment in Jira and **no** auto thread summary on close — the narrative summary is button-only (the `memo` emoji, see [thread-summary.md](thread-summary.md)). Pure helpers live in `src/mm_jira_bot/postmortem.py`; the OpenAI-compatible client lives in `src/mm_jira_bot/llm.py`. For method signatures see [../reference/service-map.md](../reference/service-map.md).

> `PostmortemMixin` is a domain mixin of `IncidentBotService` — see [architecture.md](../architecture.md) for how the service is assembled.

## Scope and boundaries

- **Owned here:** `generate_incident_postmortem` (orchestration), `_ensure_postmortem_jira_issue`, `_apply_postmortem_validity`, `_set_time_to_fix`, `_resolve_incident_closeout` / `_split_closeout_answer` / `_parse_incident_end_time` (the single closeout call → END time + title — see below), `_postmortem_thread_context`.
- **Delegated out** (called via `TYPE_CHECKING` stubs): the standalone green "closed" notice and the failure reply go through `_create_thread_summary_reply` ([../domains/thread-summary.md](../domains/thread-summary.md)); this domain no longer posts a thread summary on close. The reaction/dispatch that decides to close an incident and the idempotency *guard* live in [../domains/incidents.md](../domains/incidents.md). Jira field/option semantics: [../jira.md](../jira.md). Env vars: [../config.md](../config.md).

## The report template (the in-thread summary)

The rich incident-report template — `DEFAULT_INCIDENT_REPORT_PROMPT` in `postmortem.py` (aliased `DEFAULT_POSTMORTEM_PROMPT` / `DEFAULT_SUMMARY_PROMPT`), rendered by `build_incident_report_prompt` — now drives **only** the on-demand thread summary ([thread-summary.md](thread-summary.md)). It carries the rich structure (Мета, Сводка + Описание влияния, Решение, Извлечённые уроки, Action Items as discussion suggestions, Хронология, Риски рецидива, Открытые вопросы) and a **mandatory first line** `[INC] DD.MM.YYYY - …`. The **closeout** call at close uses its own small prompt (`build_incident_closeout_prompt`), not this template.

Invariants:

- **Placeholders** `{thread_url}` / `{participants}` / `{postmortem_author}` / `{transcript}` are substituted by an **ordered `str.replace`** with `{transcript}` **last**, so arbitrary thread text containing brace-looking tokens is never re-scanned for placeholders. `{incident_thread_url}` is a legacy alias for `{thread_url}`, kept so pre-existing override files keep working.
- **Title comes from the closeout call.** The `TITLE: [INC] DD.MM.YYYY - …` line of `_resolve_incident_closeout` is fed through `extract_postmortem_summary` (falling back to `extract_alert_title` of the root post when absent) to build the **Jira issue title**; `_limit_postmortem_summary` clamps it to 120 chars / 10 words.
- **Jira path:** the LLM always emits Markdown; `markdown_to_jira_wiki` converts it to wiki markup (the v2 comment endpoint renders wiki, not Markdown) **and** turns `@username → [~username]` (clickable mentions, when MM and Jira usernames match). This is what a **summary Jira comment** (memo emoji) uses.
- **Mattermost path:** `summary.neutralize_mentions` strips the leading `@` so the thread summary never pings participants.
- **Effective template is resolved at call time** from **env (`LLM_SUMMARY_PROMPT`, plus `*_FILE`) → built-in default**.
- The `SUMMARY_SYSTEM_PROMPT` and `CLOSEOUT_SYSTEM_PROMPT` (role / quality guidelines) stay in `llm.py` **code** — they are not env-overridable.

## `generate_incident_postmortem` — the closure flow

Runs only on the final-status completion path. By the time it runs, `handle_incident_checkmark` has already made the **single** closeout LLM call (`_resolve_incident_closeout` → END time + title) and passed the resolved `title` in. Steps:

1. `_postmortem_thread_context` collects thread posts (+ the root, reinserted if missing), resolves author display names, and returns `(thread_messages, participants, postmortem_author)`. The bot user is excluded from `participants` (the transcript itself is no longer needed here — no LLM call is made).
2. Build the Jira **title** from the passed-in closeout `title` (`extract_postmortem_summary(title or "", fallback=extract_alert_title(root))`).
3. `_ensure_postmortem_jira_issue` (create + fields when missing, else short-circuit), then `set_description` (the wiki `build_postmortem_description` **template**, not an LLM report). Mark the idempotency marker (below). **No** `add_comment` — the LLM narrative is not posted to Jira on close.
4. Post the standalone green "Инцидент закрыт" notice (below).

**Error handling:** any `ApiError` in steps 1–3 records `mark_postmortem_failed`, posts a fresh retry-able failure reply (`_create_thread_summary_reply`), and returns `ConfirmationStatus.ERROR`. The green notice (step 4) is reached only after the Jira writes succeeded.

**Idempotency:** on success this domain *sets* `mark_postmortem_comment_added` (kept as the finalize marker even though no comment is posted). The re-entry **guard** that consumes the `postmortem_comment_added` marker lives upstream — see [../domains/incidents.md](../domains/incidents.md).

## Jira field plumbing

### `_ensure_postmortem_jira_issue`

Creates the issue if missing (then `attach_jira_issue`, announce to ops, set validity / END / time-to-fix, `mark_confirmed`). For an **existing** issue it short-circuits but still honours one subtle rule, captured in code: **validity and confirmation are independent axes** — a validity emoji (`validity_label`) must update the field even on an already-confirmed incident, and END / time-to-fix / `mark_confirmed` only run when the incident is not yet confirmed.

### `_apply_postmortem_validity`

The `validity_label` (a Ложный / Ожидаемый choice made *now* in the incident thread) **wins** over any earlier value (`set_validity` + persist). With no choice and none previously recorded, default to **Валидный** (`set_valid_incident(True)`); an earlier explicit Ложный / Ожидаемый is left untouched (it was already pushed when picked).

### `_resolve_incident_closeout` (END time + title, one call)

Called once from `handle_incident_checkmark` (incidents) before any Jira write. A
single small LLM call (`resolve_incident_closeout`, prompt `build_incident_closeout_prompt`)
reads the thread chronology and returns **two** lines — `END:` and `TITLE:` — split by
`_split_closeout_answer`. The **END time** is accepted only when it parses and lands
within `[start, now + margin]` (`set_end_time` has no range guard of its own); on
no-LLM / `ApiError` / `UNKNOWN` / unparseable / out-of-range it falls back to the
reaction timestamp. The resolved END value flows into both the END field and
time-to-fix; the **title** flows into the Jira issue summary (empty → `extract_alert_title`
fallback downstream).

### `_set_time_to_fix`

Best-effort write of the incident duration in **minutes** to `JIRA_TIME_TO_FIX_FIELD`. It **must not break closure**: unlike `set_end_time`, the call is wrapped in try/except + log, and it skips (with a log line) when the field is unset, the start is missing, or the duration is non-positive. Start is `ticket.mattermost_message_created_at`; a **naive** persisted value (SQLite drops the tz) is localized to the **runtime timezone** (not assumed UTC) before subtraction, so the duration matches wall-clock reality. (The other time-to-fix call sites live in incidents / alerts — see [../jira.md](../jira.md).)

## The standalone green notice

On closure, `format_incident_closed_notice` produces a **separate** reply (posted with `INCIDENT_DONE_COLOR`), not a footer inside the summary:

```
🟢 **Инцидент закрыт**
ПМ: [title](url)
```

The title always starts with `[INC] …`, so `[`/`]` are escaped to keep the leading bracket from breaking the Markdown link; without a URL it degrades to the bare title.

## LLM client invariants (`llm.py`)

`PostmortemLlmClient.resolve_incident_closeout` (END + title at close) and `generate_summary` (the thread-summary flow) share `_generate`. Note the **boundary**: `resolve_incident_closeout` passes **no** `on_progress`, so it is never streamed live; only `generate_summary` streams. The streaming *callback* contract lives here, the placeholder/throttle *mechanics* live in [../domains/thread-summary.md](../domains/thread-summary.md):

- **Cumulative, not delta:** `on_progress` receives the **full buffer of the current attempt** (`_collect_stream` hands over `"".join(chunks)`). A retry that restarts the stream replays from an empty buffer, so a wholesale `update_post` overwrites the stale partial cleanly.
- **The callback never raises** — the consumer wraps `update_post` in try/except so a transient Mattermost edit blip can't escape `_collect_stream`, hit `_retry`, and restart the whole LLM generation (invariant enforced in thread-summary).
- An **empty** stream raises a **non-retryable** `ApiError`.
- **Buffered-JSON fallback:** if a proxy ignores `stream` and answers without `text/event-stream`, the body is parsed as a normal chat completion; `on_progress` never fires (the placeholder stays static). `LLM_STREAM=false` takes the same non-streaming path.
- Throttling of live edits is governed by `LLM_STREAM_EDIT_INTERVAL_SECONDS` / `LLM_STREAM_EDIT_MIN_CHARS` (applied by the callback, see [../config.md](../config.md)).
