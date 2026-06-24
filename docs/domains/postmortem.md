# Postmortem (PostmortemMixin)

`PostmortemMixin` (`src/mm_jira_bot/service/_postmortem.py`) generates the incident postmortem when an incident is closed: it runs the LLM report into the Jira issue, plumbs the Jira fields (validity, END time, time-to-fix), then posts the fact-based summary back into the thread and a standalone green "incident closed" notice. Pure helpers live in `src/mm_jira_bot/postmortem.py`; the OpenAI-compatible client lives in `src/mm_jira_bot/llm.py`. For method signatures see [../reference/service-map.md](../reference/service-map.md).

> `PostmortemMixin` is a domain mixin of `IncidentBotService` — see [architecture.md](../architecture.md) for how the service is assembled.

## Scope and boundaries

- **Owned here:** `generate_incident_postmortem` (orchestration), `_ensure_postmortem_jira_issue`, `_apply_postmortem_validity`, `_set_time_to_fix`, `_resolve_incident_end_time` / `_parse_incident_end_time` (END / time-to-fix recovery time — see below), `_postmortem_thread_context`.
- **Delegated out** (called via `TYPE_CHECKING` stubs): the **thread-summary FLOW** (`_post_summary_placeholder`, `_set_summary_status`, `_generate_and_finalize_summary`, `_create_thread_summary_reply`, the live-stream throttle callback) lives in [../domains/thread-summary.md](../domains/thread-summary.md) — this domain only *calls* it. The reaction/dispatch that decides to close an incident and the idempotency *guard* live in [../domains/incidents.md](../domains/incidents.md). Jira field/option semantics: [../jira.md](../jira.md). Env vars: [../config.md](../config.md). Runtime prompt overrides: [../domains/debug.md](../domains/debug.md).

## One template, two renderings (the central invariant)

The Jira postmortem **and** the in-thread summary share **one** user-prompt template — `DEFAULT_INCIDENT_REPORT_PROMPT` in `postmortem.py` (aliased `DEFAULT_POSTMORTEM_PROMPT` / `DEFAULT_SUMMARY_PROMPT`), rendered by the single `build_incident_report_prompt`. They differ only in which override is passed in and how the LLM output is rendered afterwards. The template carries the rich structure (Мета, Сводка + Описание влияния, Решение, Извлечённые уроки, Action Items as discussion suggestions, Хронология, Риски рецидива, Открытые вопросы) and a **mandatory first line** `[INC] DD.MM.YYYY - …`.

Invariants:

- **Placeholders** `{thread_url}` / `{participants}` / `{postmortem_author}` / `{transcript}` are substituted by an **ordered `str.replace`** with `{transcript}` **last**, so arbitrary thread text containing brace-looking tokens is never re-scanned for placeholders. `{incident_thread_url}` is a legacy alias for `{thread_url}`, kept so pre-existing override files keep working.
- The `[INC] DD.MM.YYYY - …` first line is read by `extract_postmortem_summary` to build the **Jira issue title** (falling back to `extract_alert_title` of the root post when absent); `_limit_postmortem_summary` clamps it to 120 chars / 10 words.
- **Jira path:** the LLM always emits Markdown; `markdown_to_jira_wiki` converts it to wiki markup (the v2 comment endpoint renders wiki, not Markdown) **and** turns `@username → [~username]` (clickable mentions, when MM and Jira usernames match).
- **Mattermost path:** `summary.neutralize_mentions` strips the leading `@` so the thread summary never pings participants.
- **Effective template is resolved at call time** by `_resolve_prompt_template` (coordinator): **DB override (debug panel) → env (`LLM_POSTMORTEM_PROMPT` / `LLM_SUMMARY_PROMPT`, plus `*_FILE`) → built-in default**.
- The two `SYSTEM_PROMPT`s (role / quality guidelines) stay in `llm.py` **code** — they are not env-overridable.

## `generate_incident_postmortem` — the closure flow

Runs only on the final-status completion path. Steps, all behind one placeholder reply that shows stepwise status:

1. `_postmortem_thread_context` collects thread posts (+ the root, reinserted if missing), resolves author display names, and returns `(thread_messages, participants, postmortem_author)`. The bot user is excluded from `participants`; `format_thread_transcript` renders the transcript.
2. Post the "Генерация саммари…" placeholder up front (status `⏳ Шаг 1/3: генерирую постмортем…`), build the prompt, call `llm.generate_postmortem(prompt)`, and `extract_postmortem_summary` for the title.
3. `⏳ Шаг 2/3: отправляю постмортем в Jira…` → `_ensure_postmortem_jira_issue`, then `set_description` and `add_comment` (the wiki-converted `build_postmortem_comment`). Mark the idempotency marker (below).
4. `⏳ Шаг 3/3: генерирую саммари…` → delegate the summary to the thread-summary flow (its own LLM call, **not** derived from the Jira report).
5. Post the standalone green notice (below).

**Error handling:** any `ApiError` in steps 1–3 records `mark_postmortem_failed`, edits the placeholder into a retry-able failure notice (so it never stays stuck on "Генерация саммари…"), and returns `ConfirmationStatus.ERROR`. The summary + green notice (steps 4–5) are reached **only after** the Jira postmortem succeeded, so the Jira link is always present there.

**Idempotency:** on success this domain *sets* `mark_postmortem_comment_added`. The re-entry **guard** that consumes the `postmortem_comment_added` marker lives upstream — see [../domains/incidents.md](../domains/incidents.md).

## Jira field plumbing

### `_ensure_postmortem_jira_issue`

Creates the issue if missing (then `attach_jira_issue`, announce to ops, set validity / END / time-to-fix, `mark_confirmed`). For an **existing** issue it short-circuits but still honours one subtle rule, captured in code: **validity and confirmation are independent axes** — a validity emoji (`validity_label`) must update the field even on an already-confirmed incident, and END / time-to-fix / `mark_confirmed` only run when the incident is not yet confirmed.

### `_apply_postmortem_validity`

The `validity_label` (a Ложный / Ожидаемый choice made *now* in the incident thread) **wins** over any earlier value (`set_validity` + persist). With no choice and none previously recorded, default to **Валидный** (`set_valid_incident(True)`); an earlier explicit Ложный / Ожидаемый is left untouched (it was already pushed when picked).

### `_resolve_incident_end_time`

Called once from `handle_incident_checkmark` (incidents) before any Jira write. A
dedicated, small LLM call (`extract_incident_end_time`) reads the thread chronology and
returns the recovery time; it is accepted only when it parses and lands within
`[start, now + margin]` (`set_end_time` has no range guard of its own). On no-LLM /
`ApiError` / `UNKNOWN` / unparseable / out-of-range it falls back to the reaction
timestamp. The single resolved value then flows into both the END field and the
postmortem.

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

`PostmortemLlmClient.generate_postmortem` and `generate_summary` share `_generate`. Note the **boundary**: `generate_postmortem` passes **no** `on_progress`, so the postmortem itself is never streamed live; only `generate_summary` (the thread-summary flow) streams. The streaming *callback* contract lives here, the placeholder/throttle *mechanics* live in [../domains/thread-summary.md](../domains/thread-summary.md):

- **Cumulative, not delta:** `on_progress` receives the **full buffer of the current attempt** (`_collect_stream` hands over `"".join(chunks)`). A retry that restarts the stream replays from an empty buffer, so a wholesale `update_post` overwrites the stale partial cleanly.
- **The callback never raises** — the consumer wraps `update_post` in try/except so a transient Mattermost edit blip can't escape `_collect_stream`, hit `_retry`, and restart the whole LLM generation (invariant enforced in thread-summary).
- An **empty** stream raises a **non-retryable** `ApiError`.
- **Buffered-JSON fallback:** if a proxy ignores `stream` and answers without `text/event-stream`, the body is parsed as a normal chat completion; `on_progress` never fires (the placeholder stays static). `LLM_STREAM=false` takes the same non-streaming path.
- Throttling of live edits is governed by `LLM_STREAM_EDIT_INTERVAL_SECONDS` / `LLM_STREAM_EDIT_MIN_CHARS` (applied by the callback, see [../config.md](../config.md)).
