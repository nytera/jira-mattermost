from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from mm_jira_bot.domain import configure_runtime_timezone


def load_dotenv_file(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _first_required(*names: str) -> str:
    for name in names:
        value = _optional(name)
        if value is not None:
            return value
    raise RuntimeError(
        "Missing required environment variable: " + " or ".join(names)
    )


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


@dataclass(frozen=True)
class Settings:
    mattermost_url: str
    mattermost_token: str
    mattermost_alert_channel_id: str
    mattermost_incident_channel_id: str
    mattermost_incident_reaction_name: str
    mattermost_bot_user_id: str
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    jira_project_key: str
    jira_issue_type: str
    jira_valid_incident_field: str
    jira_source_field: str
    jira_is_crit_alert_field: str
    jira_start_field: str | None
    jira_confirmed_status_id: str | None
    database_url: str
    incident_timezone: str = "Europe/Moscow"
    mattermost_slash_token: str | None = None
    log_level: str = "INFO"
    api_retry_attempts: int = 4
    api_retry_base_delay_seconds: float = 0.5
    pending_work_interval_seconds: int = 30
    backfill_recent_posts_limit: int = 0
    enable_websocket: bool = True
    enable_backfill_on_startup: bool = False
    debug_admin_enabled: bool = False

    def __post_init__(self) -> None:
        configure_runtime_timezone(self.incident_timezone)

    @classmethod
    def from_env(cls, dotenv_path: str | Path = ".env") -> "Settings":
        load_dotenv_file(dotenv_path)
        return cls(
            mattermost_url=_required("MATTERMOST_URL").rstrip("/"),
            mattermost_token=_required("MATTERMOST_TOKEN"),
            mattermost_alert_channel_id=_required("MATTERMOST_ALERT_CHANNEL_ID"),
            mattermost_incident_channel_id=_required("MATTERMOST_INCIDENT_CHANNEL_ID"),
            mattermost_incident_reaction_name=_optional(
                "MATTERMOST_INCIDENT_REACTION_NAME", "incident"
            )
            or "incident",
            mattermost_bot_user_id=_required("MATTERMOST_BOT_USER_ID"),
            jira_base_url=_required("JIRA_BASE_URL").rstrip("/"),
            jira_email=_required("JIRA_EMAIL"),
            jira_api_token=_required("JIRA_API_TOKEN"),
            jira_project_key=_required("JIRA_PROJECT_KEY"),
            jira_issue_type=_required("JIRA_ISSUE_TYPE"),
            jira_valid_incident_field=_first_required(
                "JIRA_VALID_INCIDENT_FIELD",
                "JIRA_VALID_INCIDENT_FIELD_NAME",
                "JIRA_VALID_INCIDENT_FIELD_ID",
            ),
            jira_source_field=_required("JIRA_SOURCE_FIELD"),
            jira_is_crit_alert_field=_required("JIRA_IS_CRIT_ALERT_FIELD"),
            jira_start_field=_optional("JIRA_START_FIELD"),
            jira_confirmed_status_id=_optional("JIRA_CONFIRMED_STATUS_ID"),
            database_url=_required("DATABASE_URL"),
            incident_timezone=_optional("INCIDENT_TIMEZONE", "Europe/Moscow")
            or "Europe/Moscow",
            mattermost_slash_token=_optional("MATTERMOST_SLASH_TOKEN"),
            log_level=_optional("LOG_LEVEL", "INFO") or "INFO",
            api_retry_attempts=_int_env("API_RETRY_ATTEMPTS", 4),
            api_retry_base_delay_seconds=_float_env(
                "API_RETRY_BASE_DELAY_SECONDS", 0.5
            ),
            pending_work_interval_seconds=_int_env("PENDING_WORK_INTERVAL_SECONDS", 30),
            backfill_recent_posts_limit=_int_env("BACKFILL_RECENT_POSTS_LIMIT", 0),
            enable_websocket=_optional("ENABLE_WEBSOCKET", "true") != "false",
            enable_backfill_on_startup=_optional("ENABLE_BACKFILL_ON_STARTUP", "false")
            == "true",
            debug_admin_enabled=_optional("DEBUG_ADMIN_ENABLED", "false") == "true",
        )
