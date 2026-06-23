from __future__ import annotations

from dataclasses import dataclass

from mm_jira_bot.domain import MattermostPost, backend_datetime
from mm_jira_bot.formatting import truncate_for_summary

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


def build_postmortem_prompt(
    *,
    incident_thread_url: str,
    participants: list[str],
    postmortem_author: str,
    transcript: str,
    max_chars: int,
) -> str:
    trimmed_transcript = trim_transcript(transcript, max_chars=max_chars)
    participant_text = ", ".join(participants) if participants else "не указано"
    return f"""Создай инцидентный отчет по треду Mattermost/Band.

Обязательные правила:
- Верни отчет строго по шаблону ниже.
- Первая строка должна быть: [INC] DD.MM.YYYY - Короткое название.
- Короткое название в первой строке: до 10 слов и до 80 символов; вся первая строка до 120 символов.
- Дата в заголовке должна соответствовать дате инцидента по московскому времени.
- "Участники инцидента" бери из списка участников ниже; не добавляй людей, которых нет в треде.
- Автор постмортема: {postmortem_author}. Укажи его в отчете, если это уместно, но не добавляй в участники без оснований.
- Хронологию строй по сообщениям треда, времена указывай в московском времени HH:MM.
- "Извлеченные уроки" и "Action Items" выводи только из того, что явно обсуждалось в треде.
- В "Action Items" добавляй только договоренности, задачи или TODO, которые реально звучали в треде; если их нет, напиши "- не указано".
- Не выдумывай подробности. Если причина, влияние или решение не ясны, так и напиши.
- Не пиши, что ты не можешь создать Jira-задачу или отправить сообщение: это сделает бот.

Тред инцидента: {incident_thread_url}
Участники инцидента: {participant_text}
Автор постмортема: {postmortem_author}

Шаблон:
[INC] DD.MM.YYYY - Короткое название
Участники инцидента: фамилия имя через запятую
Автор постмортема: фамилия имя
##Сводка
Описание того, что случилось и почему.
##Решение
Как решили инцидент: что фиксили, что откатывали, какие действия помогли.
##Извлеченные уроки
###Что было сделано хорошо / В чем повезло
 - Быстро подключились к звонку
 - Быстро нашли проблему и сделали фикс
 - Влияние было ограничено, так как было 3 часа ночи
 - За день до этого сделали индексы в БД, которые помогли в этом инциденте убрать влияние гораздо быстрее
###Что пошло не так / В чем не повезло
 - Не было алертов, узнали спустя час от клиентов
##Action Items
 - Сформулируй только те action items, которые обсуждались в треде. Если action items не обсуждались, напиши "- не указано".
##Хронология
12:04 - Начали катить релиз с фичей X
12:06 - Начало влияния. Пришел алерт о пятисотках на ручке N
12:10 - Начало инцидента. Поняли, что проблема затрагивает клиентов, отписали в канал инцидентов, создали мит
12:12 - petuhov.sergey15 заметил, что <> не <>, и предложил сделать роллбэк
12:14 - Решение. aminov.pavel3 запустил роллбэк сервиса Y в k8s
12:18 - Устранение влияния. По метрикам пятисоток видим снижение до стабильных обычных значений
12:20 - aminov.pavel3 проверил остальные метрики, ожидаем 15 минут и если ок - инцидент завершаем
12:30 - Завершение инцидента. Влияние снято, проблем нет

Тред:
{trimmed_transcript}
"""


def build_postmortem_comment(
    *,
    report: str,
    incident_thread_url: str,
    postmortem_author: str,
) -> str:
    return "\n".join(
        [
            "Постмортем сгенерирован по треду инцидента.",
            f"Тред инцидента: {incident_thread_url}",
            f"Автор постмортема: {postmortem_author}",
            "",
            report.strip(),
        ]
    )


def format_postmortem_jira_footer(
    *,
    jira_issue_key: str | None,
    jira_issue_url: str | None,
) -> str:
    """Trailing line that points the incident thread at the Jira postmortem."""
    if jira_issue_key and jira_issue_url:
        jira_text = f"[{jira_issue_key}]({jira_issue_url})"
    else:
        jira_text = jira_issue_key or "Jira issue"
    return f"Полный постмортем отправлен в Jira: {jira_text}"


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
