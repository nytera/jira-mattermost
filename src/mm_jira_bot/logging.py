from __future__ import annotations

import json
import logging
from collections import deque
from typing import Any

from mm_jira_bot.domain import backend_now

LOGGER_PREFIX = "mm_jira_bot."

TEXT_INFO_EVENT_ALLOWLIST = frozenset(
    {
        "startup.preflight.completed",
        "startup.preflight.check_failed",
        "mattermost.alert.received",
        "jira.issue.created",
        "jira.issue.create_failed",
        "jira.issue.create_stubbed",
        "incident.validity.updated",
        "incident.confirmed",
        "feedback.received",
        "postmortem.completed",
    }
)

TEXT_EVENT_LABELS = {
    "startup.preflight.completed": "startup preflight completed",
    "startup.preflight.check_failed": "startup preflight failed",
    "mattermost.alert.received": "alert received",
    "jira.issue.created": "jira issue created",
    "jira.issue.create_failed": "jira issue create failed",
    "jira.issue.create_stubbed": "jira issue stubbed",
    "incident.validity.updated": "validity updated",
    "incident.confirmed": "incident confirmed",
    "feedback.received": "feedback received",
    "postmortem.completed": "postmortem completed",
}

TEXT_FIELD_ALIASES = {
    "mattermost_post_id": "post",
    "jira_issue_key": "jira",
    "validity_label": "validity",
    "user_id": "user",
    "confirmed_by_user_id": "user",
    "reacted_by_user_id": "user",
    "incident_post_id": "incident",
    "creation_status": "status",
    "confirmation_status": "status",
    "dependency_count": "checks",
    "failed_count": "failed",
    "duration_ms": "duration_ms",
    "status": "status",
    "error": "error",
    "error_type": "error_type",
    "method": "method",
    "path": "path",
    "created": "created",
}

LEVEL_NAME_TO_NUMBER = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    extra = getattr(record, "extra_fields", None)
    return dict(extra) if isinstance(extra, dict) else {}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": backend_now().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Compact, human-scannable line: ``time LEVEL event key=value …``.

    The ``event`` extra field is already the log message, so it is dropped
    from the trailing key=value pairs to avoid repeating it.
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = backend_now().strftime("%Y-%m-%d %H:%M:%S")
        logger = record.name
        if logger.startswith(LOGGER_PREFIX):
            logger = logger[len(LOGGER_PREFIX) :]
        fields = _extra_fields(record)
        event = str(fields.pop("event", record.getMessage()))
        message = TEXT_EVENT_LABELS.get(event, record.getMessage())
        parts = [
            timestamp,
            f"{record.levelname:<7}",
            logger,
            message,
        ]
        for key, value in fields.items():
            alias = TEXT_FIELD_ALIASES.get(key)
            if alias is None:
                continue
            parts.append(f"{alias}={_render_value(value)}")
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class TextInfoFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        if record.levelno != logging.INFO:
            return True
        event = _extra_fields(record).get("event")
        # Foreign INFO records (uvicorn lifecycle/access, etc.) carry no ``event``
        # — let them through; only gate our own structured INFO events.
        if not isinstance(event, str):
            return True
        return event in TEXT_INFO_EVENT_ALLOWLIST


def _render_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    if text == "" or any(char in text for char in (" ", "=", '"')):
        return json.dumps(text, ensure_ascii=False)
    return text


def _build_formatter(log_format: str) -> logging.Formatter:
    if log_format.lower() == "text":
        return TextFormatter()
    return JsonFormatter()


def _coerce_field(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class LogRingBuffer:
    """In-memory ring buffer of recent log records for the debug admin UI."""

    def __init__(self, capacity: int) -> None:
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)

    def append(self, entry: dict[str, Any]) -> None:
        self._records.append(entry)

    def records(self, *, limit: int, min_levelno: int = 0) -> list[dict[str, Any]]:
        items = [r for r in self._records if r["levelno"] >= min_levelno]
        return items[-limit:]

    def clear(self) -> None:
        self._records.clear()


class LogBufferHandler(logging.Handler):
    def __init__(self, buffer: LogRingBuffer) -> None:
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields = _extra_fields(record)
            fields.pop("event", None)
            entry: dict[str, Any] = {
                "timestamp": backend_now().isoformat(),
                "level": record.levelname,
                "levelno": record.levelno,
                "logger": record.name,
                "message": record.getMessage(),
                "fields": {key: _coerce_field(value) for key, value in fields.items()},
            }
            if record.exc_info:
                entry["exception"] = self.formatException(record.exc_info)
            self._buffer.append(entry)
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)


_LOG_BUFFER: LogRingBuffer | None = None


def get_log_buffer() -> LogRingBuffer | None:
    return _LOG_BUFFER


def configure_logging(
    level: str = "INFO",
    log_format: str = "json",
    *,
    buffer_capacity: int = 2000,
) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_build_formatter(log_format))
    if log_format.lower() == "text":
        handler.addFilter(TextInfoFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    global _LOG_BUFFER
    if buffer_capacity > 0:
        if _LOG_BUFFER is None:
            _LOG_BUFFER = LogRingBuffer(buffer_capacity)
        root.addHandler(LogBufferHandler(_LOG_BUFFER))
    root.setLevel(level.upper())


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    exc_info: bool | BaseException | None = None,
    **fields: Any,
) -> None:
    logger.log(
        level,
        event,
        exc_info=exc_info,
        extra={"extra_fields": {"event": event, **fields}},
    )


class EventLogger:
    """Binds a stdlib logger so call sites read ``log.info(event, **fields)``.

    Pass ``exc_info=True`` inside an ``except`` block (or an explicit exception)
    to attach a traceback; every formatter/handler already renders it.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def info(
        self, event: str, *, exc_info: bool | BaseException | None = None, **fields: Any
    ) -> None:
        log_event(self._logger, logging.INFO, event, exc_info=exc_info, **fields)

    def warning(
        self, event: str, *, exc_info: bool | BaseException | None = None, **fields: Any
    ) -> None:
        log_event(self._logger, logging.WARNING, event, exc_info=exc_info, **fields)

    def error(
        self, event: str, *, exc_info: bool | BaseException | None = None, **fields: Any
    ) -> None:
        log_event(self._logger, logging.ERROR, event, exc_info=exc_info, **fields)

    def debug(
        self, event: str, *, exc_info: bool | BaseException | None = None, **fields: Any
    ) -> None:
        log_event(self._logger, logging.DEBUG, event, exc_info=exc_info, **fields)


def get_logger(name: str) -> EventLogger:
    return EventLogger(logging.getLogger(name))
