from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


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
    if confirmed_at.tzinfo is None:
        confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)
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
