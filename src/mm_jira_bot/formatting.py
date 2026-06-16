from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol

from mm_jira_bot.domain import backend_datetime

# Leading decorative symbols (status emoji like 🔴, bullets, etc.) and
# whitespace that Grafana prepends to the alert title line.
_LEADING_SYMBOLS = re.compile(r"^[\W_]+", re.UNICODE)

# When an alert clears, Grafana re-posts the same title prefixed with a green
# check mark instead of the firing 🔴. Such "resolved" posts must not create a
# Jira issue. Both the literal emoji and the shortcode form are matched.
_RESOLVED_MARKERS = ("✅", ":white_check_mark:")


def is_resolved_alert(message: str) -> bool:
    """True if the alert's first non-empty line starts with a resolved marker."""
    for line in message.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped.startswith(_RESOLVED_MARKERS)
    return False


class TicketView(Protocol):
    mattermost_message_text: str
    mattermost_message_url: str
    jira_issue_key: str | None
    jira_issue_url: str | None


def truncate_for_summary(text: str, *, limit: int = 120) -> str:
    normalized = " ".join(text.split())
    normalized = _LEADING_SYMBOLS.sub("", normalized)
    if not normalized:
        return "Band alert"
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


def format_thread_validity_changed(
    *,
    validity_label: str,
    jira_issue_key: str | None,
    jira_issue_url: str | None,
) -> str:
    return (
        f"Поле «Валидность» обновлено: {validity_label}. "
        f"Задача Jira: {_jira_link(jira_issue_key, jira_issue_url)}."
    )


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
            f"- Исходный алерт: [сообщение в Band]({ticket.mattermost_message_url})",
            f"- Задача Jira: {jira_part}",
            f"- Подтвердил: `{confirmed_by}`",
            f"- Время подтверждения: `{confirmed_at.isoformat()}`",
        ]
    )
