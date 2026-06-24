from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from mm_jira_bot import retry as retry_module
from mm_jira_bot.logging import EventLogger
from mm_jira_bot.retry import ApiError, is_retryable_status, retry_async


class FakeEventLogger(EventLogger):
    """Captures ``warning`` calls without touching the real logging stack.

    Subclasses ``EventLogger`` only to satisfy the ``retry_async`` type; the
    overridden ``warning`` never reaches the underlying ``logging`` machinery.
    """

    def __init__(self) -> None:
        super().__init__(logging.getLogger("test-retry"))
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(  # type: ignore[override]
        self, event: str, **fields: Any
    ) -> None:
        self.warnings.append((event, fields))


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace ``asyncio.sleep`` in the retry module with a no-wait capture."""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(retry_module.asyncio, "sleep", fake_sleep)
    return delays


def test_is_retryable_status_classification_boundaries() -> None:
    assert is_retryable_status(429) is True
    assert is_retryable_status(500) is True
    assert is_retryable_status(599) is True
    assert is_retryable_status(499) is False
    assert is_retryable_status(600) is False
    assert is_retryable_status(None) is False
    for code in (400, 401, 403, 404, 408):
        assert is_retryable_status(code) is False


async def test_retry_async_backoff_uses_exponential_delays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = _patch_sleep(monkeypatch)
    logger = FakeEventLogger()
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise ApiError("boom", status_code=500, retryable=True)

    with pytest.raises(ApiError):
        await retry_async(
            operation,
            attempts=4,
            base_delay_seconds=0.5,
            logger=logger,
            event="jira.create",
        )

    assert calls == 4
    assert delays == [0.5, 1.0, 2.0]


async def test_retry_async_exhausts_and_reraises_last_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sleep(monkeypatch)
    logger = FakeEventLogger()
    calls = 0
    raised: list[ApiError] = []

    async def operation() -> None:
        nonlocal calls
        calls += 1
        exc = ApiError(f"boom-{calls}", status_code=503, retryable=True)
        raised.append(exc)
        raise exc

    with pytest.raises(ApiError) as exc_info:
        await retry_async(
            operation,
            attempts=3,
            base_delay_seconds=0.1,
            logger=logger,
            event="jira.create",
        )

    assert calls == 3
    assert exc_info.value is raised[-1]
    assert str(exc_info.value) == "boom-3"
    # The last retryable error is re-raised, not the terminal RuntimeError.
    assert type(exc_info.value) is ApiError


async def test_retry_async_non_retryable_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = _patch_sleep(monkeypatch)
    logger = FakeEventLogger()
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise ApiError("nope", status_code=400, retryable=False)

    with pytest.raises(ApiError):
        await retry_async(
            operation,
            attempts=3,
            base_delay_seconds=0.5,
            logger=logger,
            event="jira.create",
        )

    assert calls == 1
    assert delays == []
    assert not any(event == "jira.create.retry" for event, _ in logger.warnings)


async def test_retry_async_succeeds_on_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = _patch_sleep(monkeypatch)
    logger = FakeEventLogger()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ApiError("transient", status_code=500, retryable=True)
        return "ok"

    result = await retry_async(
        operation,
        attempts=3,
        base_delay_seconds=0.5,
        logger=logger,
        event="jira.create",
    )

    assert result == "ok"
    assert calls == 2
    assert delays == [0.5]
    retry_warnings = [event for event, _ in logger.warnings if event == "jira.create.retry"]
    assert len(retry_warnings) == 1


async def test_retry_async_non_apierror_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = _patch_sleep(monkeypatch)
    logger = FakeEventLogger()
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("not an api error")

    with pytest.raises(ValueError):
        await retry_async(
            operation,
            attempts=3,
            base_delay_seconds=0.5,
            logger=logger,
            event="jira.create",
        )

    assert calls == 1
    assert delays == []
    assert logger.warnings == []

    cancel_calls = 0

    async def cancelling_operation() -> None:
        nonlocal cancel_calls
        cancel_calls += 1
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await retry_async(
            cancelling_operation,
            attempts=3,
            base_delay_seconds=0.5,
            logger=logger,
            event="jira.create",
        )

    assert cancel_calls == 1
    assert delays == []


async def test_retry_async_attempts_zero_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = _patch_sleep(monkeypatch)
    logger = FakeEventLogger()
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise ApiError("never called", status_code=500, retryable=True)

    with pytest.raises(RuntimeError) as exc_info:
        await retry_async(
            operation,
            attempts=0,
            base_delay_seconds=0.5,
            logger=logger,
            event="jira.create",
        )

    assert calls == 0
    assert delays == []
    assert type(exc_info.value) is RuntimeError
    assert str(exc_info.value) == "retry loop exited unexpectedly"
    assert exc_info.value.__cause__ is None
