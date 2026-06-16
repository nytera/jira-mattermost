from __future__ import annotations

from datetime import datetime
from typing import Protocol

from mm_jira_bot.domain import backend_datetime


class TicketView(Protocol):
    mattermost_message_text: str
    mattermost_message_url: str
    jira_issue_key: str | None
    jira_issue_url: str | None


def truncate_for_summary(text: str, *, limit: int = 120) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        return "Mattermost alert"
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "..."


def _jira_link(jira_issue_key: str | None, jira_issue_url: str | None) -> str:
    if jira_issue_key and jira_issue_url:
        return f"[{jira_issue_key}]({jira_issue_url})"
    return jira_issue_key or "Jira issue"


def format_thread_issue_created(
    *, jira_issue_key: str, jira_issue_url: str | None
) -> str:
    return f"Создана задача Jira: {_jira_link(jira_issue_key, jira_issue_url)}"


def format_thread_status_changed(
    *,
    jira_issue_key: str | None,
    jira_issue_url: str | None,
    incident_message_url: str | None,
    status_transitioned: bool,
) -> str:
    lines = [
        "✅ Инцидент заведён и подтверждён как валидный. "
        f"Задача Jira: {_jira_link(jira_issue_key, jira_issue_url)}.",
        "Поле «Валидность» = Валидный."
        + (" Статус задачи обновлён." if status_transitioned else ""),
    ]
    if incident_message_url:
        lines.append(f"Сообщение в канале инцидентов: {incident_message_url}")
    return "\n".join(lines)


def format_incident_message(
    ticket: TicketView,
    *,
    confirmed_by: str,
    confirmed_at: datetime,
) -> str:
    confirmed_at = backend_datetime(confirmed_at)
    jira_part = (
        f"[{ticket.jira_issue_key}]({ticket.jira_issue_url})"
        if ticket.jira_issue_key and ticket.jira_issue_url
        else "Jira issue пока недоступна"
    )
    return "\n".join(
        [
            "### Подтверждённый инцидент",
            "",
            ticket.mattermost_message_text,
            "",
            f"- Исходный алерт: [сообщение в Mattermost]({ticket.mattermost_message_url})",
            f"- Задача Jira: {jira_part}",
            f"- Подтвердил: `{confirmed_by}`",
            f"- Время подтверждения: `{confirmed_at.isoformat()}`",
        ]
    )
