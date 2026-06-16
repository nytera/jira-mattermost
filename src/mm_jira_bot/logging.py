from __future__ import annotations

import json
import logging
from typing import Any

from mm_jira_bot.domain import backend_now

LOGGER_PREFIX = "mm_jira_bot."


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
        parts = [
            timestamp,
            f"{record.levelname:<7}",
            logger,
            record.getMessage(),
        ]
        fields = _extra_fields(record)
        fields.pop("event", None)
        for key, value in fields.items():
            parts.append(f"{key}={_render_value(value)}")
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


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


def configure_logging(level: str = "INFO", log_format: str = "json") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_build_formatter(log_format))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    logger.log(level, event, extra={"extra_fields": {"event": event, **fields}})


class EventLogger:
    """Binds a stdlib logger so call sites read ``log.info(event, **fields)``."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def info(self, event: str, **fields: Any) -> None:
        log_event(self._logger, logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        log_event(self._logger, logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        log_event(self._logger, logging.ERROR, event, **fields)

    def debug(self, event: str, **fields: Any) -> None:
        log_event(self._logger, logging.DEBUG, event, **fields)


def get_logger(name: str) -> EventLogger:
    return EventLogger(logging.getLogger(name))
