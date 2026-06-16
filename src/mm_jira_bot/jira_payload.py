from __future__ import annotations

from datetime import datetime
from typing import Any

from mm_jira_bot.config import Settings
from mm_jira_bot.domain import (
    MattermostPost,
    backend_datetime,
    backend_now,
    runtime_timezone,
)
from mm_jira_bot.formatting import truncate_for_summary

JIRA_SOURCE_VALUE = "Crit alert"
JIRA_IS_CRIT_ALERT_VALUE = "Да"


def jira_option(value: str, option_id: str | None = None) -> dict[str, str]:
    if option_id:
        return {"id": option_id}
    return {"value": value}


def format_jira_datetime(value: datetime) -> str:
    """Format a datetime for a Jira date-time picker field.

    Jira's REST API v2 expects ISO 8601 with a ``[+-]hhmm`` offset (no colon)
    and mandatory fractional seconds, e.g. ``2026-06-16T14:30:00.000+0300``.
    The ``dd.MM.yyyy HH:mm`` shown in the UI is only a display format.
    """
    return value.astimezone(runtime_timezone()).strftime("%Y-%m-%dT%H:%M:%S.000%z")


def build_jira_description(
    post: MattermostPost,
    *,
    message_url: str,
    channel_name: str | None,
    author_name: str | None = None,
) -> str:
    created_at = (
        backend_datetime(post.created_at_datetime).strftime("%d.%m.%Y %H:%M")
        if post.create_at > 0 and post.created_at_datetime
        else "—"
    )
    message = post.message.strip() or "—"
    lines = [
        "h3. 🔔 Алерт из Band",
        "",
        "{quote}",
        message,
        "{quote}",
        "",
        "||Параметр||Значение||",
        f"|Автор|{author_name or post.user_id}|",
        f"|Канал|{channel_name or post.channel_id}|",
        f"|Время сообщения|{created_at}|",
        f"|Исходное сообщение|[Открыть в Band|{message_url}]|",
        "",
        "----",
        f"_Идентификатор сообщения Band: {{{{{post.id}}}}}_",
    ]
    return "\n".join(lines)


def build_jira_issue_payload(
    settings: Settings,
    valid_incident_field_id: str,
    source_field_id: str,
    is_crit_alert_field_id: str,
    post: MattermostPost,
    *,
    message_url: str,
    channel_name: str | None,
    author_name: str | None = None,
    start_field_id: str | None = None,
    valid_incident_option: dict[str, str] | None = None,
    source_option: dict[str, str] | None = None,
    is_crit_alert_option: dict[str, str] | None = None,
) -> dict[str, Any]:
    issue_type: dict[str, str]
    if settings.jira_issue_type.isdigit():
        issue_type = {"id": settings.jira_issue_type}
    else:
        issue_type = {"name": settings.jira_issue_type}

    created_at = post.created_at_datetime if post.create_at > 0 else backend_now()
    alert_date = created_at.astimezone(runtime_timezone()).strftime("%d.%m.%Y")
    first_message_line = next(
        (line for line in post.message.splitlines() if line.strip()), ""
    )

    fields: dict[str, Any] = {
        "project": {"key": settings.jira_project_key},
        "issuetype": issue_type,
        "summary": f"[INC] {alert_date} - {truncate_for_summary(first_message_line)}",
        "description": build_jira_description(
            post,
            message_url=message_url,
            channel_name=channel_name,
            author_name=author_name,
        ),
        source_field_id: source_option or jira_option(JIRA_SOURCE_VALUE),
        is_crit_alert_field_id: is_crit_alert_option
        or jira_option(JIRA_IS_CRIT_ALERT_VALUE),
        "labels": ["mattermost-alert"],
    }
    if start_field_id is not None:
        fields[start_field_id] = format_jira_datetime(created_at)
    if valid_incident_option is not None:
        fields[valid_incident_field_id] = valid_incident_option
    return {"fields": fields}


def build_confirmation_comment(
    *,
    incident_message_url: str,
    confirmed_by_user_id: str,
) -> str:
    return (
        "Alert confirmed as a valid incident from Band.\n\n"
        f"Incident channel message: {incident_message_url}\n"
        f"Confirmed by: {confirmed_by_user_id}"
    )
