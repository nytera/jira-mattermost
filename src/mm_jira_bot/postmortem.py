from __future__ import annotations

import re
from dataclasses import dataclass

from mm_jira_bot.domain import MattermostPost, backend_datetime
from mm_jira_bot.formatting import truncate_for_summary

# Markdown→Jira-wiki conversion (single deterministic pass over known Markdown).
# Order matters and no rule may consume a "*" emitted by an earlier rule:
# bold runs first; the bullet rule only adds "* " at line start.
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_HEADING = re.compile(r"^(#{1,6})[ \t]*", re.MULTILINE)
_MD_BULLET = re.compile(r"^[ \t]*[-+] ", re.MULTILINE)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
# ``@username`` (the form the LLM emits from the transcript) → Jira mention
# ``[~username]``. The look-behind skips emails (``user@host``); the charset
# matches Mattermost/Jira account names and tolerates internal dots/hyphens
# (``aminov.pavel3``) without swallowing a trailing sentence period.
_MD_MENTION = re.compile(r"(?<![\w.@])@([A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)*)")


def markdown_to_jira_wiki(text: str) -> str:
    """Convert the Markdown the LLM emits into Jira wiki markup for v2 comments.

    Idempotent on already-wiki input. Markdown italics are intentionally left
    alone: ``*x*`` collides with the wiki bold this produces, and rendering an
    italic as bold is an acceptable cosmetic loss in a postmortem.
    """
    text = _MD_BOLD.sub(r"*\1*", text)
    text = _MD_HEADING.sub(lambda m: f"h{len(m.group(1))}. ", text)
    text = _MD_BULLET.sub("* ", text)
    text = _MD_LINK.sub(r"[\1|\2]", text)
    text = _MD_MENTION.sub(r"[~\1]", text)
    return text


POSTMORTEM_SUMMARY_MAX_CHARS = 120
POSTMORTEM_TITLE_MAX_CHARS = 80
POSTMORTEM_TITLE_MAX_WORDS = 10


@dataclass(frozen=True)
class ThreadMessage:
    post: MattermostPost
    author_display: str


def _attachment_field_text(field: dict) -> str | None:
    title = str(field.get("title") or "").strip()
    value = str(field.get("value") or "").strip()
    if title and value:
        return f"{title}: {value}"
    return title or value or None


def _attachment_text(attachment: dict) -> str:
    lines: list[str] = []
    for key in ("pretext", "title", "text", "footer"):
        value = str(attachment.get(key) or "").strip()
        if value:
            lines.append(value)
    fields = attachment.get("fields")
    if isinstance(fields, list):
        lines.extend(
            field_text
            for field in fields
            if isinstance(field, dict)
            for field_text in [_attachment_field_text(field)]
            if field_text
        )
    image_url = str(attachment.get("image_url") or "").strip()
    if image_url:
        lines.append(f"image_url: {image_url}")
    title_link = str(attachment.get("title_link") or "").strip()
    if title_link:
        lines.append(f"title_link: {title_link}")
    return "\n".join(lines)


def _post_text(post: MattermostPost) -> str:
    lines: list[str] = []
    if post.message.strip():
        lines.append(post.message.strip())
    props = post.props
    attachments = props.get("attachments") if isinstance(props, dict) else None
    if isinstance(attachments, list):
        attachment_lines = [
            text
            for attachment in attachments
            if isinstance(attachment, dict)
            for text in [_attachment_text(attachment)]
            if text
        ]
        if attachment_lines:
            lines.append("Вложения:\n" + "\n\n".join(attachment_lines))
    return "\n\n".join(lines) or "(пустое сообщение)"


def format_thread_transcript(messages: list[ThreadMessage]) -> str:
    lines: list[str] = []
    for index, message in enumerate(messages, start=1):
        post = message.post
        created_at = (
            backend_datetime(post.created_at_datetime).strftime("%d.%m.%Y %H:%M:%S")
            if post.create_at > 0 and post.created_at_datetime
            else "время не указано"
        )
        marker = "оригинальное сообщение" if not post.root_id else "ответ в треде"
        lines.append(
            "\n".join(
                [
                    f"[{index}] {created_at} MSK, {message.author_display}, {marker}",
                    f"post_id: {post.id}",
                    _post_text(post),
                ]
            )
        )
    return "\n\n---\n\n".join(lines)


def trim_transcript(transcript: str, *, max_chars: int) -> str:
    if max_chars <= 0 or len(transcript) <= max_chars:
        return transcript
    head_limit = max_chars // 2
    tail_limit = max_chars - head_limit
    return (
        transcript[:head_limit].rstrip()
        + "\n\n...[середина треда обрезана из-за лимита контекста]...\n\n"
        + transcript[-tail_limit:].lstrip()
    )


# Single source-of-truth user-prompt template for BOTH the Jira postmortem
# comment and the in-thread Mattermost summary — one structure, two renderings.
# Overridable per channel via ``LLM_POSTMORTEM_PROMPT`` / ``LLM_SUMMARY_PROMPT``
# (and their ``*_FILE`` / debug-panel overrides). Supported placeholders,
# substituted by ``build_incident_report_prompt``: ``{thread_url}``,
# ``{participants}``, ``{postmortem_author}``, ``{transcript}`` (the trimmed
# thread; always substituted last so thread text can safely contain
# brace-looking tokens).
#
# The LLM always emits Markdown. The Jira path converts it to wiki markup and
# turns ``@username`` into ``[~username]`` (``markdown_to_jira_wiki``); the
# Mattermost path strips the ``@`` so the summary never pings participants
# (``summary.neutralize_mentions``).
DEFAULT_INCIDENT_REPORT_PROMPT = """Составь инцидентный отчёт по треду Mattermost строго по фактам из треда и метаданных.

Правила:
- Опирайся только на факты из треда. Не выдумывай причины, метрики, имена, сервисы и времена. Если данных нет — пиши "не указано" или TBD.
- Первая строка обязана быть: [INC] DD.MM.YYYY - Короткое название (до 10 слов и до 80 символов; вся первая строка до 120 символов; дата — по московскому времени).
- Явно отделяй подтверждённые факты от гипотез. Для неподтверждённой причины помечай [Гипотеза] с уровнем уверенности (high/medium/low).
- Blameless: фокус на процессах, системах и решениях, не на вине людей.
- Все времена указывай по московскому времени в формате HH:MM.
- Участников бери только из списка ниже; не добавляй людей, которых нет в треде. В хронологии указывай участника как @username из транскрипта.
- Где возможно, количественно описывай влияние: длительность, доля затронутых запросов/клиентов, метрики до/после.
- Action Items — это предложения на обсуждение, а не финальные задачи; если из треда ничего конкретного не следует, напиши "- не указано".
- Не пиши, что не можешь создать Jira-задачу или отправить сообщение: это сделает бот.
- Не добавляй преамбулу, code fences и служебные пояснения. Верни только отчёт по структуре ниже.

Тред инцидента: {thread_url}
Участники: {participants}
Автор отчёта: {postmortem_author}

Структура отчёта:
[INC] DD.MM.YYYY - Короткое название
Участники инцидента: фамилия имя через запятую
Автор отчёта: фамилия имя

**Мета**
- Сервис
- Начало инцидента / начало влияния
- Восстановление / завершение
- Длительность
- Текущий статус
- Как обнаружено (сотрудник / алерт / клиент)

## Сводка
Что случилось и почему. Если root cause подтверждён — укажи; если нет — перечисли гипотезы с уровнем уверенности.

### Описание влияния
- Инфраструктурное: например, 3 млн запросов отдали ошибку 503
- Денежное: например, потеряли N рублей на бонусах клиентам
- Репутационное: например, упоминания в СМИ
Если в треде есть ссылки на графики метрик / BI / логи — приложи их здесь.

## Решение
Как решили инцидент: что фиксили, что откатывали, какие действия помогли.

## Извлечённые уроки
### Что было сделано хорошо / В чём повезло
- ...
### Что пошло не так / В чём не повезло
- ...

## Action Items (на обсуждение)
- Предложи возможные action items по итогам инцидента. Если из треда ничего конкретного не следует — напиши "- не указано".

## Хронология
- 12:04 — Начали катить релиз с фичей X
- 12:06 — **Начало влияния.** Пришёл алерт о пятисотках на ручке N
- 12:10 — **Начало инцидента.** Поняли, что затрагивает клиентов, отписали в канал инцидентов, создали мит
- 12:12 — @petuhov.sergey15 заметил проблему и предложил роллбэк
- 12:14 — **Решение.** @aminov.pavel3 запустил роллбэк сервиса Y в k8s
- 12:18 — **Устранение влияния.** По метрикам пятисоток видим снижение до нормы
- 12:30 — **Завершение инцидента.** Влияние снято, проблем не наблюдается

## Риски рецидива
- Что может повториться в ближайшее время и почему.

## Открытые вопросы / недостающие данные
- Какие факты не указаны или требуют подтверждения, чтобы закрыть анализ.

## Дополнительная информация
Всё значимое, что не вошло в блоки выше.

Тред:
{transcript}
"""

# Both channels default to the same template (the override paths differ only in
# which env var / DB key they read). Kept as named aliases so existing imports
# and the debug panel can address each channel's default explicitly.
DEFAULT_POSTMORTEM_PROMPT = DEFAULT_INCIDENT_REPORT_PROMPT
DEFAULT_SUMMARY_PROMPT = DEFAULT_INCIDENT_REPORT_PROMPT


def build_incident_report_prompt(
    *,
    thread_url: str,
    participants: list[str],
    postmortem_author: str,
    transcript: str,
    max_chars: int,
    template: str | None = None,
) -> str:
    """Render the incident-report prompt for either channel.

    The Jira postmortem and the Mattermost summary share this one builder; they
    differ only in the ``template`` override passed in and in how the LLM output
    is later rendered (wiki vs Markdown).
    """
    trimmed_transcript = trim_transcript(transcript, max_chars=max_chars)
    participant_text = ", ".join(participants) if participants else "не указано"
    body = template or DEFAULT_INCIDENT_REPORT_PROMPT
    # Metadata first, transcript last: arbitrary thread text never gets re-scanned
    # for placeholder tokens. ``{incident_thread_url}`` is the legacy postmortem
    # alias for ``{thread_url}`` — kept so pre-existing override files keep working.
    return (
        body.replace("{thread_url}", thread_url)
        .replace("{incident_thread_url}", thread_url)
        .replace("{participants}", participant_text)
        .replace("{postmortem_author}", postmortem_author)
        .replace("{transcript}", trimmed_transcript)
    )


def build_postmortem_comment(
    *,
    report: str,
    incident_thread_url: str,
    postmortem_author: str,
) -> str:
    body = "\n".join(
        [
            "Постмортем сгенерирован по треду инцидента.",
            f"Тред инцидента: {incident_thread_url}",
            f"Автор постмортема: {postmortem_author}",
            "",
            report.strip(),
        ]
    )
    # The Jira v2 comment endpoint renders wiki markup, not Markdown. Convert the
    # whole assembled body (header + report) so headings/bullets/links render.
    return markdown_to_jira_wiki(body)


def format_incident_closed_notice(
    *,
    jira_issue_title: str,
    jira_issue_url: str | None,
) -> str:
    """Standalone green-boxed reply posted when an incident reaches a final status.

    Line one announces closure; line two links the Jira postmortem under its task
    title. Without a URL it degrades to the bare title.
    """
    title = jira_issue_title.strip() or "постмортем"
    if jira_issue_url:
        # The title always starts with "[INC] …"; escape brackets so the leading
        # "[" does not nest inside the markdown link text and break rendering.
        safe_title = title.replace("[", "\\[").replace("]", "\\]")
        pm_text = f"[{safe_title}]({jira_issue_url})"
    else:
        pm_text = title
    return "\n".join(["🟢 **Инцидент закрыт**", f"ПМ: {pm_text}"])


def extract_postmortem_summary(report: str, *, fallback: str) -> str:
    for line in report.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[INC]"):
            return _limit_postmortem_summary(stripped)
        break
    title = truncate_for_summary(fallback, limit=160)
    return _limit_postmortem_summary(f"[INC] {title}")


def _ellipsis(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _limit_postmortem_title(title: str) -> str:
    normalized = " ".join(title.split())
    words = normalized.split()
    if len(words) > POSTMORTEM_TITLE_MAX_WORDS:
        normalized = " ".join(words[:POSTMORTEM_TITLE_MAX_WORDS])
    normalized = _ellipsis(normalized, limit=POSTMORTEM_TITLE_MAX_CHARS)
    return normalized or "Инцидент"


def _limit_postmortem_summary(summary: str) -> str:
    normalized = " ".join(summary.split())
    if " - " in normalized:
        prefix, title = normalized.split(" - ", 1)
        normalized = f"{prefix} - {_limit_postmortem_title(title)}"
    return _ellipsis(normalized, limit=POSTMORTEM_SUMMARY_MAX_CHARS)
