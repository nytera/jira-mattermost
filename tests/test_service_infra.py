from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import replace
from typing import Any, cast
from zoneinfo import ZoneInfoNotFoundError

import httpx
import pytest
from fastapi.testclient import TestClient
from support import (
    POST_ID,
    _build_service,
    _capture_bot_logs,
    _extra_fields,
    _manual_post,
    make_alert,
)

from mm_jira_bot.colors import (
    OPS_ALERT_COLOR,
)
from mm_jira_bot.config import Settings, _csv_env, load_dotenv_file
from mm_jira_bot.domain import (
    ConfirmationResult,
    ReactionEvent,
)
from mm_jira_bot.logging import get_logger
from mm_jira_bot.mattermost import MattermostClient
from mm_jira_bot.ops import OpsLogHandler, OpsNotifier
from mm_jira_bot.repository import (
    create_database_engine,
    init_db,
)
from mm_jira_bot.retry import ApiError
from mm_jira_bot.service import parse_post_id_from_text
from mm_jira_bot.web import _redact_database_url, create_app, run_startup_preflight


def test_loads_russian_jira_field_name_with_spaces(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JIRA_VALID_INCIDENT_FIELD=Валидный инцидент\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("JIRA_VALID_INCIDENT_FIELD", raising=False)

    load_dotenv_file(env_file)

    assert os.environ["JIRA_VALID_INCIDENT_FIELD"] == "Валидный инцидент"


def test_settings_do_not_backfill_old_messages_by_default(tmp_path, monkeypatch):
    required_env = {
        "MATTERMOST_URL": "https://mattermost.example.com",
        "MATTERMOST_TOKEN": "mm-token",
        "MATTERMOST_ALERT_CHANNEL_ID": "alerts-channel",
        "MATTERMOST_INCIDENT_CHANNEL_ID": "incidents-channel",
        "MATTERMOST_BOT_USER_ID": "bot-user",
        "JIRA_BASE_URL": "https://jira.example.com",
        "JIRA_API_TOKEN": "jira-token",
        "JIRA_PROJECT_KEY": "OPS",
        "JIRA_ISSUE_TYPE": "Incident",
        "JIRA_VALID_INCIDENT_FIELD": "Валидность",
        "JIRA_SOURCE_FIELD": "Источник",
        "JIRA_IS_CRIT_ALERT_FIELD": "Был ли крит алерт?",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'bot.db'}",
    }
    for key, value in required_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BACKFILL_RECENT_POSTS_LIMIT", raising=False)
    monkeypatch.delenv("ENABLE_BACKFILL_ON_STARTUP", raising=False)

    loaded_settings = Settings.from_env(tmp_path / "missing.env")

    assert loaded_settings.backfill_recent_posts_limit == 0
    assert loaded_settings.enable_backfill_on_startup is False


def test_settings_loads_read_only_mode(tmp_path, monkeypatch):
    required_env = {
        "MATTERMOST_URL": "https://mattermost.example.com",
        "MATTERMOST_TOKEN": "mm-token",
        "MATTERMOST_ALERT_CHANNEL_ID": "alerts-channel",
        "MATTERMOST_INCIDENT_CHANNEL_ID": "incidents-channel",
        "MATTERMOST_BOT_USER_ID": "bot-user",
        "JIRA_BASE_URL": "https://jira.example.com",
        "JIRA_API_TOKEN": "jira-token",
        "JIRA_PROJECT_KEY": "OPS",
        "JIRA_ISSUE_TYPE": "Incident",
        "JIRA_VALID_INCIDENT_FIELD": "Валидность",
        "JIRA_SOURCE_FIELD": "Источник",
        "JIRA_IS_CRIT_ALERT_FIELD": "Был ли крит алерт?",
        "READ_ONLY_MODE": "true",
        "MATTERMOST_AUDIT_CHANNEL_ID": "audit-channel",
        "MATTERMOST_TEST_ALERT_CHANNEL_ID": "test-alert",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'bot.db'}",
    }
    for key, value in required_env.items():
        monkeypatch.setenv(key, value)

    loaded_settings = Settings.from_env(tmp_path / "missing.env")

    assert loaded_settings.read_only_mode is True
    assert loaded_settings.mattermost_audit_channel_id == "audit-channel"
    assert loaded_settings.mattermost_test_alert_channel_id == "test-alert"


def test_test_channels_route_to_live_path_only_in_read_only(settings):
    """A leftover test-channel env var must never route real traffic into the live
    alert/incident path in a normal (non-read-only) deployment — the test channels
    are a shadow-only concept, folded in only under read_only_mode."""
    with_test = replace(
        settings,
        mattermost_test_alert_channel_id="test-alert",
        mattermost_test_incident_channel_id="test-incident",
    )

    prod = _build_service(replace(with_test, read_only_mode=False))
    # Real channels are always recognised.
    assert prod._is_alert_channel("alerts-channel")
    assert prod._is_incident_channel("incidents-channel")
    # Test channels are NOT live in prod mode (would otherwise create real Jira
    # issues / Mattermost writes from test traffic).
    assert not prod._is_alert_channel("test-alert")
    assert not prod._is_incident_channel("test-incident")

    shadow = _build_service(replace(with_test, read_only_mode=True))
    assert shadow._is_alert_channel("test-alert")
    assert shadow._is_incident_channel("test-incident")


def test_settings_bot_user_id_optional(tmp_path, monkeypatch):
    """MATTERMOST_BOT_USER_ID is optional: unset ⇒ empty string (resolved from the
    token at startup), not a startup failure."""
    _set_env(monkeypatch, _full_valid_env())
    monkeypatch.delenv("MATTERMOST_BOT_USER_ID", raising=False)

    loaded = Settings.from_env(tmp_path / "missing.env")

    assert loaded.mattermost_bot_user_id == ""


async def test_resolve_bot_user_id_from_token_when_unset(settings):
    service = _build_service(replace(settings, mattermost_bot_user_id=""))
    service.mattermost.bot_user_id_from_api = "resolved-bot"

    await service.resolve_bot_user_id()

    # Pushed into both the service settings (hot path) and the client (add_reaction).
    assert service.settings.mattermost_bot_user_id == "resolved-bot"
    assert service.mattermost.adopted_bot_user_id == "resolved-bot"


async def test_resolve_bot_user_id_keeps_configured(settings):
    service = _build_service(settings)  # conftest sets bot-user
    service.mattermost.bot_user_id_from_api = "should-not-be-used"

    await service.resolve_bot_user_id()

    assert service.settings.mattermost_bot_user_id == "bot-user"
    assert service.mattermost.adopted_bot_user_id is None


async def test_resolve_bot_user_id_raises_on_empty_api(settings):
    service = _build_service(replace(settings, mattermost_bot_user_id=""))
    service.mattermost.bot_user_id_from_api = ""

    with pytest.raises(RuntimeError, match="resolve bot user id"):
        await service.resolve_bot_user_id()


def test_settings_load_llm_prompt_overrides(tmp_path, monkeypatch):
    required_env = {
        "MATTERMOST_URL": "https://mattermost.example.com",
        "MATTERMOST_TOKEN": "mm-token",
        "MATTERMOST_ALERT_CHANNEL_ID": "alerts-channel",
        "MATTERMOST_INCIDENT_CHANNEL_ID": "incidents-channel",
        "MATTERMOST_BOT_USER_ID": "bot-user",
        "JIRA_BASE_URL": "https://jira.example.com",
        "JIRA_API_TOKEN": "jira-token",
        "JIRA_PROJECT_KEY": "OPS",
        "JIRA_ISSUE_TYPE": "Incident",
        "JIRA_VALID_INCIDENT_FIELD": "Валидность",
        "JIRA_SOURCE_FIELD": "Источник",
        "JIRA_IS_CRIT_ALERT_FIELD": "Был ли крит алерт?",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'bot.db'}",
    }
    for key, value in required_env.items():
        monkeypatch.setenv(key, value)

    # Unset → defaults stay (None means "use built-in template").
    assert Settings.from_env(tmp_path / "missing.env").llm_summary_prompt is None

    # Inline var is used; the *_FILE variant takes precedence and its file
    # contents (including a multi-line body) become the value.
    prompt_file = tmp_path / "summary.txt"
    prompt_file.write_text("Саммари из файла\nвторая строка {transcript}", encoding="utf-8")
    monkeypatch.setenv("LLM_SUMMARY_PROMPT", "инлайн который проиграет")
    monkeypatch.setenv("LLM_SUMMARY_PROMPT_FILE", str(prompt_file))

    loaded = Settings.from_env(tmp_path / "missing.env")
    assert loaded.llm_summary_prompt == "Саммари из файла\nвторая строка {transcript}"


def test_init_db_adds_alert_title_column_to_existing_schema(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE alert_tickets (
                id INTEGER PRIMARY KEY,
                mattermost_post_id VARCHAR(64) NOT NULL UNIQUE,
                mattermost_channel_id VARCHAR(64) NOT NULL,
                mattermost_channel_name VARCHAR(255),
                mattermost_message_url TEXT NOT NULL,
                mattermost_message_text TEXT NOT NULL,
                mattermost_author_id VARCHAR(64) NOT NULL,
                mattermost_message_created_at TIMESTAMP WITH TIME ZONE,
                jira_issue_key VARCHAR(64) UNIQUE,
                jira_issue_url TEXT,
                valid_incident BOOLEAN NOT NULL DEFAULT FALSE,
                incident_post_id VARCHAR(64) UNIQUE,
                incident_message_url TEXT,
                confirmed_by_user_id VARCHAR(64),
                confirmed_at TIMESTAMP WITH TIME ZONE,
                creation_status VARCHAR(32) NOT NULL DEFAULT 'pending_jira',
                confirmation_status VARCHAR(32) NOT NULL DEFAULT 'none',
                pending_confirmation_by_user_id VARCHAR(64),
                pending_confirmation_at TIMESTAMP WITH TIME ZONE,
                jira_confirmation_comment_added BOOLEAN NOT NULL DEFAULT FALSE,
                validity_label VARCHAR(64),
                last_error TEXT,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    init_db(engine)

    with engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(alert_tickets)")}
    assert "mattermost_alert_title" in columns


def test_settings_reports_all_valid_incident_field_env_names(tmp_path, monkeypatch):
    required_env = {
        "MATTERMOST_URL": "https://mattermost.example.com",
        "MATTERMOST_TOKEN": "mm-token",
        "MATTERMOST_ALERT_CHANNEL_ID": "alerts-channel",
        "MATTERMOST_INCIDENT_CHANNEL_ID": "incidents-channel",
        "MATTERMOST_BOT_USER_ID": "bot-user",
        "JIRA_BASE_URL": "https://jira.example.com",
        "JIRA_API_TOKEN": "jira-token",
        "JIRA_PROJECT_KEY": "OPS",
        "JIRA_ISSUE_TYPE": "Incident",
        "JIRA_SOURCE_FIELD": "Источник",
        "JIRA_IS_CRIT_ALERT_FIELD": "Был ли крит алерт?",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'bot.db'}",
    }
    for key, value in required_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("JIRA_VALID_INCIDENT_FIELD", raising=False)
    monkeypatch.delenv("JIRA_VALID_INCIDENT_FIELD_NAME", raising=False)
    monkeypatch.delenv("JIRA_VALID_INCIDENT_FIELD_ID", raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        Settings.from_env(tmp_path / "missing.env")

    assert str(exc_info.value) == (
        "Missing required environment variable: "
        "JIRA_VALID_INCIDENT_FIELD or "
        "JIRA_VALID_INCIDENT_FIELD_NAME or "
        "JIRA_VALID_INCIDENT_FIELD_ID"
    )


@pytest.mark.asyncio
async def test_mattermost_preflight_checks_bot_user_and_channels(settings):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api/v4/users/me":
            return httpx.Response(
                200,
                json={"id": "bot-user", "username": "incident-bot"},
            )
        if request.url.path == "/api/v4/channels/alerts-channel":
            return httpx.Response(200, json={"display_name": "Alerts"})
        if request.url.path == "/api/v4/channels/incidents-channel":
            return httpx.Response(200, json={"name": "incidents"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = MattermostClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.mattermost_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        result = await client.preflight_check()
    finally:
        await client.aclose()

    assert result["bot_user_id"] == "bot-user"
    assert result["bot_username"] == "incident-bot"
    assert result["bot_user_id_matches_config"] is True
    assert result["alert_channel_name"] == "Alerts"
    assert result["incident_channel_name"] == "incidents"
    assert requests == [
        "/api/v4/users/me",
        "/api/v4/channels/alerts-channel",
        "/api/v4/channels/incidents-channel",
    ]


def test_extracts_post_id_from_mattermost_permalink():
    assert parse_post_id_from_text(f"https://mattermost.example.com/team/pl/{POST_ID}") == POST_ID
    assert (
        parse_post_id_from_text(f"https://mattermost.example.com/_redirect/pl/{POST_ID}") == POST_ID
    )


@pytest.mark.asyncio
async def test_startup_preflight_logs_failures_without_raising(service, caplog):
    class FailingPreflightClient:
        async def preflight_check(self):
            raise RuntimeError("preflight boom")

    class PassingPreflightClient:
        async def preflight_check(self):
            return {"dependency_ok": True}

    service.mattermost = FailingPreflightClient()
    service.jira = PassingPreflightClient()
    service.llm = PassingPreflightClient()

    with caplog.at_level(logging.INFO):
        await run_startup_preflight(service)

    messages = [record.message for record in caplog.records]
    assert "startup.preflight.check_failed" in messages
    assert "startup.preflight.completed" in messages


def test_http_error_boundary_returns_500_and_logs(service):
    """The app-global ``@app.middleware('http')`` 500 boundary: an unhandled
    error in any route becomes a clean JSON 500 plus a structured
    ``http.request.failed`` event carrying the request path. Driven through a
    throwaway route registered just for this test."""
    app = create_app(service.settings, service=service)

    @app.get("/_boom")
    async def _boom():
        raise RuntimeError("kaboom")

    records: list[logging.LogRecord] = []
    logger, handler = _capture_bot_logs(records)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/_boom")
    finally:
        logger.removeHandler(handler)

    assert response.status_code == 500
    assert response.json() == {"error": "Internal server error."}
    failures = [r for r in records if r.msg == "http.request.failed"]
    assert failures
    assert failures[0].exc_info is not None
    assert _extra_fields(failures[0])["error_type"] == "RuntimeError"
    assert _extra_fields(failures[0])["path"] == "/_boom"


def _authorized_service(settings, usernames, resolvable):
    service = _build_service(replace(settings, mattermost_authorized_usernames=usernames))
    service.mattermost.username_to_id = dict(resolvable)
    return service


@pytest.mark.asyncio
async def test_authorization_disabled_when_no_usernames_configured(service):
    await service.resolve_authorized_users()

    assert service._authorization_enforced is False
    assert service.mattermost.usernames_lookups == []
    # An arbitrary user can still act (backward compatible allow-all).
    assert service._is_authorized("anyone") is True


@pytest.mark.asyncio
async def test_authorized_user_reaction_is_honored(settings):
    service = _authorized_service(settings, ("alice",), {"alice": "u-alice"})
    await service.resolve_authorized_users()
    assert service._authorization_enforced is True

    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="u-alice", emoji_name="incident", create_at=1)
    )

    assert isinstance(result, ConfirmationResult)
    assert result.status != "ignored"
    assert service.jira.valid_updates == [("OPS-1", True)]


@pytest.mark.asyncio
async def test_unauthorized_user_reaction_is_ignored(settings):
    service = _authorized_service(settings, ("alice",), {"alice": "u-alice"})
    await service.resolve_authorized_users()

    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="u-bob", emoji_name="incident", create_at=1)
    )

    assert isinstance(result, ConfirmationResult)
    assert result.status == "ignored"
    assert service.jira.valid_updates == []


@pytest.mark.asyncio
async def test_partial_resolution_keeps_resolved_and_drops_typo(settings):
    service = _authorized_service(settings, ("alice", "typo"), {"alice": "u-alice"})
    await service.resolve_authorized_users()

    assert service._authorization_enforced is True
    assert service._authorized_user_ids == frozenset({"u-alice"})
    assert service._is_authorized("u-alice") is True
    assert service._is_authorized("u-typo") is False


@pytest.mark.asyncio
async def test_total_resolution_failure_is_fail_open(settings):
    service = _authorized_service(settings, ("alice",), {"alice": "u-alice"})

    async def boom(_usernames):
        raise ApiError("mattermost down", retryable=True)

    service.mattermost.get_user_ids_by_usernames = boom
    await service.resolve_authorized_users()

    # Fail-open: gate disabled, everyone acts (network isolation is the boundary).
    assert service._authorization_enforced is False
    assert service._is_authorized("anyone") is True


@pytest.mark.asyncio
async def test_no_usernames_resolved_is_fail_open(settings):
    # Every configured login is a typo -> Mattermost returns {} (no ApiError).
    service = _authorized_service(settings, ("typo1", "typo2"), {})
    await service.resolve_authorized_users()

    # Must fail open (act on everyone), not lock the whole team out.
    assert service._authorization_enforced is False
    assert service._is_authorized("anyone") is True


@pytest.mark.asyncio
async def test_websocket_event_routes_incident_post_to_manual_handler(settings):
    service = _build_service(settings)
    post = _manual_post()
    service.mattermost.posts[post.id] = post

    await service.handle_websocket_event(
        {
            "event": "posted",
            "data": {
                "post": json.dumps(
                    {
                        "id": post.id,
                        "channel_id": "incidents-channel",
                        "user_id": "human",
                        "message": post.message,
                        "create_at": post.create_at,
                        "root_id": "",
                    }
                ),
                "channel_name": "incidents",
            },
        }
    )

    # Emoji-only mode has no create_task card; the manual handler instead opens
    # the incident thread (cheat-sheet reply) and records the ticket row.
    def _attachment_text(created: dict) -> str:
        attachments = (created["props"] or {}).get("attachments") or [{}]
        return attachments[0].get("text", "")

    help_replies = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == post.id and "Памятка дежурному" in _attachment_text(c)
    ]
    assert len(help_replies) == 1
    assert service.repository.get_by_incident_post_id(post.id) is not None


# --- Ops alerts channel ------------------------------------------------------


def _error_record(event: str, level: int = logging.ERROR, **fields) -> logging.LogRecord:
    record = logging.LogRecord("mm_jira_bot.test", level, __file__, 1, event, None, None)
    cast(Any, record).extra_fields = {"event": event, **fields}
    return record


@pytest.mark.asyncio
async def test_ops_handler_enqueues_once_within_cooldown():
    handler = OpsLogHandler(cooldown_seconds=300)
    queue: asyncio.Queue = asyncio.Queue()
    handler.activate(queue, asyncio.get_running_loop())
    handler.emit(_error_record("ops.test.evt", error="x"))
    handler.emit(_error_record("ops.test.evt", error="x"))  # cooldown suppresses repeat
    await asyncio.sleep(0)  # let call_soon_threadsafe run
    assert queue.qsize() == 1
    payload = queue.get_nowait()
    assert payload["event"] == "ops.test.evt"
    assert payload["fields"]["error"] == "x"


@pytest.mark.asyncio
async def test_ops_notifier_posts_boxed_alert(service, settings):
    notifier = OpsNotifier(
        service.mattermost, replace(settings, mattermost_ops_channel_id="ops-channel")
    )
    await notifier._post({"event": "pending_work.failed", "fields": {"error": "boom"}})
    posted = service.mattermost.created_posts
    assert len(posted) == 1
    attachment = posted[0]["props"]["attachments"][0]
    assert posted[0]["channel_id"] == "ops-channel"
    assert attachment["color"] == OPS_ALERT_COLOR
    assert "pending_work.failed" in attachment["text"]
    assert "boom" in attachment["text"]


@pytest.mark.asyncio
async def test_ops_notifier_post_is_best_effort(service, settings):
    async def boom(**_kwargs):
        raise ApiError("mattermost down")

    service.mattermost.create_post = boom
    notifier = OpsNotifier(
        service.mattermost, replace(settings, mattermost_ops_channel_id="ops-channel")
    )
    # Must not raise even though the underlying post fails.
    await notifier._post({"event": "boom", "fields": {}})


@pytest.mark.asyncio
async def test_ops_notifier_buffers_startup_errors(service, settings):
    """activate() runs before preflight, so an early ERROR is buffered (not
    dropped) and later posted when drain consumes it."""
    notifier = OpsNotifier(
        service.mattermost, replace(settings, mattermost_ops_channel_id="ops-channel")
    )
    notifier.install()
    notifier.activate()
    try:
        get_logger("mm_jira_bot.service").error("startup.preflight.check_failed", dependency="jira")
        await asyncio.sleep(0)  # let call_soon_threadsafe enqueue
        assert notifier._queue is not None
        assert notifier._queue.qsize() == 1
        await notifier._post(notifier._queue.get_nowait())
        assert service.mattermost.created_posts[0]["channel_id"] == "ops-channel"
    finally:
        logging.getLogger("mm_jira_bot").removeHandler(notifier._handler)


# --- Allowlist: groups, separators, refresh ---------------------------------


def test_csv_env_splits_on_comma_and_semicolon(monkeypatch):
    monkeypatch.setenv("ALLOW", "alice, bob;@carol ; ,sre-team")
    assert _csv_env("ALLOW") == ("alice", "bob", "carol", "sre-team")


@pytest.mark.asyncio
async def test_group_members_resolve_into_allowlist(settings):
    service = _authorized_service(settings, ("sre-team",), {})
    service.mattermost.group_name_to_id = {"sre-team": "g-sre"}
    service.mattermost.group_members = {"g-sre": {"u-alice", "u-bob"}}

    await service.resolve_authorized_users()

    assert service._authorization_enforced is True
    assert service._authorized_user_ids == frozenset({"u-alice", "u-bob"})
    assert service._is_authorized("u-bob") is True
    assert service._is_authorized("u-stranger") is False


@pytest.mark.asyncio
async def test_mixed_logins_and_groups_resolve(settings):
    service = _authorized_service(settings, ("alice", "sre-team"), {"alice": "u-alice"})
    service.mattermost.group_name_to_id = {"sre-team": "g-sre"}
    service.mattermost.group_members = {"g-sre": {"u-bob"}}

    await service.resolve_authorized_users()

    assert service._authorized_user_ids == frozenset({"u-alice", "u-bob"})
    # The group lookup only got the names that were not resolved as logins.
    assert service.mattermost.group_lookups == [["sre-team"]]


@pytest.mark.asyncio
async def test_refresh_picks_up_new_group_member(settings):
    service = _authorized_service(settings, ("sre-team",), {})
    service.mattermost.group_name_to_id = {"sre-team": "g-sre"}
    service.mattermost.group_members = {"g-sre": {"u-alice"}}
    await service.resolve_authorized_users()
    assert service._is_authorized("u-bob") is False

    # Someone is added to the group; the next refresh picks them up.
    service.mattermost.group_members["g-sre"].add("u-bob")
    await service.resolve_authorized_users()
    assert service._is_authorized("u-bob") is True


@pytest.mark.asyncio
async def test_refresh_keeps_last_good_on_api_error(settings):
    service = _authorized_service(settings, ("alice",), {"alice": "u-alice"})
    await service.resolve_authorized_users()
    assert service._is_authorized("u-alice") is True

    async def boom(_usernames):
        raise ApiError("mattermost down", retryable=True)

    service.mattermost.get_user_ids_by_usernames = boom
    await service.resolve_authorized_users()

    # A transient refresh failure must not clobber a working allowlist.
    assert service._authorization_enforced is True
    assert service._authorized_user_ids == frozenset({"u-alice"})


@pytest.mark.asyncio
async def test_group_lookup_failure_keeps_login_allowlist(settings):
    service = _authorized_service(settings, ("alice", "sre-team"), {"alice": "u-alice"})

    async def boom(_names):
        raise ApiError("groups need a license", retryable=False)

    service.mattermost.get_group_ids_by_names = boom
    await service.resolve_authorized_users()

    # Group failure (e.g. missing license) must not brick the login allowlist.
    assert service._authorization_enforced is True
    assert service._authorized_user_ids == frozenset({"u-alice"})


# --- Config: required vars, numeric knobs, tz, prompt file, dotenv -----------


def _full_valid_env() -> dict[str, str]:
    """A complete env that makes ``Settings.from_env`` succeed."""
    return {
        "MATTERMOST_URL": "https://mattermost.example.com",
        "MATTERMOST_TOKEN": "mm-token",
        "MATTERMOST_ALERT_CHANNEL_ID": "alerts-channel",
        "MATTERMOST_INCIDENT_CHANNEL_ID": "incidents-channel",
        "MATTERMOST_BOT_USER_ID": "bot-user",
        "JIRA_BASE_URL": "https://jira.example.com",
        "JIRA_API_TOKEN": "jira-token",
        "JIRA_PROJECT_KEY": "OPS",
        "JIRA_ISSUE_TYPE": "Incident",
        "JIRA_VALID_INCIDENT_FIELD": "Валидность",
        "JIRA_SOURCE_FIELD": "Источник",
        "JIRA_IS_CRIT_ALERT_FIELD": "Был ли крит алерт?",
        "DATABASE_URL": "sqlite:///./bot.db",
    }


def _set_env(monkeypatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize(
    "missing",
    [
        "MATTERMOST_URL",
        "MATTERMOST_TOKEN",
        "DATABASE_URL",
        "JIRA_BASE_URL",
        "JIRA_PROJECT_KEY",
        "JIRA_ISSUE_TYPE",
        "JIRA_SOURCE_FIELD",
        "JIRA_IS_CRIT_ALERT_FIELD",
    ],
)
def test_missing_single_required_var_raises_runtime_error(tmp_path, monkeypatch, missing):
    env = _full_valid_env()
    _set_env(monkeypatch, env)
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        Settings.from_env(tmp_path / "missing.env")

    assert str(exc_info.value) == f"Missing required environment variable: {missing}"


@pytest.mark.parametrize(
    "var",
    [
        "API_RETRY_ATTEMPTS",
        "MATTERMOST_AUTHORIZED_REFRESH_SECONDS",
        "PENDING_WORK_INTERVAL_SECONDS",
        "BACKFILL_RECENT_POSTS_LIMIT",
        "LLM_MAX_TOKENS",
        "LLM_THREAD_MAX_CHARS",
        "LLM_STREAM_EDIT_MIN_CHARS",
    ],
)
def test_int_env_rejects_non_numeric(tmp_path, monkeypatch, var):
    _set_env(monkeypatch, _full_valid_env())
    monkeypatch.setenv(var, "four")

    with pytest.raises(ValueError):
        Settings.from_env(tmp_path / "missing.env")


def test_invalid_timezone_raises_zoneinfo_not_found(tmp_path, monkeypatch):
    _set_env(monkeypatch, _full_valid_env())
    monkeypatch.setenv("INCIDENT_TIMEZONE", "Mars/Phobos")

    with pytest.raises(ZoneInfoNotFoundError):
        Settings.from_env(tmp_path / "missing.env")


def test_missing_summary_prompt_file_raises(tmp_path, monkeypatch):
    _set_env(monkeypatch, _full_valid_env())
    monkeypatch.setenv("LLM_SUMMARY_PROMPT_FILE", str(tmp_path / "does-not-exist.txt"))

    with pytest.raises(FileNotFoundError):
        Settings.from_env(tmp_path / "missing.env")


def test_load_dotenv_skips_comments_blank_and_no_equals(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n\n   \nNOEQUALSLINE\nDOTENV_REAL_KEY=real-value\n",
        encoding="utf-8",
    )
    for key in ("NOEQUALSLINE", "DOTENV_REAL_KEY", "a comment"):
        monkeypatch.delenv(key, raising=False)

    load_dotenv_file(env_file)

    assert os.environ["DOTENV_REAL_KEY"] == "real-value"
    assert "NOEQUALSLINE" not in os.environ


def test_load_dotenv_strips_symmetric_quotes_and_splits_on_first_equals(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'DOTENV_DQ="double quoted"\n'
        "DOTENV_SQ='single quoted'\n"
        "DOTENV_FIRST_EQ=a=b=c\n"
        'DOTENV_MISMATCH="only-leading\n',
        encoding="utf-8",
    )
    for key in ("DOTENV_DQ", "DOTENV_SQ", "DOTENV_FIRST_EQ", "DOTENV_MISMATCH"):
        monkeypatch.delenv(key, raising=False)

    load_dotenv_file(env_file)

    assert os.environ["DOTENV_DQ"] == "double quoted"
    assert os.environ["DOTENV_SQ"] == "single quoted"
    # Split on the first '=' only — the value keeps its own '=' characters.
    assert os.environ["DOTENV_FIRST_EQ"] == "a=b=c"
    # Asymmetric quoting is left untouched (only matching first/last char strips).
    assert os.environ["DOTENV_MISMATCH"] == '"only-leading'


def test_load_dotenv_never_overrides_existing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTENV_PRESET", "from-process")
    env_file = tmp_path / ".env"
    env_file.write_text("DOTENV_PRESET=from-file\n", encoding="utf-8")

    load_dotenv_file(env_file)

    assert os.environ["DOTENV_PRESET"] == "from-process"


# --- _redact_database_url ----------------------------------------------------


def test_redact_database_url_masks_password():
    assert _redact_database_url("postgresql://user:s3cret@db/bot") == "postgresql://user:***@db/bot"


def test_redact_database_url_no_password_unchanged():
    url = "sqlite:///./bot.db"
    assert _redact_database_url(url) == url


def test_redact_database_url_invalid_returns_placeholder():
    assert _redact_database_url("http://[::1") == "<invalid>"


@pytest.mark.asyncio
async def test_startup_configuration_log_carries_no_plaintext_password(service):
    service.settings = replace(service.settings, database_url="postgresql://user:s3cret@db/bot")
    records: list[logging.LogRecord] = []
    logger, handler = _capture_bot_logs(records)
    try:
        await run_startup_preflight(service)
    finally:
        logger.removeHandler(handler)

    config_logs = [r for r in records if r.msg == "startup.configuration"]
    assert config_logs
    redacted = _extra_fields(config_logs[0])["database_url"]
    assert redacted == "postgresql://user:***@db/bot"
    assert "s3cret" not in cast(str, redacted)


# --- /healthz ---------------------------------------------------------------


def test_healthz_returns_ok(service, settings):
    app = create_app(settings, service=service)
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


# --- Lifespan: backfill failure tolerated; loops launched & cancelled -------


def test_lifespan_continues_when_backfill_raises(service, settings):
    async def boom():
        raise RuntimeError("backfill kaboom")

    service.backfill_recent_alerts = boom
    app = create_app(
        replace(settings, enable_backfill_on_startup=True, enable_websocket=False),
        service=service,
    )
    records: list[logging.LogRecord] = []
    logger, handler = _capture_bot_logs(records)
    try:
        with TestClient(app) as client:
            response = client.get("/healthz")
            assert response.status_code == 200
            assert response.json() == {"ok": True}
    finally:
        logger.removeHandler(handler)

    assert "startup.backfill_failed" in [r.msg for r in records]


def test_lifespan_launches_and_cancels_background_loops(service, settings):
    app = create_app(
        replace(settings, enable_websocket=True, enable_backfill_on_startup=False),
        service=service,
    )
    with TestClient(app) as client:
        client.get("/healthz")
        tasks = app.state.background_tasks
        # websocket_loop + pending_work_loop launched (no ops channel, no allowlist).
        assert len(tasks) == 2
        assert all(not task.done() for task in tasks)

    # After shutdown the lifespan cancels every background task.
    assert all(task.cancelled() or task.done() for task in tasks)
