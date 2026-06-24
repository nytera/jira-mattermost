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


def alert_signature(message: str) -> str:
    """Stable identity for grouping repeated firings of the same alert.

    Keyed on the extracted title rather than the Grafana rule UID: the alert
    link is not always present (some alerts arrive as plain text, and a resolved
    ✅ repost may drop the link), so a UID-first key would make a firing and its
    resolve diverge and the episode would never close. ``extract_alert_title``
    recovers the same title from both the link text and the plain line, and
    strips the leading 🔴/✅ markers, so it stays symmetric across firing/resolve
    and link presence. Trade-off: two distinct rules sharing a title merge into
    one episode (a single stray "expected" mark, human-recoverable).
    """
    return f"title:{extract_alert_title(message)}"


def _jira_link(jira_issue_key: str | None, jira_issue_url: str | None) -> str:
    if jira_issue_key and jira_issue_url:
        return f"[{jira_issue_key}]({jira_issue_url})"
    return jira_issue_key or "Jira issue"


def format_thread_issue_created(*, jira_issue_key: str, jira_issue_url: str | None) -> str:
    return f"Создана задача Jira: {_jira_link(jira_issue_key, jira_issue_url)}"


def format_thread_linked_to_root(
    *, root_issue_key: str | None, root_issue_url: str | None
) -> str:
    return f"Прилинковано к корневой задаче: {_jira_link(root_issue_key, root_issue_url)}"


def format_thread_status_changed(*, incident_message_url: str | None) -> str:
    if incident_message_url:
        return f"🚨 **Инцидент заведен**\n- [Ссылка на инцидент]({incident_message_url})"
    return "🚨 **Инцидент заведен**"


def format_thread_validity_changed(*, validity_label: str) -> str:
    # The Jira link is already in the "Создана задача" reply at the top of this
    # same alert thread, so the follow-up notice doesn't repeat it.
    return f"Валидность обновлена: `{validity_label}`"


# Incident-message title: red while open, green once closed. The completion
# update keys off the exact open-title line, so keep them in sync.
INCIDENT_TITLE_OPEN = "##### 🔴 Инцидент открыт"
INCIDENT_TITLE_DONE = "##### 🟢 Инцидент закрыт"


def mark_incident_message_completed(message: str) -> str:
    """Swap the open title for the closed one in an incident message."""
    return message.replace(INCIDENT_TITLE_OPEN, INCIDENT_TITLE_DONE, 1)


_MENTION = re.compile(r"@[^\s()]+")


def mention_from_display(display: str) -> str:
    """Extract just the `@username` tag from a "Name (@username)" display string."""
    match = _MENTION.search(display)
    return match.group(0) if match else display


_DUTY_HELP_HEADER = "**ℹ️ Памятка дежурному SRE**"


def format_alert_duty_help(
    *,
    incident_emoji: str,
    false_emoji: str,
    expected_emoji: str,
    summary_emoji: str,
) -> str:
    """Reaction cheat-sheet posted in a firing-alert thread.

    Each reaction lists both the ``:shortcode:`` (which renders to the emoji
    when it exists on the instance) and a plain-language label, so the help
    stays readable even if a custom emoji like ``:incident:`` is missing.
    """
    return "\n".join(
        [
            _DUTY_HELP_HEADER,
            "Реакции на этот алерт:",
            f"- :{incident_emoji}: — завести инцидент",
            f"- :{false_emoji}: — пометить ложным",
            f"- :{expected_emoji}: — пометить ожидаемым",
            f"- :{summary_emoji}: — саммари треда",
        ]
    )


def format_incident_duty_help(
    *,
    false_emoji: str,
    expected_emoji: str,
    summary_emoji: str,
) -> str:
    """Reaction cheat-sheet posted in an incident thread.

    Unlike the alert thread, validity reactions here also *close* the incident
    and trigger a postmortem — spell that out so on-call doesn't mistake them
    for the alert channel's label-only behavior.
    """
    return "\n".join(
        [
            _DUTY_HELP_HEADER,
            "Реакции на корневое сообщение инцидента:",
            "- ✅ галочка — валидный, завершить инцидент + постмортем",
            f"- :{false_emoji}: — ложный, завершить инцидент + постмортем",
            f"- :{expected_emoji}: — ожидаемый, завершить инцидент + постмортем",
            f"- :{summary_emoji}: — саммари треда",
        ]
    )


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
    lines = [INCIDENT_TITLE_OPEN, ""]
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
