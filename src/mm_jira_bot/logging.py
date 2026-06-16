from __future__ import annotations

import json
import logging
from typing import Any

from mm_jira_bot.domain import backend_now


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": backend_now().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
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
