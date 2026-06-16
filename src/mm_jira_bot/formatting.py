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


def format_incident_message(
    ticket: TicketView,
    *,
    confirmed_by_user_id: str,
    confirmed_at: datetime,
) -> str:
    confirmed_at = backend_datetime(confirmed_at)
    jira_part = (
        f"[{ticket.jira_issue_key}]({ticket.jira_issue_url})"
        if ticket.jira_issue_key and ticket.jira_issue_url
        else "Jira issue is not available yet"
    )
    return "\n".join(
        [
            "### Confirmed incident",
            "",
            ticket.mattermost_message_text,
            "",
            f"- Original alert: [Mattermost post]({ticket.mattermost_message_url})",
            f"- Jira issue: {jira_part}",
            f"- Confirmed by: `{confirmed_by_user_id}`",
            f"- Confirmed at: `{confirmed_at.isoformat()}`",
        ]
    )
