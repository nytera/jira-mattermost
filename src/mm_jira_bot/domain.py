from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

_runtime_timezone = ZoneInfo("UTC")


def configure_runtime_timezone(timezone_name: str) -> None:
    global _runtime_timezone
    _runtime_timezone = ZoneInfo(timezone_name)


def runtime_timezone() -> ZoneInfo:
    return _runtime_timezone


def backend_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_runtime_timezone)


def datetime_from_mattermost_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=_runtime_timezone)


def backend_now() -> datetime:
    return datetime.now(_runtime_timezone)


def _attachment_field_text(field: dict) -> str | None:
    title = str(field.get("title") or "").strip()
    value = str(field.get("value") or "").strip()
    if title and value:
        return f"{title}: {value}"
    return title or value or None


def _attachment_text(attachment: dict) -> str:
    lines: list[str] = []
    for key in ("pretext", "title", "text"):
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
    return "\n".join(lines)


def _message_from_api(data: dict) -> str:
    message = data.get("message") or ""
    if message:
        return str(message)

    props = data.get("props")
    if not isinstance(props, dict):
        return ""
    attachments = props.get("attachments")
    if not isinstance(attachments, list):
        return ""
    return "\n\n".join(
        text
        for attachment in attachments
        if isinstance(attachment, dict)
        for text in [_attachment_text(attachment)]
        if text
    )


@dataclass(frozen=True)
class MattermostPost:
    id: str
    channel_id: str
    user_id: str
    message: str
    create_at: int
    channel_name: str | None = None
    root_id: str | None = None
    props: dict | None = None
    post_type: str = ""

    @classmethod
    def from_api(cls, data: dict, channel_name: str | None = None) -> MattermostPost:
        props = data.get("props")
        return cls(
            id=data["id"],
            channel_id=data["channel_id"],
            user_id=data["user_id"],
            message=_message_from_api(data),
            create_at=int(data.get("create_at") or 0),
            channel_name=channel_name,
            root_id=data.get("root_id") or None,
            props=props if isinstance(props, dict) else None,
            post_type=data.get("type") or "",
        )

    @property
    def created_at_datetime(self) -> datetime | None:
        return datetime_from_mattermost_ms(self.create_at)


@dataclass(frozen=True)
class ReactionEvent:
    post_id: str
    user_id: str
    emoji_name: str
    create_at: int


class ConfirmationStatus(StrEnum):
    CONFIRMED = "confirmed"
    ALREADY_CONFIRMED = "already_confirmed"
    PENDING_JIRA = "pending_jira"
    NOT_FOUND = "not_found"
    IGNORED = "ignored"
    ERROR = "error"
    VALIDITY_SET = "validity_set"
    INCIDENT_ENDED = "incident_ended"


@dataclass(frozen=True)
class ConfirmationResult:
    status: ConfirmationStatus
    message: str
    jira_issue_url: str | None = None
    incident_message_url: str | None = None


@dataclass(frozen=True)
class JiraIssue:
    key: str
    url: str
