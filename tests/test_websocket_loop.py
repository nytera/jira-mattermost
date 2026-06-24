from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from support import _capture_bot_logs, _extra_fields

from mm_jira_bot.mattermost import (
    MattermostClient,
    parse_posted_event,
    parse_reaction_event,
)
from mm_jira_bot.web import (
    _HANDLED_WS_EVENTS,
    _handle_ws_event,
    authorized_users_refresh_loop,
    pending_work_loop,
    websocket_loop,
)

# --- helpers ----------------------------------------------------------------

# The loops call ``asyncio.sleep`` via ``mm_jira_bot.web.asyncio.sleep``, which is
# the real module attribute. Tests monkeypatch that name, so a fake_sleep that
# wants to yield to the loop must call this captured original — not the patched
# name, which would recurse into itself.
_real_sleep = asyncio.sleep


async def _yield_events(events: list[dict]):
    """Async generator standing in for MattermostClient.websocket_events().

    After draining the fixed list it suspends forever (``asyncio.Event().wait()``)
    so the ``while True`` read loop parks at a real suspension point instead of
    busy-reconnecting; the driving test cancels it there.
    """
    for event in events:
        yield event
    await asyncio.Event().wait()


def _capture():
    records: list[logging.LogRecord] = []
    logger, handler = _capture_bot_logs(records)
    return records, logger, handler


# --- websocket_loop dispatch ------------------------------------------------


async def test_websocket_loop_dispatches_handled_and_skips_unhandled(monkeypatch):
    posted = {"event": "posted", "data": {}}
    reaction = {"event": "reaction_added", "data": {}}
    typing = {"event": "typing", "data": {}}

    assert "posted" in _HANDLED_WS_EVENTS
    assert "reaction_added" in _HANDLED_WS_EVENTS
    assert "typing" not in _HANDLED_WS_EVENTS

    handled: list[dict] = []

    async def handle_websocket_event(event: dict) -> None:
        handled.append(event)

    def websocket_events():
        # After delivering the events the loop would reconnect; raising
        # CancelledError from the (patched) sleep is the clean exit. But this
        # stream does not error, so it simply re-enters the async-for. Make the
        # second connect attempt cancel the loop.
        return _yield_events([typing, posted, reaction])

    service: Any = SimpleNamespace(
        mattermost=SimpleNamespace(websocket_events=websocket_events),
        handle_websocket_event=handle_websocket_event,
    )

    task = asyncio.create_task(websocket_loop(service))
    # Let the loop drain the single stream pass and spawn handler tasks.
    await _real_sleep(0)
    await _real_sleep(0)
    await _real_sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # typing was filtered before a task was spawned; only the two handled
    # events reached handle_websocket_event.
    assert {e["event"] for e in handled} == {"posted", "reaction_added"}
    assert len(handled) == 2


async def test_websocket_loop_reconnects_after_stream_error(monkeypatch):
    connects = {"count": 0}

    def websocket_events():
        connects["count"] += 1
        if connects["count"] == 1:

            async def boom():
                raise RuntimeError("socket dropped")
                yield  # pragma: no cover - makes this an async generator

            return boom()
        # Second connect: a stream that simply ends, then the loop would try a
        # third connect — but the patched sleep below already exited the loop.
        return _yield_events([{"event": "posted", "data": {}}])

    handled: list[dict] = []

    async def handle_websocket_event(event: dict) -> None:
        handled.append(event)

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        # Yield to the loop without really waiting, so the reconnect happens.
        await _real_sleep(0)

    monkeypatch.setattr("mm_jira_bot.web.asyncio.sleep", fake_sleep)

    service: Any = SimpleNamespace(
        mattermost=SimpleNamespace(websocket_events=websocket_events),
        handle_websocket_event=handle_websocket_event,
    )

    records, logger, handler = _capture()
    try:
        task = asyncio.create_task(websocket_loop(service))
        for _ in range(10):
            await _real_sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        logger.removeHandler(handler)

    failures = [r for r in records if r.msg == "mattermost.websocket.failed"]
    assert failures
    assert _extra_fields(failures[0])["error_type"] == "RuntimeError"
    assert failures[0].exc_info is not None
    # It slept (the 5s reconnect backoff) and reconnected at least once.
    assert sleeps and sleeps[0] == 5
    assert connects["count"] >= 2
    # The second connect's event was handled — proving the loop recovered.
    assert {e["event"] for e in handled} == {"posted"}


async def test_websocket_loop_propagates_cancelled_without_reconnect(monkeypatch):
    connects = {"count": 0}

    def websocket_events():
        connects["count"] += 1

        async def cancel_stream():
            raise asyncio.CancelledError
            yield  # pragma: no cover

        return cancel_stream()

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("mm_jira_bot.web.asyncio.sleep", fake_sleep)

    service: Any = SimpleNamespace(
        mattermost=SimpleNamespace(websocket_events=websocket_events),
        handle_websocket_event=lambda event: None,
    )

    with pytest.raises(asyncio.CancelledError):
        await websocket_loop(service)

    # CancelledError propagated: exactly one connect, no reconnect backoff sleep.
    assert connects["count"] == 1
    assert sleeps == []


# --- _handle_ws_event isolation ---------------------------------------------


async def test_handle_ws_event_swallows_handler_failure_and_logs():
    async def handle_websocket_event(event: dict) -> None:
        raise ValueError("handler blew up")

    service: Any = SimpleNamespace(handle_websocket_event=handle_websocket_event)

    records, logger, handler = _capture()
    try:
        # Must not raise — the read loop is protected.
        await _handle_ws_event(service, {"event": "posted"})
    finally:
        logger.removeHandler(handler)

    failures = [r for r in records if r.msg == "mattermost.event.handler_failed"]
    assert failures
    assert _extra_fields(failures[0])["error_type"] == "ValueError"
    assert failures[0].exc_info is not None


async def test_handle_ws_event_reraises_cancelled():
    async def handle_websocket_event(event: dict) -> None:
        raise asyncio.CancelledError

    service: Any = SimpleNamespace(handle_websocket_event=handle_websocket_event)

    with pytest.raises(asyncio.CancelledError):
        await _handle_ws_event(service, {"event": "posted"})


async def test_websocket_loop_handler_failure_does_not_kill_read_loop():
    """A failing handler task is logged but the read loop keeps draining."""
    handled: list[dict] = []

    async def handle_websocket_event(event: dict) -> None:
        handled.append(event)
        if event.get("data") == "fail":
            raise RuntimeError("boom")

    events = [
        {"event": "posted", "data": "fail"},
        {"event": "posted", "data": "ok"},
    ]

    service: Any = SimpleNamespace(
        mattermost=SimpleNamespace(websocket_events=lambda: _yield_events(events)),
        handle_websocket_event=handle_websocket_event,
    )

    records, logger, handler = _capture()
    try:
        task = asyncio.create_task(websocket_loop(service))
        for _ in range(6):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        logger.removeHandler(handler)

    # Both events were dispatched despite the first handler raising.
    assert [e["data"] for e in handled] == ["fail", "ok"]
    failures = [r for r in records if r.msg == "mattermost.event.handler_failed"]
    assert len(failures) == 1
    assert _extra_fields(failures[0])["error_type"] == "RuntimeError"


# --- pending_work_loop ------------------------------------------------------


async def test_pending_work_loop_survives_iteration_error(monkeypatch):
    ticks = {"count": 0}

    async def process_pending_work() -> None:
        ticks["count"] += 1
        if ticks["count"] == 1:
            raise RuntimeError("transient")
        # second (successful) tick — record and stop scheduling further work.

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        # The inter-tick sleep is the loop's only suspension point here; yield so
        # cancellation can land and the next tick can run.
        await _real_sleep(0)

    monkeypatch.setattr("mm_jira_bot.web.asyncio.sleep", fake_sleep)

    service: Any = SimpleNamespace(
        process_pending_work=process_pending_work,
        settings=SimpleNamespace(pending_work_interval_seconds=30),
    )

    records, logger, handler = _capture()
    try:
        task = asyncio.create_task(pending_work_loop(service))
        for _ in range(10):
            await _real_sleep(0)
            if ticks["count"] >= 2:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        logger.removeHandler(handler)

    failures = [r for r in records if r.msg == "pending_work.failed"]
    assert len(failures) == 1
    assert _extra_fields(failures[0])["error_type"] == "RuntimeError"
    # Loop continued past the failure: a second tick ran, sleeping the
    # configured interval between iterations.
    assert ticks["count"] >= 2
    assert sleeps and sleeps[0] == 30


async def test_pending_work_loop_propagates_cancelled(monkeypatch):
    async def process_pending_work() -> None:
        raise asyncio.CancelledError

    async def fake_sleep(delay: float) -> None:  # pragma: no cover - never reached
        return None

    monkeypatch.setattr("mm_jira_bot.web.asyncio.sleep", fake_sleep)

    service: Any = SimpleNamespace(
        process_pending_work=process_pending_work,
        settings=SimpleNamespace(pending_work_interval_seconds=30),
    )

    with pytest.raises(asyncio.CancelledError):
        await pending_work_loop(service)


# --- authorized_users_refresh_loop ------------------------------------------


async def test_authorized_users_refresh_loop_survives_error(monkeypatch):
    calls = {"count": 0}

    async def resolve_authorized_users() -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("ldap down")

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        # This loop sleeps before each refresh; yield so cancellation lands and
        # the next refresh runs.
        await _real_sleep(0)

    monkeypatch.setattr("mm_jira_bot.web.asyncio.sleep", fake_sleep)

    service: Any = SimpleNamespace(
        resolve_authorized_users=resolve_authorized_users,
        settings=SimpleNamespace(mattermost_authorized_refresh_seconds=600),
    )

    records, logger, handler = _capture()
    try:
        task = asyncio.create_task(authorized_users_refresh_loop(service))
        for _ in range(10):
            await _real_sleep(0)
            if calls["count"] >= 2:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        logger.removeHandler(handler)

    failures = [r for r in records if r.msg == "authorized_users.refresh_failed"]
    assert len(failures) == 1
    assert _extra_fields(failures[0])["error_type"] == "RuntimeError"
    assert calls["count"] >= 2
    # The loop sleeps before each refresh.
    assert sleeps and sleeps[0] == 600


async def test_authorized_users_refresh_loop_propagates_cancelled(monkeypatch):
    async def resolve_authorized_users() -> None:
        raise asyncio.CancelledError

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("mm_jira_bot.web.asyncio.sleep", fake_sleep)

    service: Any = SimpleNamespace(
        resolve_authorized_users=resolve_authorized_users,
        settings=SimpleNamespace(mattermost_authorized_refresh_seconds=600),
    )

    with pytest.raises(asyncio.CancelledError):
        await authorized_users_refresh_loop(service)


def test_authorized_users_refresh_loop_gated_by_config():
    """The lifespan only spawns the refresh loop when usernames are configured.

    This characterizes the gate in create_app's lifespan
    (``if settings.mattermost_authorized_usernames``): an empty allowlist means
    no refresh task is scheduled.
    """
    from mm_jira_bot import web as web_module

    source = web_module.__file__
    with open(source, encoding="utf-8") as fh:
        text = fh.read()
    assert "if settings.mattermost_authorized_usernames:" in text
    assert "authorized_users_refresh_loop(service)" in text


# --- parse_posted_event -----------------------------------------------------


def test_parse_posted_event_rejects_wrong_event():
    assert parse_posted_event({"event": "reaction_added", "data": {}}) is None
    assert parse_posted_event({"event": "typing", "data": {}}) is None
    assert parse_posted_event({}) is None


def test_parse_posted_event_missing_post_returns_none():
    assert parse_posted_event({"event": "posted", "data": {}}) is None
    assert parse_posted_event({"event": "posted"}) is None
    assert parse_posted_event({"event": "posted", "data": {"post": ""}}) is None


def _post_api_dict() -> dict[str, Any]:
    return {
        "id": "post-1",
        "channel_id": "chan-1",
        "user_id": "user-1",
        "message": "hello",
        "create_at": 1_700_000_000_000,
    }


def test_parse_posted_event_accepts_raw_post_as_string():
    payload = {
        "event": "posted",
        "data": {
            "post": json.dumps(_post_api_dict()),
            "channel_name": "alerts",
        },
    }
    post = parse_posted_event(payload)
    assert post is not None
    assert post.id == "post-1"
    assert post.channel_id == "chan-1"
    assert post.channel_name == "alerts"


def test_parse_posted_event_accepts_raw_post_as_dict():
    payload = {
        "event": "posted",
        "data": {
            "post": _post_api_dict(),
            "channel_display_name": "alerts-display",
        },
    }
    post = parse_posted_event(payload)
    assert post is not None
    assert post.id == "post-1"
    # Falls back to channel_display_name when channel_name is absent.
    assert post.channel_name == "alerts-display"


# --- parse_reaction_event ---------------------------------------------------


def test_parse_reaction_event_rejects_wrong_event():
    assert parse_reaction_event({"event": "posted", "data": {}}) is None
    assert parse_reaction_event({"event": "typing", "data": {}}) is None
    assert parse_reaction_event({}) is None


def test_parse_reaction_event_missing_reaction_returns_none():
    assert parse_reaction_event({"event": "reaction_added", "data": {}}) is None
    assert parse_reaction_event({"event": "reaction_added"}) is None
    assert parse_reaction_event({"event": "reaction_added", "data": {"reaction": ""}}) is None


def _reaction_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "post_id": "post-1",
        "user_id": "user-1",
        "emoji_name": "incident",
        "create_at": 1_700_000_000_000,
    }
    base.update(overrides)
    return base


def test_parse_reaction_event_accepts_raw_reaction_as_string():
    payload = {
        "event": "reaction_added",
        "data": {"reaction": json.dumps(_reaction_dict())},
    }
    reaction = parse_reaction_event(payload)
    assert reaction is not None
    assert reaction.post_id == "post-1"
    assert reaction.user_id == "user-1"
    assert reaction.emoji_name == "incident"
    assert reaction.create_at == 1_700_000_000_000


def test_parse_reaction_event_accepts_raw_reaction_as_dict():
    payload = {
        "event": "reaction_added",
        "data": {"reaction": _reaction_dict(create_at=42)},
    }
    reaction = parse_reaction_event(payload)
    assert reaction is not None
    assert reaction.create_at == 42


def test_parse_reaction_event_missing_create_at_defaults_to_zero():
    reaction_dict = _reaction_dict()
    del reaction_dict["create_at"]
    payload = {"event": "reaction_added", "data": {"reaction": reaction_dict}}
    reaction = parse_reaction_event(payload)
    assert reaction is not None
    assert reaction.create_at == 0


def test_parse_reaction_event_missing_post_id_raises_keyerror():
    """A reaction frame without post_id is malformed and surfaces a KeyError
    rather than being silently mis-routed."""
    reaction_dict = _reaction_dict()
    del reaction_dict["post_id"]
    payload = {"event": "reaction_added", "data": {"reaction": reaction_dict}}
    with pytest.raises(KeyError):
        parse_reaction_event(payload)


# --- websocket_events authentication ----------------------------------------


async def test_websocket_events_authenticates_then_yields_frames(settings, monkeypatch):
    client = MattermostClient(settings)
    sent: list[str] = []
    frames = [json.dumps({"event": "hello"}), json.dumps({"event": "posted"})]

    class _FakeWs:
        async def send(self, message: str) -> None:
            sent.append(message)

        async def __aiter__(self):
            for frame in frames:
                yield frame

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    def fake_connect(url: str, **kwargs: Any) -> _FakeWs:
        return _FakeWs()

    monkeypatch.setattr("mm_jira_bot.mattermost.websockets.connect", fake_connect)

    received = [event async for event in client.websocket_events()]
    await client.aclose()

    # Exactly one authentication_challenge frame (seq 1, token) is sent first.
    assert len(sent) == 1
    auth = json.loads(sent[0])
    assert auth["seq"] == 1
    assert auth["action"] == "authentication_challenge"
    assert auth["data"]["token"] == settings.mattermost_token
    # Each raw frame is yielded as json.loads(...).
    assert received == [{"event": "hello"}, {"event": "posted"}]
