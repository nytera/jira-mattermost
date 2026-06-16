from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_runtime_timezone)


def datetime_from_mattermost_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=_runtime_timezone)


def backend_now() -> datetime:
    return datetime.now(_runtime_timezone)


@dataclass(frozen=True)
class MattermostPost:
    id: str
    channel_id: str
    user_id: str
    message: str
    create_at: int
    channel_name: str | None = None

    @classmethod
    def from_api(cls, data: dict, channel_name: str | None = None) -> "MattermostPost":
        return cls(
            id=data["id"],
            channel_id=data["channel_id"],
            user_id=data["user_id"],
            message=data.get("message", ""),
            create_at=int(data.get("create_at") or 0),
            channel_name=channel_name,
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
