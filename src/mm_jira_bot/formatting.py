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

# When an alert clears, Grafana re-posts the same title with a green check mark
# instead of the firing 🔴. Such "resolved" posts must not create a Jira issue.
# The marker may sit anywhere on the title line (Grafana wraps it in markdown,
# e.g. ``**✅ Title**``), so we look for its presence, not a strict prefix. Both
# the literal emoji and the shortcode form are matched.
_RESOLVED_MARKERS = ("✅", ":white_check_mark:")


def is_resolved_alert(message: str) -> bool:
    """True if the alert's first non-empty line contains a resolved marker.

    Grafana sometimes wraps the title in markdown (``**✅ …**``) or pads the
    marker, so the check is "marker anywhere on the title line" rather than a
    strict prefix. Trade-off: a firing whose title literally contains ✅ would be
    read as resolved — accepted, since Grafana firings are prefixed with 🔴.
    """
    for line in message.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return any(marker in stripped for marker in _RESOLVED_MARKERS)
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


def format_ops_issue_created(
    *,
    jira_issue_key: str,
    jira_issue_url: str | None,
    source_title: str | None,
    source_message_url: str | None,
    channel_name: str | None,
    incident_message_url: str | None = None,
) -> str:
    """Ops-channel feed line for a newly created Jira issue with a link back to
    the source Mattermost thread/message."""
    lines = [f":page_facing_up: **Создана задача** {_jira_link(jira_issue_key, jira_issue_url)}"]
    if source_title:
        lines.append(source_title)
    source_bits: list[str] = []
    if source_message_url:
        source_bits.append(f"[сообщение-источник]({source_message_url})")
    if channel_name:
        source_bits.append(f"канал «{channel_name}»")
    if source_bits:
        lines.append(" · ".join(source_bits))
    if incident_message_url and incident_message_url != source_message_url:
        lines.append(f"[Тред инцидента]({incident_message_url})")
    return "\n".join(lines)


def format_thread_linked_to_root(
    *,
    root_issue_key: str | None,
    root_issue_url: str | None,
    root_message_url: str | None,
) -> str:
    if root_issue_url:
        root_issue_link = f"[корневой задаче]({root_issue_url})"
    else:
        root_issue_link = _jira_link(root_issue_key, root_issue_url)

    lines = [
        ":arrows_counterclockwise: **Повторный алерт**",
        f"Тикет прилинкован к {root_issue_link} (корневая задача первого алерта).",
    ]
    if root_message_url:
        lines.append(f"[Корневой алерт]({root_message_url})")
    return "\n".join(lines)


def format_thread_status_changed(*, incident_message_url: str | None) -> str:
    if incident_message_url:
        return f"🚨 **Инцидент заведен**\n- [Ссылка на инцидент]({incident_message_url})"
    return "🚨 **Инцидент заведен**"


def format_thread_validity_changed(*, validity_label: str) -> str:
    # The Jira link is already in the "Создана задача" reply at the top of this
    # same alert thread, so the follow-up notice doesn't repeat it.
    return f"Валидность обновлена: `{validity_label}`"


# Incident-message title box: just the alert name, kept as a heading so the
# incident is identifiable at a glance. Status is carried textually by the
# detail box below (INCIDENT_STATUS_*), not by a circle on the title.
INCIDENT_TITLE_PREFIX = "#####"

# Status label on the first line of the incident detail box; the border color
# also flips open→closed. ``mark_incident_message_completed`` keys off the exact
# open label, so keep the two in sync.
INCIDENT_STATUS_OPEN = "**Новый инцидент**"
INCIDENT_STATUS_DONE = "**Закрытый инцидент**"


def mark_incident_message_completed(message: str) -> str:
    """Swap the open status label for the closed one in the incident detail box.

    Only the ``Новый инцидент`` → ``Закрытый инцидент`` label changes; the Jira
    link on the same line and the rest of the box are preserved.
    """
    return message.replace(INCIDENT_STATUS_OPEN, INCIDENT_STATUS_DONE, 1)


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
    (set END time + validity) — spell that out so on-call doesn't mistake them
    for the alert channel's label-only behavior. The narrative summary is not
    produced on close; it is the summary reaction's job (button-only).
    """
    return "\n".join(
        [
            _DUTY_HELP_HEADER,
            "Реакции на корневое сообщение инцидента:",
            "- ✅ галочка — валидный, завершить инцидент",
            f"- :{false_emoji}: — ложный, завершить инцидент",
            f"- :{expected_emoji}: — ожидаемый, завершить инцидент",
            f"- :{summary_emoji}: — саммари треда",
        ]
    )


def format_incident_title(ticket: TicketView) -> str:
    """Top box of the incident post: just the alert name, as a heading."""
    alert_title = extract_alert_title(ticket.mattermost_message_text)
    return f"{INCIDENT_TITLE_PREFIX} {alert_title}"


def format_incident_message(
    ticket: TicketView,
    *,
    author: str,
    alert_at: datetime | None,
    include_alert_text: bool = True,
) -> str:
    """Detail box of the incident post (the box below the title box).

    The first line is the status label carrying the Jira link — it flips
    ``Новый инцидент`` → ``Закрытый инцидент`` on close. When the alert has no
    forwarded attachment block, its full body is embedded here so it isn't lost.
    """
    jira_part = (
        f"[{ticket.jira_issue_key}]({ticket.jira_issue_url})"
        if ticket.jira_issue_key and ticket.jira_issue_url
        else "Jira issue пока недоступна"
    )
    lines = [f"{INCIDENT_STATUS_OPEN} — {jira_part}", ""]
    if include_alert_text and ticket.mattermost_message_text.strip():
        lines.extend([ticket.mattermost_message_text, ""])
    when = backend_datetime(alert_at).strftime("%d.%m.%Y %H:%M") if alert_at else "—"
    lines.extend(
        [
            # Alert lives in the alerts channel; always link it.
            f"- Исходный алерт: [сообщение в Band]({ticket.mattermost_message_url})",
            # Just the @mention (no name, no backticks) so it renders as a live ping.
            f"- Автор: {author}",
            f"- Время алерта: {when}",
        ]
    )
    return "\n".join(lines)
