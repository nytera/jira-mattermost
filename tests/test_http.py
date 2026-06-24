from __future__ import annotations

import asyncio
import dataclasses

import httpx
import pytest

from mm_jira_bot.config import Settings
from mm_jira_bot.http import AsyncApiClient, wrap_transport_error
from mm_jira_bot.jira import JiraClient
from mm_jira_bot.logging import get_logger
from mm_jira_bot.retry import ApiError

LOG = get_logger("test_http")


def _retry_settings(settings: Settings) -> Settings:
    """Tighten retry knobs so retries don't really wait."""
    return dataclasses.replace(
        settings,
        api_retry_attempts=3,
        api_retry_base_delay_seconds=0.001,
    )


@pytest.fixture()
def no_sleep(monkeypatch):
    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


def test_wrap_transport_error_empty_stringify_rstrip():
    err = wrap_transport_error("msg", httpx.ConnectError("boom"))
    assert isinstance(err, ApiError)
    assert err.retryable is True
    assert "ConnectError" in str(err)
    assert str(err) == "msg: ConnectError: boom"

    # An httpx error whose str() is "" must not leave a trailing ": ".
    empty = wrap_transport_error("msg", httpx.ConnectError(""))
    assert str(empty) == "msg: ConnectError"
    assert not str(empty).endswith(": ")
    assert "ConnectError" in str(empty)


def test_raise_for_status_maps_success_and_failure(settings):
    client = AsyncApiClient(
        settings,
        httpx.AsyncClient(),
        own_client=False,
        log=LOG,
    )

    # 200 -> no raise.
    client._raise_for_status(httpx.Response(200, json={"ok": True}), "boom")

    # 503 -> retryable ApiError with status_code 503.
    with pytest.raises(ApiError) as exc_503:
        client._raise_for_status(httpx.Response(503, text="down"), "boom")
    assert exc_503.value.status_code == 503
    assert exc_503.value.retryable is True
    assert "HTTP 503" in str(exc_503.value)

    # 404 -> non-retryable ApiError.
    with pytest.raises(ApiError) as exc_404:
        client._raise_for_status(httpx.Response(404, text="missing"), "boom")
    assert exc_404.value.status_code == 404
    assert exc_404.value.retryable is False
    assert "HTTP 404" in str(exc_404.value)


async def test_request_transport_error_becomes_retryable_apierror(settings, no_sleep):
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectTimeout("connect timed out")

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://jira.example.com")
    client = AsyncApiClient(
        _retry_settings(settings),
        http_client,
        own_client=True,
        log=LOG,
    )
    try:
        with pytest.raises(ApiError) as exc:
            await client._request(
                "GET",
                "/anything",
                error_message="boom",
                event="test.request",
            )
    finally:
        await client.aclose()

    assert exc.value.retryable is True
    # Retried: handler invoked more than once (attempts >= 2).
    assert calls["count"] > 1
    assert calls["count"] == 3


async def test_jira_client_recovers_after_transient_503(settings, no_sleep):
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, text="temporarily down")
        return httpx.Response(200, json={"fields": {"customfield_12345": {"value": "Валидный"}}})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://jira.example.com")
    jira = JiraClient(_retry_settings(settings), http_client=http_client)
    try:
        result = await jira.get_valid_incident("OPS-1")
    finally:
        await jira.aclose()

    assert result is True
    assert calls["count"] == 2


async def test_jira_client_raises_after_exhausting_retries(settings, no_sleep):
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(503, text="still down")

    tuned = _retry_settings(settings)
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://jira.example.com")
    jira = JiraClient(tuned, http_client=http_client)
    try:
        with pytest.raises(ApiError) as exc:
            await jira.get_valid_incident("OPS-1")
    finally:
        await jira.aclose()

    assert exc.value.retryable is True
    assert exc.value.status_code == 503
    # Exactly api_retry_attempts calls; no infinite loop.
    assert calls["count"] == tuned.api_retry_attempts == 3


async def test_aclose_closes_client_only_when_own_client(settings):
    injected = httpx.AsyncClient()
    borrowed = AsyncApiClient(settings, injected, own_client=False, log=LOG)
    await borrowed.aclose()
    assert injected.is_closed is False
    await injected.aclose()

    owned_client = httpx.AsyncClient()
    owner = AsyncApiClient(settings, owned_client, own_client=True, log=LOG)
    await owner.aclose()
    assert owned_client.is_closed is True
