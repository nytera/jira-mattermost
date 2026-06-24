from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import replace
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from support import (
    POST_ID,
    _build_service,
    _capture_bot_logs,
    _extra_fields,
    _incident_service,
    _manual_post,
    make_alert,
)

from mm_jira_bot.actions import (
    OPS_ALERT_COLOR,
)
from mm_jira_bot.config import Settings, _csv_env, load_dotenv_file
from mm_jira_bot.domain import (
    ConfirmationResult,
    ReactionEvent,
)
from mm_jira_bot.logging import get_logger
from mm_jira_bot.mattermost import MattermostClient
from mm_jira_bot.metrics import TicketStatsCollector, errors_total
from mm_jira_bot.ops import OpsLogHandler, OpsNotifier
from mm_jira_bot.repository import (
    create_database_engine,
    init_db,
)
from mm_jira_bot.retry import ApiError
from mm_jira_bot.service import parse_post_id_from_text
from mm_jira_bot.web import create_app, run_startup_preflight


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


def test_settings_loads_jira_create_stub_mode(tmp_path, monkeypatch):
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
        "JIRA_CREATE_ENABLED": "false",
        "JIRA_STUB_ISSUE_KEY": "ADSDEV-12024",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'bot.db'}",
    }
    for key, value in required_env.items():
        monkeypatch.setenv(key, value)

    loaded_settings = Settings.from_env(tmp_path / "missing.env")

    assert loaded_settings.jira_create_enabled is False
    assert loaded_settings.jira_stub_issue_key == "ADSDEV-12024"


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
    assert Settings.from_env(tmp_path / "missing.env").llm_postmortem_prompt is None

    # Inline var is used; the *_FILE variant takes precedence and its file
    # contents (including a multi-line body) become the value.
    prompt_file = tmp_path / "pm.txt"
    prompt_file.write_text("ПМ из файла\nвторая строка {transcript}", encoding="utf-8")
    monkeypatch.setenv("LLM_POSTMORTEM_PROMPT", "инлайн который проиграет")
    monkeypatch.setenv("LLM_POSTMORTEM_PROMPT_FILE", str(prompt_file))
    monkeypatch.setenv("LLM_SUMMARY_PROMPT", "саммари инлайн")

    loaded = Settings.from_env(tmp_path / "missing.env")
    assert loaded.llm_postmortem_prompt == "ПМ из файла\nвторая строка {transcript}"
    assert loaded.llm_summary_prompt == "саммари инлайн"


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


@pytest.mark.asyncio
async def test_mattermost_client_opens_dialog(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append({"path": request.url.path, "json": json.loads(request.content)})
        return httpx.Response(200, json={})

    client = MattermostClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.mattermost_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        await client.open_dialog(
            trigger_id="trigger-1",
            url="https://bot.example.com/mattermost/dialogs/feedback",
            dialog={"title": "Обратная связь"},
        )
    finally:
        await client.aclose()

    assert requests == [
        {
            "path": "/api/v4/actions/dialogs/open",
            "json": {
                "trigger_id": "trigger-1",
                "url": "https://bot.example.com/mattermost/dialogs/feedback",
                "dialog": {"title": "Обратная связь"},
            },
        }
    ]


def test_extracts_post_id_from_mattermost_permalink():
    assert parse_post_id_from_text(f"https://mattermost.example.com/team/pl/{POST_ID}") == POST_ID
    assert (
        parse_post_id_from_text(f"https://mattermost.example.com/_redirect/pl/{POST_ID}") == POST_ID
    )


def test_invalid_slash_link_returns_ephemeral_response(service, settings):
    app = create_app(settings, service=service)
    with TestClient(app) as client:
        response = client.post(
            "/mattermost/slash/incident",
            data={"token": "slash-token", "user_id": "validator", "text": "not-a-link"},
        )

    assert response.status_code == 200
    assert "Invalid link" in response.json()["text"]


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


def test_slash_command_handles_missing_jira_mapping(service, settings):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    app = create_app(settings, service=service)
    with TestClient(app) as client:
        response = client.post(
            "/mattermost/slash/incident",
            data={
                "token": "slash-token",
                "user_id": "validator",
                "text": f"https://mattermost.example.com/_redirect/pl/{post.id}",
            },
        )

    assert response.status_code == 200
    assert "Incident confirmed" in response.json()["text"]
    assert len(service.jira.created_payloads) == 1


def test_http_error_boundary_returns_500_and_logs(service, settings):
    async def boom(**kwargs):
        raise RuntimeError("kaboom")

    service.handle_feedback_dialog_submission = boom
    app = create_app(settings, service=service)
    records: list[logging.LogRecord] = []
    logger, handler = _capture_bot_logs(records)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/mattermost/dialogs/feedback",
                json={"user_id": "u", "state": "s", "submission": {}},
            )
    finally:
        logger.removeHandler(handler)

    assert response.status_code == 500
    assert response.json() == {"error": "Internal server error."}
    failures = [r for r in records if r.msg == "http.request.failed"]
    assert failures
    assert failures[0].exc_info is not None
    assert _extra_fields(failures[0])["error_type"] == "RuntimeError"
    assert _extra_fields(failures[0])["path"] == "/mattermost/dialogs/feedback"


def test_alert_action_rejects_malformed_json(service, settings):
    app = create_app(settings, service=service)
    records: list[logging.LogRecord] = []
    logger, handler = _capture_bot_logs(records)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/mattermost/actions/alert",
                content="{not json",
                headers={"content-type": "application/json"},
            )
    finally:
        logger.removeHandler(handler)

    assert response.status_code == 400
    assert "http.request.bad_json" in [r.msg for r in records]


def test_ticket_collector_logs_on_repository_failure():
    class FailingRepo:
        def debug_summary(self):
            raise RuntimeError("db down")

    collector = TicketStatsCollector(FailingRepo())
    records: list[logging.LogRecord] = []
    logger, handler = _capture_bot_logs(records)
    try:
        result = list(collector.collect())
    finally:
        logger.removeHandler(handler)

    assert result == []
    failures = [r for r in records if r.msg == "metrics.collect_failed"]
    assert failures
    assert failures[0].exc_info is not None
    assert _extra_fields(failures[0])["error_type"] == "RuntimeError"


def test_alert_action_endpoint_dispatches(service, settings):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    app = create_app(settings, service=service)
    with TestClient(app) as client:
        response = client.post(
            "/mattermost/actions/alert",
            json={
                "user_id": "clicker",
                "context": {
                    "action": "validity",
                    "alert_post_id": post.id,
                    "selected_option": "false",
                },
            },
        )

    assert response.status_code == 200
    assert "Ложный" in response.json()["ephemeral_text"]
    assert service.jira.validity_updates == [("OPS-1", "Ложный")]


def test_feedback_dialog_endpoint_stores_feedback(service, settings):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    service.mattermost.display_names["clicker"] = "@clicker"
    service.repository.create_or_get_alert(
        post,
        message_url=service.mattermost.permalink(post.id),
        channel_name="alerts",
    )
    app = create_app(settings, service=service)
    with TestClient(app) as client:
        response = client.post(
            "/mattermost/dialogs/feedback",
            json={
                "user_id": "clicker",
                "state": json.dumps({"alert_post_id": post.id}),
                "submission": {"feedback": "Хорошая форма"},
            },
        )

    assert response.status_code == 200
    assert response.json() == {}
    feedback = service.repository.list_feedback(post.id)
    assert len(feedback) == 1
    assert feedback[0].message == "Хорошая форма"


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
async def test_unauthorized_action_is_blocked(settings):
    service = _authorized_service(settings, ("alice",), {"alice": "u-alice"})
    await service.resolve_authorized_users()

    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    service.jira.valid_updates.clear()

    result = await service.handle_alert_action(
        action="incident",
        alert_post_id=post.id,
        user_id="u-bob",
        user_name="bob",
        channel_id="alert-channel",
    )

    assert result.message == ""
    assert service.jira.valid_updates == []
    # A visible thread reply with the denial notice must be posted.
    notice_replies = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == post.id and "@bob" in (c.get("message") or "")
    ]
    assert len(notice_replies) == 1
    att_text = notice_replies[0]["props"]["attachments"][0]["text"]
    assert "авторизованным" in att_text
    assert "@alice" in att_text


@pytest.mark.asyncio
async def test_feedback_action_allowed_for_unauthorized_user(settings):
    service = _build_service(
        replace(
            settings,
            mattermost_authorized_usernames=("alice",),
            service_public_url="https://bot.example.com",
        )
    )
    service.mattermost.username_to_id = {"alice": "u-alice"}
    await service.resolve_authorized_users()

    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.handle_alert_action(
        action="feedback",
        alert_post_id=post.id,
        user_id="u-bob",
        trigger_id="trigger-1",
    )

    assert "Открыта форма" in result.message
    assert len(service.mattermost.opened_dialogs) == 1


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


def test_endpoint_routes_incident_create_task(settings):
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=True,
        )
    )
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    service.repository.create_or_get_incident_thread(
        post, message_url=service.mattermost.permalink(post.id), channel_name="incidents"
    )

    app = create_app(settings, service=service)
    with TestClient(app) as client:
        response = client.post(
            "/mattermost/actions/alert",
            json={
                "user_id": "opener",
                "context": {
                    "action": "create_task",
                    "source": "incident",
                    "incident_post_id": post.id,
                },
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["update"]["props"]["attachments"][0]["actions"]
    ticket = service.repository.get_by_incident_post_id(post.id)
    assert ticket is not None
    assert ticket.jira_issue_key == "OPS-1"


@pytest.mark.asyncio
async def test_websocket_event_routes_incident_post_to_manual_handler(settings):
    service = _incident_service(settings)
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

    cards = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == post.id and (c["props"] or {}).get("attachments", [{}])[0].get("actions")
    ]
    assert len(cards) == 1
    assert cards[0]["props"]["attachments"][0]["actions"][0]["id"] == "create_task"


# --- Ops alerts channel & Prometheus metrics ---------------------------------


def _error_record(event: str, level: int = logging.ERROR, **fields) -> logging.LogRecord:
    record = logging.LogRecord("mm_jira_bot.test", level, __file__, 1, event, None, None)
    cast(Any, record).extra_fields = {"event": event, **fields}
    return record


def _errors_counter(event: str) -> float:
    return errors_total.labels(event=event)._value.get()


def test_ops_handler_counts_errors_and_skips_non_errors():
    handler = OpsLogHandler(cooldown_seconds=300)
    before = _errors_counter("ops.test.boom")
    handler.emit(_error_record("ops.test.boom"))
    handler.emit(_error_record("ops.test.boom"))
    handler.emit(_error_record("ops.test.warn", level=logging.WARNING))
    assert _errors_counter("ops.test.boom") - before == 2
    # A non-error record is ignored entirely (no counter for it).
    assert _errors_counter("ops.test.warn") == 0


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


def test_metrics_endpoint_exposes_series(service, settings):
    app = create_app(settings, service=service)
    with TestClient(app) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "bot_http_requests_total" in body
    assert "bot_tickets_total" in body


def test_metrics_endpoint_absent_when_disabled(service, settings):
    app = create_app(replace(settings, metrics_enabled=False), service=service)
    with TestClient(app) as client:
        response = client.get("/metrics")
    assert response.status_code == 404


# --- Runtime-editable prompt settings ---------------------------------------


def test_repository_setting_crud(service):
    repo = service.repository
    assert repo.get_setting("llm_summary_prompt") is None
    repo.set_setting("llm_summary_prompt", "custom")
    assert repo.get_setting("llm_summary_prompt") == "custom"
    repo.set_setting("llm_summary_prompt", "custom2")  # upsert overwrites
    assert repo.get_setting("llm_summary_prompt") == "custom2"
    repo.delete_setting("llm_summary_prompt")
    assert repo.get_setting("llm_summary_prompt") is None


def test_resolve_prompt_template_precedence(settings):
    service = _build_service(replace(settings, llm_summary_prompt="env-template"))
    # env override applies when there is no DB override
    assert service._resolve_prompt_template("llm_summary_prompt") == "env-template"
    # DB override (debug-panel edit) beats env
    service.repository.set_setting("llm_summary_prompt", "db-template")
    assert service._resolve_prompt_template("llm_summary_prompt") == "db-template"
    # reset → falls back to env again
    service.repository.delete_setting("llm_summary_prompt")
    assert service._resolve_prompt_template("llm_summary_prompt") == "env-template"
    # neither env nor DB → None (builder uses the built-in default)
    assert service._resolve_prompt_template("llm_postmortem_prompt") is None


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
