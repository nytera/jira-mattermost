from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol

from mm_jira_bot.domain import backend_datetime

# Leading decorative symbols (status emoji like 🔴, bullets, etc.) and
# whitespace that Grafana prepends to the alert title line.
_LEADING_SYMBOLS = re.compile(r"^[\W_]+", re.UNICODE)
_LEADING_EMOJI_SHORTCODES = re.compile(r"^(?::[a-z0-9_+-]+:\s*)+", re.IGNORECASE)
_GRAFANA_ALERT_URL = re.compile(
    r"(?:https?://)?grafana\.wb\.ru/alerting/grafana/[^)\s>]+",
    re.IGNORECASE,
)
_GRAFANA_ALERT_ANGLE_LINK = re.compile(
    r"<(?:https?://)?grafana\.wb\.ru/alerting/grafana/[^|>\s]+\|([^>\n]+)>",
    re.IGNORECASE,
)

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
    normalized = _LEADING_EMOJI_SHORTCODES.sub("", normalized)
    normalized = _LEADING_SYMBOLS.sub("", normalized)
    if not normalized:
        return "Band alert"
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "..."


def _find_markdown_link_open(line: str, closing_bracket_index: int) -> int | None:
    depth = 1
    for index in range(closing_bracket_index - 1, -1, -1):
        char = line[index]
        if char == "]":
            depth += 1
        elif char == "[":
            depth -= 1
            if depth == 0:
                return index
    return None


def _extract_grafana_markdown_link_title(line: str, *, limit: int) -> str | None:
    for match in _GRAFANA_ALERT_URL.finditer(line):
        paren_index = line.rfind("(", 0, match.start())
        if paren_index <= 0 or line[paren_index - 1] != "]":
            continue
        bracket_open_index = _find_markdown_link_open(line, paren_index - 1)
        if bracket_open_index is None:
            continue
        title = truncate_for_summary(line[bracket_open_index + 1 : paren_index - 1], limit=limit)
        if title != "Band alert":
            return title
    return None


def _extract_grafana_angle_link_title(line: str, *, limit: int) -> str | None:
    match = _GRAFANA_ALERT_ANGLE_LINK.search(line)
    if not match:
        return None
    title = truncate_for_summary(match.group(1), limit=limit)
    if title != "Band alert":
        return title
    return None


def _extract_grafana_alert_link_title(line: str, *, limit: int) -> str | None:
    return _extract_grafana_markdown_link_title(
        line, limit=limit
    ) or _extract_grafana_angle_link_title(line, limit=limit)


def extract_alert_title(message: str, *, limit: int = 120) -> str:
    for line in message.splitlines():
        linked_title = _extract_grafana_alert_link_title(line, limit=limit)
        if linked_title is not None:
            return linked_title
        title = truncate_for_summary(line, limit=limit)
        if title != "Band alert":
            return title
    return "Band alert"


def _jira_link(jira_issue_key: str | None, jira_issue_url: str | None) -> str:
    if jira_issue_key and jira_issue_url:
        return f"[{jira_issue_key}]({jira_issue_url})"
    return jira_issue_key or "Jira issue"


def format_thread_issue_created(*, jira_issue_key: str, jira_issue_url: str | None) -> str:
    return f"Создана задача Jira: {_jira_link(jira_issue_key, jira_issue_url)}"


def format_thread_status_changed(*, incident_message_url: str | None) -> str:
    if incident_message_url:
        return f"✅ **Инцидент заведён.** Ссылка на сообщение: {incident_message_url}"
    return "✅ **Инцидент заведён.**"


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


# Incident-message title: the alert name prefixed with a status circle — red
# while open, green once ended. The completion update swaps just the prefix, so
# the alert name is preserved.
INCIDENT_TITLE_OPEN_PREFIX = "##### 🔴 "
INCIDENT_TITLE_DONE_PREFIX = "##### 🟢 "


def mark_incident_message_completed(message: str) -> str:
    """Swap the open status prefix for the completed one (keeps the alert name)."""
    return message.replace(INCIDENT_TITLE_OPEN_PREFIX, INCIDENT_TITLE_DONE_PREFIX, 1)


_MENTION = re.compile(r"@[^\s()]+")


def mention_from_display(display: str) -> str:
    """Extract just the `@username` tag from a "Name (@username)" display string."""
    match = _MENTION.search(display)
    return match.group(0) if match else display


def format_incident_message(
    ticket: TicketView,
    *,
    confirmed_by: str,
    confirmed_at: datetime,
    include_alert_text: bool = True,
) -> str:
    confirmed_at = backend_datetime(confirmed_at)
    jira_part = (
        f"[{ticket.jira_issue_key}]({ticket.jira_issue_url})"
        if ticket.jira_issue_key and ticket.jira_issue_url
        else "Jira issue пока недоступна"
    )
    lines = [INCIDENT_TITLE_OPEN_PREFIX + extract_alert_title(ticket.mattermost_message_text), ""]
    if include_alert_text and ticket.mattermost_message_text.strip():
        lines.extend([ticket.mattermost_message_text, ""])
    lines.extend(
        [
            f"- Задача Jira: {jira_part}",
            # Alert lives in the alerts channel; always link it.
            f"- Исходный алерт: [сообщение в Band]({ticket.mattermost_message_url})",
            # Just the @mention (no name, no backticks) so it renders as a live ping.
            f"- Подтвердил: {confirmed_by}",
            f"- Время подтверждения: {confirmed_at.strftime('%d.%m.%Y %H:%M')}",
        ]
    )
    return "\n".join(lines)
