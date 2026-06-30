from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any, TypeVar, overload

import httpx

from mm_jira_bot.config import Settings
from mm_jira_bot.logging import EventLogger
from mm_jira_bot.metrics import observe_http
from mm_jira_bot.retry import ApiError, is_retryable_status, retry_async

T = TypeVar("T")

#: HTTP methods that mutate server state. In read-only mode any such request is
#: blocked by the ``_request`` backstop unless the caller passes
#: ``allow_in_read_only=True`` (the audit-channel post, or a read that happens to
#: use POST, e.g. ``/users/usernames``).
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


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

    #: Label for Prometheus HTTP metrics; overridden per concrete client.
    metrics_client_name: str = "unknown"

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

    @overload
    async def _request(
        self,
        method: str,
        path: str,
        *,
        error_message: str,
        event: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
        parse: Callable[[httpx.Response], T],
        allow_in_read_only: bool = False,
        **fields: Any,
    ) -> T: ...

    @overload
    async def _request(
        self,
        method: str,
        path: str,
        *,
        error_message: str,
        event: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
        parse: None = None,
        allow_in_read_only: bool = False,
        **fields: Any,
    ) -> None: ...

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
        allow_in_read_only: bool = False,
        **fields: Any,
    ) -> T | None:
        if (
            self._settings.read_only_mode
            and method.upper() in _WRITE_METHODS
            and not allow_in_read_only
        ):
            # Last-resort backstop: every write is supposed to be suppressed or
            # redirected to the audit channel at the client-method level. Reaching
            # here means one slipped through — fail loudly rather than mutate prod.
            raise RuntimeError(
                f"read-only backstop blocked {method.upper()} {path} "
                f"({self.metrics_client_name}); writes must be suppressed before _request"
            )

        async def operation() -> T | None:
            started = perf_counter()
            status = "error"
            try:
                try:
                    response = await self._client.request(method, path, json=json, params=params)
                except httpx.HTTPError as exc:
                    raise wrap_transport_error(error_message, exc) from exc
                status = str(response.status_code)
                self._raise_for_status(response, error_message)
                return parse(response) if parse is not None else None
            finally:
                observe_http(self.metrics_client_name, method, status, perf_counter() - started)

        return await self._retry(operation, event=event, **fields)

    def _raise_for_status(self, response: httpx.Response, message: str) -> None:
        if response.is_success:
            return
        raise ApiError(
            f"{message}: HTTP {response.status_code} {response.text}",
            status_code=response.status_code,
            retryable=is_retryable_status(response.status_code),
        )
