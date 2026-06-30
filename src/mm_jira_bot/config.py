from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import overload

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


@overload
def _env(name: str) -> str | None: ...


@overload
def _env(name: str, default: str) -> str: ...


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _required(name: str) -> str:
    value = _env(name)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _first_required(*names: str) -> str:
    for name in names:
        value = _env(name)
        if value is not None:
            return value
    raise RuntimeError("Missing required environment variable: " + " or ".join(names))


def _int_env(name: str, default: int) -> int:
    value = _env(name)
    return default if value is None else int(value)


def _float_env(name: str, default: float) -> float:
    value = _env(name)
    return default if value is None else float(value)


def _text_env(name: str) -> str | None:
    """Read a (possibly large, multi-line) text setting.

    Prefers ``<NAME>_FILE`` (a path whose contents become the value) over the
    inline ``<NAME>`` var, because ``load_dotenv_file`` is line-based and cannot
    carry a multi-line value from ``.env`` — the file variant is the practical
    way to supply a big prompt. A missing file path fails fast at startup.
    """
    file_path = _env(f"{name}_FILE")
    if file_path:
        return Path(file_path).read_text(encoding="utf-8")
    return _env(name)


def _first_env(*names: str) -> str | None:
    for name in names:
        value = _env(name)
        if value is not None:
            return value
    return None


def _csv_env(name: str) -> tuple[str, ...]:
    """Parse a comma/semicolon-separated env var into trimmed, non-empty items.

    Either ``,`` or ``;`` (mixed is fine) separates entries. A leading ``@``
    (e.g. ``@ivanov``) is stripped so operators can paste Mattermost mentions
    directly. Empty/unset -> empty tuple.
    """
    value = _env(name)
    if value is None:
        return ()
    items = [item.strip().lstrip("@") for item in re.split(r"[,;]", value)]
    return tuple(item for item in items if item)


@dataclass(frozen=True)
class Settings:
    mattermost_url: str
    mattermost_token: str
    mattermost_alert_channel_id: str
    mattermost_incident_channel_id: str
    mattermost_incident_reaction_name: str
    mattermost_bot_user_id: str
    jira_base_url: str
    jira_api_token: str
    jira_project_key: str
    jira_issue_type: str
    jira_valid_incident_field: str
    jira_source_field: str
    jira_is_crit_alert_field: str
    jira_start_field: str | None
    jira_end_field: str | None
    database_url: str
    read_only_mode: bool = False
    mattermost_audit_channel_id: str | None = None
    mattermost_test_alert_channel_id: str | None = None
    mattermost_test_incident_channel_id: str | None = None
    bind_host: str = "0.0.0.0"
    bind_port: int = 8080
    incident_timezone: str = "Europe/Moscow"
    duty_help_enabled: bool = True
    jira_time_to_fix_field: str | None = None
    jira_repeat_link_inward: str = "is child of"
    mattermost_false_incident_reaction_name: str = "man_gesturing_no"
    mattermost_expected_incident_reaction_name: str = "arrows_counterclockwise"
    mattermost_summary_reaction_name: str = "memo"
    mattermost_authorized_usernames: tuple[str, ...] = ()
    mattermost_authorized_refresh_seconds: int = 300
    mattermost_duty_mention: str | None = None
    mattermost_ops_channel_id: str | None = None
    ops_cooldown_seconds: int = 300
    log_level: str = "INFO"
    log_format: str = "json"
    api_retry_attempts: int = 4
    api_retry_base_delay_seconds: float = 0.5
    pending_work_interval_seconds: int = 30
    backfill_recent_posts_limit: int = 0
    enable_websocket: bool = True
    enable_backfill_on_startup: bool = False
    llm_base_url: str = "https://corellm.wb.ru/deepseek/v1"
    llm_api_token: str | None = None
    llm_model: str = "deepseek-chat"
    llm_max_tokens: int = 4000
    llm_thread_max_chars: int = 24000
    llm_summary_prompt: str | None = None
    llm_stream: bool = True
    llm_read_timeout: float = 120.0
    llm_stream_edit_interval_seconds: float = 1.5
    llm_stream_edit_min_chars: int = 80

    def __post_init__(self) -> None:
        configure_runtime_timezone(self.incident_timezone)

    @classmethod
    def from_env(cls, dotenv_path: str | Path = ".env") -> Settings:
        load_dotenv_file(dotenv_path)
        return cls(
            mattermost_url=_required("MATTERMOST_URL").rstrip("/"),
            mattermost_token=_required("MATTERMOST_TOKEN"),
            mattermost_alert_channel_id=_required("MATTERMOST_ALERT_CHANNEL_ID"),
            mattermost_incident_channel_id=_required("MATTERMOST_INCIDENT_CHANNEL_ID"),
            mattermost_incident_reaction_name=_env("MATTERMOST_INCIDENT_REACTION_NAME", "incident"),
            # Optional: empty ⇒ resolved from the bot token via /users/me at startup
            # (see IncidentBotService.resolve_bot_user_id). When set, preflight
            # cross-checks it against the token's real id.
            mattermost_bot_user_id=_env("MATTERMOST_BOT_USER_ID", ""),
            jira_base_url=_required("JIRA_BASE_URL").rstrip("/"),
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
            jira_start_field=_env("JIRA_START_FIELD"),
            jira_end_field=_env("JIRA_END_FIELD"),
            jira_time_to_fix_field=_env("JIRA_TIME_TO_FIX_FIELD"),
            jira_repeat_link_inward=_env("JIRA_REPEAT_LINK_INWARD", "is child of"),
            database_url=_required("DATABASE_URL"),
            read_only_mode=_env("READ_ONLY_MODE", "false") == "true",
            mattermost_audit_channel_id=_env("MATTERMOST_AUDIT_CHANNEL_ID"),
            mattermost_test_alert_channel_id=_env("MATTERMOST_TEST_ALERT_CHANNEL_ID"),
            mattermost_test_incident_channel_id=_env("MATTERMOST_TEST_INCIDENT_CHANNEL_ID"),
            bind_host=_env("HOST", "0.0.0.0"),
            bind_port=_int_env("PORT", 8080),
            incident_timezone=_env("INCIDENT_TIMEZONE", "Europe/Moscow"),
            duty_help_enabled=_env("DUTY_HELP_ENABLED", "true") != "false",
            mattermost_false_incident_reaction_name=_env(
                "MATTERMOST_FALSE_INCIDENT_REACTION_NAME", "man_gesturing_no"
            ),
            mattermost_expected_incident_reaction_name=_env(
                "MATTERMOST_EXPECTED_INCIDENT_REACTION_NAME", "arrows_counterclockwise"
            ),
            mattermost_summary_reaction_name=_env("MATTERMOST_SUMMARY_REACTION_NAME", "memo"),
            mattermost_authorized_usernames=_csv_env("MATTERMOST_AUTHORIZED_USERNAMES"),
            mattermost_authorized_refresh_seconds=_int_env(
                "MATTERMOST_AUTHORIZED_REFRESH_SECONDS", 300
            ),
            mattermost_duty_mention=_env("MATTERMOST_DUTY_MENTION"),
            mattermost_ops_channel_id=_env("MATTERMOST_OPS_CHANNEL_ID"),
            ops_cooldown_seconds=_int_env("MATTERMOST_OPS_COOLDOWN_SECONDS", 300),
            log_level=_env("LOG_LEVEL", "INFO"),
            log_format=_env("LOG_FORMAT", "json"),
            api_retry_attempts=_int_env("API_RETRY_ATTEMPTS", 4),
            api_retry_base_delay_seconds=_float_env("API_RETRY_BASE_DELAY_SECONDS", 0.5),
            pending_work_interval_seconds=_int_env("PENDING_WORK_INTERVAL_SECONDS", 30),
            backfill_recent_posts_limit=_int_env("BACKFILL_RECENT_POSTS_LIMIT", 0),
            enable_websocket=_env("ENABLE_WEBSOCKET", "true") != "false",
            enable_backfill_on_startup=_env("ENABLE_BACKFILL_ON_STARTUP", "false") == "true",
            llm_base_url=_env("LLM_BASE_URL", "https://corellm.wb.ru/deepseek/v1").rstrip("/"),
            llm_api_token=_first_env("LLM_API_TOKEN", "CORELLM_API_TOKEN", "OPENAI_API_KEY"),
            llm_model=_env("LLM_MODEL", "deepseek-chat"),
            llm_max_tokens=_int_env("LLM_MAX_TOKENS", 4000),
            llm_thread_max_chars=_int_env("LLM_THREAD_MAX_CHARS", 24000),
            llm_summary_prompt=_text_env("LLM_SUMMARY_PROMPT"),
            llm_stream=_env("LLM_STREAM", "true") != "false",
            llm_read_timeout=_float_env("LLM_READ_TIMEOUT", 120.0),
            llm_stream_edit_interval_seconds=_float_env("LLM_STREAM_EDIT_INTERVAL_SECONDS", 1.5),
            llm_stream_edit_min_chars=_int_env("LLM_STREAM_EDIT_MIN_CHARS", 80),
        )
