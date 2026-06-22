from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

from mm_jira_bot.config import Settings
from mm_jira_bot.logging import EventLogger
from mm_jira_bot.retry import ApiError, is_retryable_status, retry_async

T = TypeVar("T")


def wrap_transport_error(message: str, exc: httpx.HTTPError) -> ApiError:
    """Turn a transport-level httpx error into a retryable :class:`ApiError`.

    Connect/read timeouts and connection drops otherwise propagate raw (httpx
    stringifies them to ``""``), escaping the ``except ApiError`` handlers and,
    for events handled inline, tearing down the websocket loop. Wrapping them
    lets ``retry_async`` retry and callers degrade gracefully.
    """
    return ApiError(
        f"{message}: {type(exc).__name__}: {exc}".rstrip(": "),
        retryable=True,
    )


class AsyncApiClient:
    """Shared base for the Mattermost/Jira REST clients.

    Owns the httpx client lifecycle and folds the per-request retry/HTTP
    boilerplate into ``_request`` / ``_retry``.
    """

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        own_client: bool,
        log: EventLogger,
    ) -> None:
        self._settings = settings
        self._client = client
        self._own_client = own_client
        self._log = log

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def _retry(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        event: str,
        **fields: Any,
    ) -> T:
        return await retry_async(
            operation,
            attempts=self._settings.api_retry_attempts,
            base_delay_seconds=self._settings.api_retry_base_delay_seconds,
            logger=self._log,
            event=event,
            **fields,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        error_message: str,
        event: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
        parse: Callable[[httpx.Response], T] | None = None,
        **fields: Any,
    ) -> T | None:
        async def operation() -> T | None:
            try:
                response = await self._client.request(method, path, json=json, params=params)
            except httpx.HTTPError as exc:
                raise wrap_transport_error(error_message, exc) from exc
            self._raise_for_status(response, error_message)
            return parse(response) if parse is not None else None

        return await self._retry(operation, event=event, **fields)

    def _raise_for_status(self, response: httpx.Response, message: str) -> None:
        if response.is_success:
            return
        raise ApiError(
            f"{message}: HTTP {response.status_code} {response.text}",
            status_code=response.status_code,
            retryable=is_retryable_status(response.status_code),
        )
