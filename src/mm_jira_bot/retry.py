from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from mm_jira_bot.logging import log_event

T = TypeVar("T")


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def is_retryable_status(status_code: int | None) -> bool:
    return status_code == 429 or (status_code is not None and 500 <= status_code <= 599)


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay_seconds: float,
    logger: logging.Logger,
    event: str,
    **log_fields: object,
) -> T:
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except ApiError as exc:
            last_error = exc
            if not exc.retryable or attempt >= attempts:
                raise
            delay = base_delay_seconds * (2 ** (attempt - 1))
            log_event(
                logger,
                logging.WARNING,
                f"{event}.retry",
                attempt=attempt,
                delay_seconds=delay,
                status_code=exc.status_code,
                error=str(exc),
                **log_fields,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("retry loop exited unexpectedly") from last_error
