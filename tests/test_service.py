from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

import mm_jira_bot.jira as jira_module
import mm_jira_bot.jira_payload as jira_payload_module
from mm_jira_bot.config import Settings, load_dotenv_file
from mm_jira_bot.domain import (
    JiraIssue,
    MattermostPost,
    ReactionEvent,
    datetime_from_mattermost_ms,
)
from mm_jira_bot.formatting import format_incident_message
from mm_jira_bot.jira_payload import build_jira_issue_payload
from mm_jira_bot.llm import PostmortemLlmClient
from mm_jira_bot.mattermost import MattermostClient
from mm_jira_bot.postmortem import extract_postmortem_summary
from mm_jira_bot.repository import (
    AlertTicketRepository,
    create_database_engine,
    create_session_factory,
    init_db,
)
from mm_jira_bot.retry import ApiError
from mm_jira_bot.service import IncidentBotService, parse_post_id_from_text
from mm_jira_bot.web import create_app, run_startup_preflight

POST_ID = "abcdefghijklmnopqrstuvwx01"


class FakeMattermostClient:
    def __init__(self) -> None:
        self.posts: dict[str, MattermostPost] = {}
        self.created_posts: list[dict] = []
        self.opened_dialogs: list[dict] = []
        self.updated_posts: list[dict] = []
        self.display_names: dict[str, str] = {}
        self.username_to_id: dict[str, str] = {}
        self.usernames_lookups: list[list[str]] = []

    def permalink(self, post_id: str) -> str:
        return f"https://mattermost.example.com/_redirect/pl/{post_id}"

    async def get_channel_name(self, channel_id: str) -> str:
        return "alerts"

    async def get_post(self, post_id: str) -> MattermostPost:
        return self.posts[post_id]

    async def get_thread_posts(self, post_id: str):
        root = self.posts[post_id]
        replies = [post for post in self.posts.values() if post.root_id == post_id]
        return sorted([root, *replies], key=lambda post: (post.create_at, post.id))

    async def get_user_display_name(self, user_id: str) -> str:
        if user_id in self.display_names:
            return self.display_names[user_id]
        return f"@{user_id}"

    async def get_user_ids_by_usernames(self, usernames: list[str]) -> dict[str, str]:
        self.usernames_lookups.append(list(usernames))
        return {
            name: self.username_to_id[name] for name in usernames if name in self.username_to_id
        }

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        props: dict | None = None,
        root_id: str | None = None,
    ):
        post = MattermostPost(
            id=f"incidentpost{len(self.created_posts):014d}",
            channel_id=channel_id,
            user_id="bot-user",
            message=message,
            create_at=1_700_000_100_000,
            root_id=root_id,
            props=props,
        )
        self.created_posts.append(
            {
                "channel_id": channel_id,
                "message": message,
                "props": props,
                "root_id": root_id,
                "post": post,
            }
        )
        self.posts[post.id] = post
        return post

    async def update_post(self, post_id: str, *, message=None, props=None) -> None:
        self.updated_posts.append({"post_id": post_id, "message": message, "props": props})
        if post_id in self.posts:
            changes = {}
            if message is not None:
                changes["message"] = message
            if props is not None:
                changes["props"] = props
            self.posts[post_id] = replace(self.posts[post_id], **changes)

    async def open_dialog(
        self,
        *,
        trigger_id: str,
        url: str,
        dialog: dict,
    ) -> None:
        self.opened_dialogs.append({"trigger_id": trigger_id, "url": url, "dialog": dialog})

    async def fetch_recent_channel_posts(self, channel_id: str, *, limit: int):
        return []

    async def aclose(self) -> None:
        return None


class FakeJiraClient:
    def __init__(self) -> None:
        self.created_payloads: list[dict] = []
        self.valid_updates: list[tuple[str, bool]] = []
        self.comments: list[tuple[str, str, str]] = []
        self.transitions: list[tuple[str, str]] = []
        self.valid_by_issue: dict[str, bool] = {}
        self.validity_updates: list[tuple[str, str]] = []
        self.validity_end_updates: list[tuple[str, datetime]] = []
        self.end_updates: list[tuple[str, datetime]] = []
        self.validity_by_issue: dict[str, str] = {}
        self.descriptions: list[tuple[str, str]] = []
        self.postmortem_payloads: list[dict] = []
        self.generic_comments: list[tuple[str, str]] = []

    async def create_issue(
        self,
        post,
        *,
        message_url: str,
        channel_name: str | None,
    ):
        key = f"OPS-{len(self.created_payloads) + 1}"
        self.created_payloads.append(
            {
                "post": post,
                "message_url": message_url,
                "channel_name": channel_name,
            }
        )
        self.valid_by_issue[key] = False
        return JiraIssue(key=key, url=f"https://jira.example.com/browse/{key}")

    async def create_postmortem_issue(
        self,
        post,
        *,
        message_url: str,
        channel_name: str | None,
        summary: str,
        description: str,
    ):
        key = f"OPS-{len(self.created_payloads) + 1}"
        self.created_payloads.append(
            {
                "post": post,
                "message_url": message_url,
                "channel_name": channel_name,
            }
        )
        self.postmortem_payloads.append(
            {
                "post": post,
                "message_url": message_url,
                "channel_name": channel_name,
                "summary": summary,
                "description": description,
            }
        )
        self.valid_by_issue[key] = False
        return JiraIssue(key=key, url=f"https://jira.example.com/browse/{key}")

    async def get_valid_incident(self, issue_key: str):
        return self.valid_by_issue.get(issue_key, False)

    async def set_valid_incident(self, issue_key: str, value: bool):
        self.valid_updates.append((issue_key, value))
        self.valid_by_issue[issue_key] = value

    async def set_validity(
        self, issue_key: str, option_value: str, *, ended_at: datetime | None = None
    ):
        self.validity_updates.append((issue_key, option_value))
        if ended_at is not None:
            self.validity_end_updates.append((issue_key, ended_at))
        self.validity_by_issue[issue_key] = option_value

    async def set_end_time(self, issue_key: str, ended_at: datetime):
        self.end_updates.append((issue_key, ended_at))

    async def set_description(self, issue_key: str, description: str):
        self.descriptions.append((issue_key, description))

    async def add_confirmation_comment(
        self, issue_key: str, *, incident_message_url: str, confirmed_by_user_id: str
    ):
        self.comments.append((issue_key, incident_message_url, confirmed_by_user_id))

    async def add_comment(self, issue_key: str, body: str):
        self.generic_comments.append((issue_key, body))

    async def transition_issue(self, issue_key: str, transition_id: str):
        self.transitions.append((issue_key, transition_id))

    async def aclose(self) -> None:
        return None


class FakeLlmClient:
    def __init__(
        self,
        report: str = (
            "[INC] 15.11.2023 - Ошибки API\n"
            "Участники инцидента: Иван Иванов (@ivanov.ivan), Петр Петров (@petrov.petr)\n"
            "Автор постмортема: Иван Иванов (@ivanov.ivan)\n"
            "##Сводка\n"
            "API начал отвечать 500.\n"
            "##Решение\n"
            "Откатили релиз.\n"
            "##Извлеченные уроки\n"
            "###Что было сделано хорошо / В чем повезло\n"
            " - Быстро нашли проблему.\n"
            "###Что пошло не так / В чем не повезло\n"
            " - не указано\n"
            "##Action Items\n"
            " - Завести алерт на рост 500.\n"
            "##Хронология\n"
            "01:15 - Обнаружили проблему\n"
            "01:20 - Откатили релиз"
        ),
    ) -> None:
        self.report = report
        self.prompts: list[str] = []
        self.summary_prompts: list[str] = []
        self.summary = "Суть: всё сломалось.\nСтатус: в работе."

    async def generate_postmortem(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.report

    async def generate_summary(self, prompt: str) -> str:
        self.summary_prompts.append(prompt)
        return self.summary

    async def aclose(self) -> None:
        return None


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        mattermost_url="https://mattermost.example.com",
        mattermost_token="mm-token",
        mattermost_alert_channel_id="alerts-channel",
        mattermost_incident_channel_id="incidents-channel",
        mattermost_incident_reaction_name="incident",
        mattermost_bot_user_id="bot-user",
        jira_base_url="https://jira.example.com",
        jira_api_token="jira-token",
        jira_project_key="OPS",
        jira_issue_type="Incident",
        jira_valid_incident_field="customfield_12345",
        jira_source_field="customfield_23456",
        jira_is_crit_alert_field="customfield_34567",
        jira_start_field=None,
        jira_end_field=None,
        jira_confirmed_status_id="31",
        database_url=f"sqlite:///{tmp_path / 'bot.db'}",
        mattermost_slash_token="slash-token",
        enable_websocket=False,
        enable_backfill_on_startup=False,
    )


@pytest.fixture()
def service(settings):
    engine = create_database_engine(settings.database_url)
    init_db(engine)
    repository = AlertTicketRepository(create_session_factory(engine))
    mattermost = FakeMattermostClient()
    jira = FakeJiraClient()
    service = IncidentBotService(
        settings=settings,
        repository=repository,
        mattermost_client=mattermost,
        jira_client=jira,
    )
    return service


def make_alert(
    post_id: str = POST_ID,
    channel_id: str = "alerts-channel",
    message: str = "CPU usage is above 95%",
    props: dict | None = None,
) -> MattermostPost:
    return MattermostPost(
        id=post_id,
        channel_id=channel_id,
        user_id="author-user",
        message=message,
        create_at=1_700_000_000_000,
        channel_name="alerts",
        props=props,
    )


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
async def test_creates_jira_issue_for_new_mattermost_message(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is not None
    assert ticket.jira_issue_key == "OPS-1"
    assert ticket.mattermost_alert_title == "CPU usage is above 95%"
    assert len(service.jira.created_payloads) == 1


@pytest.mark.asyncio
async def test_stores_grafana_alert_title(service):
    post = make_alert(
        message=(
            "[Деньги | Минус-слова vs Общее | выше на 70% [Crit]]"
            "(https://grafana.wb.ru/alerting/grafana/alert-id/view)\n"
            "State: Alerting"
        )
    )
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is not None
    assert ticket.mattermost_alert_title == ("Деньги | Минус-слова vs Общее | выше на 70% [Crit]")


@pytest.mark.asyncio
async def test_uses_stub_jira_issue_when_creation_disabled(settings):
    service = _build_service(
        replace(
            settings,
            jira_create_enabled=False,
            jira_stub_issue_key="ADSDEV-12024",
            service_public_url="https://bot.example.com/",
        )
    )
    post = make_alert()
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is not None
    assert ticket.jira_issue_key == f"ADSDEV-12024-{post.id[:12]}"
    assert ticket.jira_issue_url == (f"https://jira.example.com/browse/ADSDEV-12024-{post.id[:12]}")
    assert len(service.jira.created_payloads) == 0
    reply = _issue_reply(service, post.id, issue_key=ticket.jira_issue_key)
    assert reply["message"] == ""
    attachment = reply["props"]["attachments"][0]
    assert "title" not in attachment
    assert "title_link" not in attachment
    assert attachment["text"] == (
        "**Создана задача: [ADSDEV-12024](https://jira.example.com/browse/ADSDEV-12024)**"
    )
    assert ticket.jira_issue_key not in attachment["text"]


@pytest.mark.asyncio
async def test_reuses_display_stub_jira_issue_without_db_conflict(settings):
    service = _build_service(
        replace(
            settings,
            jira_create_enabled=False,
            jira_stub_issue_key="ADSDEV-12024",
        )
    )
    first_post = make_alert(post_id="firststubalertpost00000001")
    second_post = make_alert(post_id="secondstubalertpost0000002")
    service.mattermost.posts[first_post.id] = first_post
    service.mattermost.posts[second_post.id] = second_post

    first_ticket = await service.handle_alert_post(first_post)
    second_ticket = await service.handle_alert_post(second_post)

    assert first_ticket is not None
    assert second_ticket is not None
    assert first_ticket.jira_issue_key != second_ticket.jira_issue_key
    assert len(service.jira.created_payloads) == 0
    first_reply = _issue_reply(service, first_post.id, issue_key=first_ticket.jira_issue_key)
    second_reply = _issue_reply(service, second_post.id, issue_key=second_ticket.jira_issue_key)
    assert "Создана задача Jira: [ADSDEV-12024]" in first_reply["message"]
    assert "Создана задача Jira: [ADSDEV-12024]" in second_reply["message"]


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


@pytest.mark.asyncio
async def test_skips_alert_thread_reply(service):
    post = replace(
        make_alert(post_id="threadreplypost000000000001"),
        root_id=POST_ID,
    )
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is None
    assert len(service.jira.created_payloads) == 0
    assert service.repository.get_by_post_id(post.id) is None


@pytest.mark.asyncio
async def test_skips_resolved_alert_post(service):
    post = make_alert(message="✅ CPU usage is above 95% :: crit")
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is None
    assert len(service.jira.created_payloads) == 0
    assert service.repository.get_by_post_id(post.id) is None


@pytest.mark.asyncio
async def test_does_not_create_two_issues_for_same_post_id(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)
    await service.handle_alert_post(post)

    assert len(service.jira.created_payloads) == 1


@pytest.mark.asyncio
async def test_confirms_incident_through_reaction(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )

    ticket = service.repository.get_by_post_id(post.id)
    assert result.status == "confirmed"
    assert ticket is not None
    assert ticket.valid_incident is True
    assert ticket.incident_post_id is not None
    assert service.jira.valid_updates == [("OPS-1", True)]
    assert service.jira.validity_end_updates == []
    assert len(service.jira.comments) == 1
    assert len(service.jira.descriptions) == 1
    issue_key, description = service.jira.descriptions[0]
    assert issue_key == "OPS-1"
    assert "h2. Хронология" in description
    assert ticket.incident_message_url in description
    assert ticket.mattermost_message_url in description


@pytest.mark.asyncio
async def test_confirmed_incident_embeds_grafana_attachment(service):
    attachments = [
        {
            "color": "#F2495C",
            "title": "Деньги | Минус-слова vs Общее | выше на 70% [Crit]",
            "title_link": "http://grafana.wb.ru/alerting/grafana/alert-id/view",
            "text": "Runbook: https://wiki.example.com/runbook",
            "image_url": "http://grafana.wb.ru/render/d-solo/dashboard/panel.png",
            "footer": "Grafana v12.4.2",
        }
    ]
    post = make_alert(
        message="Деньги | Минус-слова vs Общее | выше на 70% [Crit]",
        props={"attachments": attachments},
    )
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )

    incident_posts = [
        created
        for created in service.mattermost.created_posts
        if created["channel_id"] == "incidents-channel"
    ]
    assert len(incident_posts) == 1
    incident_post = incident_posts[0]
    post_attachments = incident_post["props"]["attachments"]
    # Gray info block first, then the forwarded alert attachment(s) (a copy).
    info_block = post_attachments[0]
    assert info_block["color"] == "#4B5563"
    assert post_attachments[1:] == attachments
    assert post_attachments[1] is not attachments[0]
    info_text = info_block["text"]
    assert info_text.startswith("##### 🔴 Инцидент открыт")
    assert "Задача Jira: [OPS-1]" in info_text
    assert "Исходный алерт" in info_text
    # The alert text is in the forwarded block, not duplicated in the info block.
    assert "Деньги | Минус-слова vs Общее" not in info_text
    assert incident_post["message"] == ""


@pytest.mark.asyncio
async def test_checkmark_on_incident_post_sets_end_time(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(
            post_id=post.id,
            user_id="validator",
            emoji_name="incident",
            create_at=1,
        )
    )
    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.incident_post_id is not None

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="closer",
            emoji_name="white_check_mark",
            create_at=1_700_000_200_000,
        )
    )

    assert result.status == "incident_ended"
    assert service.jira.end_updates == [("OPS-1", datetime_from_mattermost_ms(1_700_000_200_000))]
    assert service.jira.validity_end_updates == []


@pytest.mark.asyncio
async def test_checkmark_on_incident_thread_reply_is_ignored(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(
            post_id=post.id,
            user_id="validator",
            emoji_name="incident",
            create_at=1,
        )
    )
    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.incident_post_id is not None
    reply = MattermostPost(
        id="incidentreply000000000001",
        channel_id="incidents-channel",
        user_id="closer",
        message="done",
        create_at=1_700_000_150_000,
        root_id=ticket.incident_post_id,
    )
    service.mattermost.posts[reply.id] = reply

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=reply.id,
            user_id="closer",
            emoji_name="heavy_check_mark",
            create_at=1_700_000_250_000,
        )
    )

    assert result.status == "ignored"
    assert service.jira.end_updates == []


@pytest.mark.asyncio
async def test_checkmark_on_incident_thread_generates_postmortem_for_existing_issue(service):
    service.llm = FakeLlmClient()
    service.mattermost.display_names.update(
        {
            "validator": "Иван Иванов (@ivanov.ivan)",
            "closer": "Петр Петров (@petrov.petr)",
            "bot-user": "@incident-bot",
        }
    )
    post = make_alert(message="API 500 on checkout")
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(
            post_id=post.id,
            user_id="validator",
            emoji_name="incident",
            create_at=1,
        )
    )
    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.incident_post_id is not None
    reply = MattermostPost(
        id="incidentreply000000000002",
        channel_id="incidents-channel",
        user_id="closer",
        message="Откатили релиз, ошибки ушли.",
        create_at=1_700_000_250_000,
        root_id=ticket.incident_post_id,
    )
    service.mattermost.posts[reply.id] = reply

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="validator",
            emoji_name="white_check_mark",
            create_at=1_700_000_300_000,
        )
    )

    assert result.status == "incident_ended"
    assert service.jira.end_updates == [("OPS-1", datetime_from_mattermost_ms(1_700_000_300_000))]
    assert len(service.jira.created_payloads) == 1
    assert len(service.llm.prompts) == 1
    prompt = service.llm.prompts[0]
    assert "API 500 on checkout" in prompt
    assert "Откатили релиз" in prompt
    assert "Иван Иванов (@ivanov.ivan)" in prompt
    assert "Петр Петров (@petrov.petr)" in prompt
    assert "##Извлеченные уроки" in prompt
    assert "###Что было сделано хорошо / В чем повезло" in prompt
    assert "###Что пошло не так / В чем не повезло" in prompt
    assert "##Action Items" in prompt
    assert "только те action items, которые обсуждались в треде" in prompt
    assert "до 10 слов" in prompt
    assert "до 80 символов" in prompt
    assert "до 120 символов" in prompt
    issue_key, description = service.jira.descriptions[-1]
    assert issue_key == "OPS-1"
    assert "|*Авторы ПМ*|Иван Иванов (@ivanov.ivan)|" in description
    assert "|*Участники инцидента*|" in description
    assert "Иван Иванов (@ivanov.ivan)" in description
    assert "Петр Петров (@petrov.petr)" in description
    assert "[Основное сообщение инцидента|" in description
    assert ticket.incident_message_url in description
    assert ticket.mattermost_message_url in description
    assert "h2. Извлеченные уроки" in description
    assert "h2. Action Items" in description
    assert "h2. Хронология" in description
    assert "API начал отвечать 500." not in description
    assert "##Хронология" not in description
    assert service.jira.generic_comments
    comment_issue_key, comment = service.jira.generic_comments[-1]
    assert comment_issue_key == "OPS-1"
    assert "Постмортем сгенерирован" in comment
    assert "API начал отвечать 500." in comment
    assert "##Хронология" in comment
    thread_replies = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == ticket.incident_post_id
        and "Инцидентный отчет готов" in created["message"]
    ]
    assert len(thread_replies) == 1
    assert "**Что случилось:**" in thread_replies[0]["message"]
    assert "**Как решили:**" in thread_replies[0]["message"]
    assert "**Action items:**" in thread_replies[0]["message"]
    assert "##Сводка" not in thread_replies[0]["message"]
    assert "##Хронология" not in thread_replies[0]["message"]


@pytest.mark.asyncio
async def test_checkmark_on_manual_incident_thread_creates_postmortem_issue(service):
    service.llm = FakeLlmClient()
    service.mattermost.display_names.update(
        {
            "author": "Анна Автор (@author.anna)",
            "closer": "Иван Иванов (@ivanov.ivan)",
        }
    )
    root = MattermostPost(
        id="manualincidentroot000000001",
        channel_id="incidents-channel",
        user_id="author",
        message="Создали инцидент по росту 500 на checkout.",
        create_at=1_700_000_100_000,
        channel_name="incidents",
    )
    reply = MattermostPost(
        id="manualincidentreply00000001",
        channel_id="incidents-channel",
        user_id="closer",
        message="Проблема была в релизе, откат помог.",
        create_at=1_700_000_200_000,
        root_id=root.id,
    )
    service.mattermost.posts[root.id] = root
    service.mattermost.posts[reply.id] = reply

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=root.id,
            user_id="closer",
            emoji_name="white_check_mark",
            create_at=1_700_000_300_000,
        )
    )

    assert result.status == "incident_ended"
    ticket = service.repository.get_by_incident_post_id(root.id)
    assert ticket is not None
    assert ticket.jira_issue_key == "OPS-1"
    assert ticket.valid_incident is True
    assert len(service.jira.postmortem_payloads) == 1
    payload = service.jira.postmortem_payloads[0]
    assert payload["summary"] == "[INC] 15.11.2023 - Ошибки API"
    assert "[Основное сообщение инцидента|" in payload["description"]
    assert "Анна Автор (@author.anna)" in payload["description"]
    assert "Иван Иванов (@ivanov.ivan)" in payload["description"]
    assert "API начал отвечать 500." not in payload["description"]
    assert service.jira.valid_updates == [("OPS-1", True)]
    assert service.jira.end_updates == [("OPS-1", datetime_from_mattermost_ms(1_700_000_300_000))]
    assert service.jira.generic_comments
    assert "API начал отвечать 500." in service.jira.generic_comments[-1][1]
    thread_replies = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == root.id and "Инцидентный отчет готов" in created["message"]
    ]
    assert len(thread_replies) == 1
    assert "Полный постмортем" in thread_replies[0]["message"]
    assert "##Сводка" not in thread_replies[0]["message"]


def test_postmortem_summary_limits_llm_title_words_and_chars():
    report = (
        "[INC] 15.11.2023 - "
        "Очень длинное название инцидента про падение checkout api после релиза "
        "новой корзины с большим количеством технических деталей"
    )

    summary = extract_postmortem_summary(report, fallback="fallback")
    title = summary.split(" - ", 1)[1]

    assert len(summary) <= 120
    assert len(title) <= 80
    assert len(title.split()) <= 10
    assert summary == (
        "[INC] 15.11.2023 - Очень длинное название инцидента про падение checkout api после релиза"
    )


@pytest.mark.asyncio
async def test_checkmark_on_alert_post_is_ignored(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=post.id,
            user_id="closer",
            emoji_name="white_check_mark",
            create_at=1_700_000_200_000,
        )
    )

    assert result.status == "ignored"
    assert service.jira.end_updates == []


@pytest.mark.asyncio
async def test_false_reaction_sets_validity_without_incident_post(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=post.id,
            user_id="validator",
            emoji_name="man_gesturing_no",
            create_at=1,
        )
    )

    ticket = service.repository.get_by_post_id(post.id)
    assert result.status == "validity_set"
    assert service.jira.validity_updates == [("OPS-1", "Ложный")]
    assert service.jira.validity_end_updates == [("OPS-1", datetime_from_mattermost_ms(1))]
    assert ticket.validity_label == "Ложный"
    # Lightweight path: not a confirmed incident, nothing posted to incidents channel.
    assert ticket.valid_incident is False
    assert ticket.incident_post_id is None
    incident_posts = [
        created
        for created in service.mattermost.created_posts
        if created["channel_id"] == "incidents-channel"
    ]
    assert incident_posts == []
    thread_replies = [
        created for created in service.mattermost.created_posts if created["root_id"] == post.id
    ]
    # One reply for issue creation, one for the validity change.
    assert len(thread_replies) == 2
    assert "Ложный" in thread_replies[1]["message"]


@pytest.mark.asyncio
async def test_expected_reaction_sets_validity(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=post.id,
            user_id="validator",
            emoji_name="arrows_counterclockwise",
            create_at=1,
        )
    )

    assert result.status == "validity_set"
    assert service.jira.validity_updates == [("OPS-1", "Ожидаемый")]
    assert service.jira.validity_end_updates == [("OPS-1", datetime_from_mattermost_ms(1))]


@pytest.mark.asyncio
async def test_last_validity_reaction_wins(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="v", emoji_name="incident", create_at=1)
    )
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="v", emoji_name="man_gesturing_no", create_at=2)
    )

    ticket = service.repository.get_by_post_id(post.id)
    assert service.jira.valid_updates == [("OPS-1", True)]
    assert service.jira.validity_updates == [("OPS-1", "Ложный")]
    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"
    assert ticket.validity_label == "Ложный"


@pytest.mark.asyncio
async def test_repeated_validity_reaction_is_idempotent(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="v", emoji_name="man_gesturing_no", create_at=1)
    )
    result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="v", emoji_name="man_gesturing_no", create_at=2)
    )

    assert result.status == "validity_set"
    assert service.jira.validity_updates == [("OPS-1", "Ложный")]
    validity_replies = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post.id and "Валидность" in created["message"]
    ]
    assert len(validity_replies) == 1


@pytest.mark.asyncio
async def test_unknown_reaction_is_ignored(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="v", emoji_name="thumbsup", create_at=1)
    )

    assert result.status == "ignored"
    assert service.jira.validity_updates == []


@pytest.mark.asyncio
async def test_replies_in_alert_thread_when_issue_created(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    thread_replies = [
        created for created in service.mattermost.created_posts if created["root_id"] == post.id
    ]
    assert len(thread_replies) == 1
    reply = thread_replies[0]
    assert reply["channel_id"] == "alerts-channel"
    assert "OPS-1" in reply["message"]
    assert "https://jira.example.com/browse/OPS-1" in reply["message"]


@pytest.mark.asyncio
async def test_replies_in_alert_thread_on_status_change(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=2)
    )

    thread_replies = [
        created for created in service.mattermost.created_posts if created["root_id"] == post.id
    ]
    # One reply for issue creation, one for status change; no duplicates on retry.
    assert len(thread_replies) == 2
    status_reply = thread_replies[1]
    assert status_reply["channel_id"] == "alerts-channel"
    assert "Инцидент заведён" in status_reply["message"]
    assert "Ссылка на сообщение" in status_reply["message"]

    incident_posts = [
        created
        for created in service.mattermost.created_posts
        if created["channel_id"] == "incidents-channel"
    ]
    assert len(incident_posts) == 1
    info_text = incident_posts[0]["props"]["attachments"][0]["text"]
    assert "Подтвердил: @validator" in info_text


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


def test_debug_admin_routes_are_disabled_by_default(service, settings):
    app = create_app(settings, service=service)
    with TestClient(app) as client:
        response = client.get("/debug/admin")

    assert response.status_code == 404


def test_debug_admin_lists_alerts_when_enabled(service, settings):
    post = make_alert(message="CPU usage is above 95%\nsecond line")
    service.repository.create_or_get_alert(
        post,
        message_url=service.mattermost.permalink(post.id),
        channel_name="alerts",
    )
    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        summary_response = client.get("/debug/admin/api/summary")
        list_response = client.get("/debug/admin/api/alerts")
        detail_response = client.get(f"/debug/admin/api/alerts/{post.id}")

    assert summary_response.status_code == 200
    assert summary_response.json()["total"] == 1
    assert list_response.status_code == 200
    assert list_response.json()["alerts"][0]["mattermost_post_id"] == post.id
    assert list_response.json()["alerts"][0]["mattermost_alert_title"] == ("CPU usage is above 95%")
    assert "second line" in list_response.json()["alerts"][0]["mattermost_message_preview"]
    assert detail_response.status_code == 200
    assert detail_response.json()["mattermost_alert_title"] == "CPU usage is above 95%"
    assert detail_response.json()["mattermost_message_text"] == post.message


def test_debug_admin_filters_empty_validity(service, settings):
    empty_post = make_alert(post_id="emptyvaliditypost000000001")
    labeled_post = make_alert(post_id="labeledvaliditypost000001")
    for post in (empty_post, labeled_post):
        service.repository.create_or_get_alert(
            post,
            message_url=service.mattermost.permalink(post.id),
            channel_name="alerts",
        )
    service.repository.set_validity_label(labeled_post.id, "Ложный")

    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        summary_response = client.get("/debug/admin/api/summary")
        list_response = client.get("/debug/admin/api/alerts?validity=empty")
        detail_response = client.get(f"/debug/admin/api/alerts/{empty_post.id}")

    assert summary_response.status_code == 200
    assert summary_response.json()["empty_validity"] == 1
    assert list_response.status_code == 200
    alerts = list_response.json()["alerts"]
    assert [alert["mattermost_post_id"] for alert in alerts] == [empty_post.id]
    assert alerts[0]["validity_is_empty"] is True
    assert alerts[0]["validity_status"] is None
    assert alerts[0]["mattermost_message_created_at"] is not None
    assert alerts[0]["created_at"] is not None
    assert detail_response.status_code == 200
    assert detail_response.json()["validity_is_empty"] is True


def test_debug_admin_retries_jira_creation_for_ticket_without_issue(service, settings):
    post = make_alert()
    service.repository.create_or_get_alert(
        post,
        message_url=service.mattermost.permalink(post.id),
        channel_name="alerts",
    )
    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    client = TestClient(app)
    try:
        response = client.post(f"/debug/admin/api/alerts/{post.id}/jira/recreate")
    finally:
        client.close()

    ticket = service.repository.get_by_post_id(post.id)
    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert response.json()["jira_issue_key"] == "OPS-1"
    assert ticket is not None
    assert ticket.jira_issue_key == "OPS-1"
    assert len(service.jira.created_payloads) == 1


@pytest.mark.asyncio
async def test_debug_admin_recreate_without_force_conflicts_existing_issue(service, settings):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        response = client.post(f"/debug/admin/api/alerts/{post.id}/jira/recreate")

    assert response.status_code == 409
    assert response.json()["status"] == "conflict"
    assert len(service.jira.created_payloads) == 1


@pytest.mark.asyncio
async def test_debug_admin_force_recreates_confirmed_issue_without_duplicate_incident(
    service, settings
):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(
            post_id=post.id,
            user_id="validator",
            emoji_name="incident",
            create_at=1,
        )
    )

    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        response = client.post(f"/debug/admin/api/alerts/{post.id}/jira/recreate?force=true")

    ticket = service.repository.get_by_post_id(post.id)
    incident_posts = [
        created
        for created in service.mattermost.created_posts
        if created["channel_id"] == "incidents-channel"
    ]
    assert response.status_code == 200
    assert response.json()["status"] == "recreated"
    assert response.json()["previous_jira_issue_key"] == "OPS-1"
    assert response.json()["jira_issue_key"] == "OPS-2"
    assert ticket is not None
    assert ticket.jira_issue_key == "OPS-2"
    assert ticket.valid_incident is True
    assert ticket.jira_confirmation_comment_added is True
    assert service.jira.valid_updates == [("OPS-1", True), ("OPS-2", True)]
    assert [comment[0] for comment in service.jira.comments] == ["OPS-1", "OPS-2"]
    assert len(incident_posts) == 1


@pytest.mark.asyncio
async def test_repeated_confirmation_does_not_duplicate_incident_post(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=2)
    )

    incident_posts = [
        created
        for created in service.mattermost.created_posts
        if created["channel_id"] == "incidents-channel"
    ]
    assert len(incident_posts) == 1
    assert len(service.jira.comments) == 1


def test_builds_jira_payload(settings):
    post = replace(make_alert(), message="CPU usage is above 95%\nsecond line")
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    fields = payload["fields"]
    assert fields["project"] == {"key": "OPS"}
    assert fields["issuetype"] == {"name": "Incident"}
    assert "customfield_12345" not in fields
    assert fields["customfield_23456"] == {"value": "Crit alert"}
    assert fields["customfield_34567"] == {"value": "Да"}
    assert fields["summary"] == "[INC] 15.11.2023 - CPU usage is above 95%"
    description = fields["description"]
    assert isinstance(description, str)
    assert "h3. 🔔 Невалидный алерт из Band" in description
    assert "|Автор|" not in description
    assert "{quote}" not in description
    assert "CPU usage is above 95%" not in description
    assert "|Канал|alerts|" in description
    assert "|Время сообщения|15.11.2023 01:13|" in description
    assert (
        "|Исходное сообщение|[Открыть в Band|"
        "https://mattermost.example.com/_redirect/pl/post]|" in description
    )
    assert f"{{{{{POST_ID}}}}}" in description


def test_builds_manual_incident_payload_without_alert_only_fields(settings):
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        make_alert(channel_id="incidents-channel"),
        message_url="https://mattermost.example.com/_redirect/pl/incident",
        channel_name="incidents",
        summary="[INC] 15.11.2023 - Ручной инцидент",
        description="PM template",
        labels=["mattermost-incident", "postmortem"],
        include_alert_fields=False,
    )

    fields = payload["fields"]
    assert fields["summary"] == "[INC] 15.11.2023 - Ручной инцидент"
    assert fields["description"] == "PM template"
    assert fields["labels"] == ["mattermost-incident", "postmortem"]
    assert "customfield_23456" not in fields
    assert "customfield_34567" not in fields


def test_summary_uses_first_non_empty_line_without_leading_emoji(settings):
    message = "\n🔴 Доля рекламных кликов :: Платформа :: Ниже на 10% :: crit\n@sre-ads-duty\n"
    post = replace(make_alert(), message=message)
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == (
        "[INC] 15.11.2023 - Доля рекламных кликов :: Платформа :: Ниже на 10% :: crit"
    )


def test_summary_uses_alert_title_after_emoji_only_line(settings):
    message = "🔴\nДеньги | Минус-слова vs Общее | выше на 70% [Crit]\n@sre-ads-duty\n"
    post = replace(make_alert(), message=message)
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == (
        "[INC] 15.11.2023 - Деньги | Минус-слова vs Общее | выше на 70% [Crit]"
    )


def test_summary_removes_mattermost_emoji_shortcode(settings):
    post = replace(
        make_alert(),
        message=":rotating_light: Деньги | Минус-слова vs Общее | выше на 70% [Crit]",
    )
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == (
        "[INC] 15.11.2023 - Деньги | Минус-слова vs Общее | выше на 70% [Crit]"
    )


def test_summary_uses_grafana_alert_markdown_link_title(settings):
    message = (
        "🔴 [Деньги | Минус-слова vs Общее | выше на 70% [Crit]]"
        "(http://grafana.wb.ru/alerting/grafana/abc123/view)\n"
        "@sre-ads-duty\n"
    )
    post = replace(make_alert(), message=message)
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == (
        "[INC] 15.11.2023 - Деньги | Минус-слова vs Общее | выше на 70% [Crit]"
    )


def test_summary_uses_grafana_alert_angle_link_title(settings):
    message = (
        "🔴 <http://grafana.wb.ru/alerting/grafana/abc123/view|"
        "Доля рекламных кликов :: Платформа :: Ниже на 10% :: crit>\n"
        "@sre-ads-duty\n"
    )
    post = replace(make_alert(), message=message)
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == (
        "[INC] 15.11.2023 - Доля рекламных кликов :: Платформа :: Ниже на 10% :: crit"
    )


def test_summary_uses_grafana_attachment_title_when_message_is_empty(settings):
    post = MattermostPost.from_api(
        {
            "id": POST_ID,
            "channel_id": "alerts-channel",
            "user_id": "grafana-bot",
            "message": "",
            "create_at": 1700000000000,
            "props": {
                "attachments": [
                    {
                        "title": "ads_stat-consumer_antifraud_remove_messages 20% [Crit]",
                        "title_link": ("http://grafana.wb.ru/alerting/grafana/abc123/view"),
                        "text": (
                            "Message: Кол-во сообщений на удаление в датабас "
                            "антифрода снизилось на 20%"
                        ),
                    }
                ]
            },
        }
    )
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == (
        "[INC] 15.11.2023 - ads_stat-consumer_antifraud_remove_messages 20% [Crit]"
    )
    assert "Message: Кол-во сообщений" not in payload["fields"]["description"]


def test_builds_jira_payload_with_start_field(settings):
    post = make_alert()
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
        start_field_id="customfield_45678",
    )

    # Jira date-time picker wants ISO 8601 with a [+-]hhmm offset and
    # mandatory fractional seconds, derived from the alert arrival time.
    assert payload["fields"]["customfield_45678"] == "2023-11-15T01:13:20.000+0300"


def test_builds_jira_payload_without_start_field_by_default(settings):
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        make_alert(),
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert "customfield_45678" not in payload["fields"]


def test_builds_jira_payload_with_current_date_when_post_date_missing(settings, monkeypatch):
    monkeypatch.setattr(
        jira_payload_module,
        "backend_now",
        lambda: datetime(2026, 5, 29, 22, 30, tzinfo=UTC),
    )
    post = replace(make_alert(), create_at=0)
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == "[INC] 30.05.2026 - CPU usage is above 95%"
    assert "|Время сообщения|—|" in payload["fields"]["description"]


@pytest.mark.asyncio
async def test_jira_client_makes_no_calls_in_test_mode(settings):
    """JIRA_CREATE_ENABLED=false must not hit Jira for issue-key operations, so a
    bogus stub key never aborts the confirm/validity/end flows."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected Jira call in test mode: {request.method} {request.url}")

    client = jira_module.JiraClient(
        replace(settings, jira_create_enabled=False, jira_stub_issue_key="ADSDEV-12024"),
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )

    assert await client.get_valid_incident("OPS-1") is None
    await client.set_valid_incident("OPS-1", True)
    await client.set_validity("OPS-1", "Ложный")
    await client.set_end_time("OPS-1", datetime(2026, 1, 1, tzinfo=UTC))
    await client.set_description("OPS-1", "desc")
    await client.add_comment("OPS-1", "body")
    await client.transition_issue("OPS-1", "31")
    issue = await client.create_postmortem_issue(
        make_alert(), message_url="u", channel_name="c", summary="s", description="d"
    )
    assert issue.key.startswith("ADSDEV-12024-")
    await client.aclose()


@pytest.mark.asyncio
async def test_creates_issue_with_default_valid_incident_and_option_ids(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read()) if request.method == "POST" else None
        requests.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes":
            return httpx.Response(
                200,
                json={"values": [{"id": "10001", "name": "Incident"}]},
            )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001":
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "customfield_12345": {
                            "allowedValues": [
                                {"id": "101", "value": "Валидный"},
                                {"id": "102", "value": "Ложный"},
                                {"id": "103", "value": "Ожидаемый"},
                            ]
                        },
                        "customfield_23456": {
                            "allowedValues": [{"id": "201", "value": "Crit alert"}]
                        },
                        "customfield_34567": {"allowedValues": [{"id": "301", "value": "Да"}]},
                    }
                },
            )
        if request.url.path == "/rest/api/2/issue":
            return httpx.Response(201, json={"key": "OPS-1"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        issue = await client.create_issue(
            make_alert(),
            message_url="https://mattermost.example.com/_redirect/pl/post",
            channel_name="alerts",
        )
    finally:
        await client.aclose()

    issue_body = requests[-1]["body"]["fields"]
    assert issue.key == "OPS-1"
    assert requests[0]["path"] == "/rest/api/2/issue/createmeta/OPS/issuetypes"
    assert requests[1]["path"] == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001"
    assert "customfield_12345" not in issue_body
    assert issue_body["customfield_23456"] == {"id": "201"}
    assert issue_body["customfield_34567"] == {"id": "301"}
    assert isinstance(issue_body["description"], str)


@pytest.mark.asyncio
async def test_jira_preflight_resolves_fields_and_options(settings):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes":
            return httpx.Response(
                200,
                json={"values": [{"id": "10001", "name": "Incident"}]},
            )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001":
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "customfield_12345": {
                            "allowedValues": [
                                {"id": "101", "value": "Валидный"},
                                {"id": "102", "value": "Ложный"},
                                {"id": "103", "value": "Ожидаемый"},
                            ]
                        },
                        "customfield_23456": {
                            "allowedValues": [{"id": "201", "value": "Crit alert"}]
                        },
                        "customfield_34567": {"allowedValues": [{"id": "301", "value": "Да"}]},
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        result = await client.preflight_check()
    finally:
        await client.aclose()

    assert result["jira_issue_type_id"] == "10001"
    assert result["create_field_count"] == 3
    assert result["valid_incident_field_id"] == "customfield_12345"
    assert result["source_option"] == {"id": "201"}
    assert result["is_crit_alert_option"] == {"id": "301"}
    assert requests == [
        "/rest/api/2/issue/createmeta/OPS/issuetypes",
        "/rest/api/2/issue/createmeta/OPS/issuetypes/10001",
    ]


@pytest.mark.asyncio
async def test_creates_postmortem_issue_without_alert_source_fields(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read()) if request.method == "POST" else None
        requests.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path == "/rest/api/2/issue":
            return httpx.Response(201, json={"key": "OPS-1"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        issue = await client.create_postmortem_issue(
            make_alert(channel_id="incidents-channel"),
            message_url="https://mattermost.example.com/_redirect/pl/incident",
            channel_name="incidents",
            summary="[INC] 15.11.2023 - Ручной инцидент",
            description="PM template",
        )
    finally:
        await client.aclose()

    issue_body = requests[-1]["body"]["fields"]
    assert issue.key == "OPS-1"
    assert len(requests) == 1
    assert requests[0]["method"] == "POST"
    assert requests[0]["path"] == "/rest/api/2/issue"
    assert issue_body["summary"] == "[INC] 15.11.2023 - Ручной инцидент"
    assert issue_body["description"] == "PM template"
    assert issue_body["labels"] == ["mattermost-incident", "postmortem"]
    assert "customfield_12345" not in issue_body
    assert "customfield_23456" not in issue_body
    assert "customfield_34567" not in issue_body


@pytest.mark.asyncio
async def test_creates_issue_with_paged_create_metadata_fields(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read()) if request.method == "POST" else None
        requests.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes":
            return httpx.Response(
                200,
                json={
                    "last": True,
                    "size": 1,
                    "start": 0,
                    "values": [{"id": "10001", "name": "Incident"}],
                },
            )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001":
            if request.url.params.get("startAt") == "0":
                return httpx.Response(
                    200,
                    json={
                        "last": False,
                        "size": 1,
                        "start": 0,
                        "total": 4,
                        "values": [
                            {
                                "fieldId": "summary",
                                "name": "Summary",
                            },
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "last": True,
                    "size": 3,
                    "start": 1,
                    "total": 3,
                    "values": [
                        {
                            "fieldId": "customfield_12345",
                            "allowedValues": [
                                {"id": "101", "value": "Валидный"},
                            ],
                        },
                        {
                            "fieldId": "customfield_23456",
                            "allowedValues": [{"id": "201", "value": "Crit alert"}],
                        },
                        {
                            "fieldId": "customfield_34567",
                            "allowedValues": [{"id": "301", "value": "Да"}],
                        },
                    ],
                },
            )
        if request.url.path == "/rest/api/2/issue":
            return httpx.Response(201, json={"key": "OPS-1"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        issue = await client.create_issue(
            make_alert(),
            message_url="https://mattermost.example.com/_redirect/pl/post",
            channel_name="alerts",
        )
    finally:
        await client.aclose()

    issue_body = requests[-1]["body"]["fields"]
    field_metadata_requests = [
        request
        for request in requests
        if request["path"] == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001"
    ]
    assert issue.key == "OPS-1"
    assert len(field_metadata_requests) == 2
    assert issue_body["customfield_23456"] == {"id": "201"}
    assert issue_body["customfield_34567"] == {"id": "301"}


@pytest.mark.asyncio
async def test_create_issue_sends_start_field_when_configured(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read()) if request.method == "POST" else None
        requests.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes":
            return httpx.Response(200, json={"values": [{"id": "10001", "name": "Incident"}]})
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001":
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "customfield_23456": {
                            "allowedValues": [{"id": "201", "value": "Crit alert"}]
                        },
                        "customfield_34567": {"allowedValues": [{"id": "301", "value": "Да"}]},
                    }
                },
            )
        if request.url.path == "/rest/api/2/issue":
            return httpx.Response(201, json={"key": "OPS-1"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        replace(settings, jira_start_field="customfield_45678"),
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        await client.create_issue(
            make_alert(),
            message_url="https://mattermost.example.com/_redirect/pl/post",
            channel_name="alerts",
        )
    finally:
        await client.aclose()

    issue_body = requests[-1]["body"]["fields"]
    assert issue_body["customfield_45678"] == "2023-11-15T01:13:20.000+0300"


@pytest.mark.asyncio
async def test_jira_client_uses_bearer_auth_by_default(settings):
    headers = jira_module.build_jira_auth_headers(settings)

    assert headers["Authorization"] == "Bearer jira-token"
    assert headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_jira_client_uses_rest_api_v2(settings):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/rest/api/2/issue/OPS-1":
            return httpx.Response(
                200, json={"fields": {"customfield_12345": {"value": "Валидный"}}}
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            headers=jira_module.build_jira_auth_headers(settings),
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        value = await client.get_valid_incident("OPS-1")
    finally:
        await client.aclose()

    assert value is True
    assert requests == ["/rest/api/2/issue/OPS-1"]


@pytest.mark.asyncio
async def test_resolves_valid_incident_field_name_from_jira(settings):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/rest/api/2/field":
            return httpx.Response(
                200,
                json=[
                    {"id": "summary", "name": "Summary"},
                    {"id": "customfield_12345", "name": "Valid Incident"},
                ],
            )
        if request.url.path == "/rest/api/2/issue/OPS-1":
            return httpx.Response(
                200, json={"fields": {"customfield_12345": {"value": "Валидный"}}}
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        replace(settings, jira_valid_incident_field="Valid Incident"),
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        value = await client.get_valid_incident("OPS-1")
    finally:
        await client.aclose()

    assert value is True
    assert requests == ["/rest/api/2/field", "/rest/api/2/issue/OPS-1"]


@pytest.mark.asyncio
async def test_updates_valid_incident_as_jira_option(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.read()) if request.method == "PUT" else None,
            }
        )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes":
            return httpx.Response(
                200,
                json={"values": [{"id": "10001", "name": "Incident"}]},
            )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001":
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "customfield_12345": {"allowedValues": [{"id": "201", "value": "Валидный"}]}
                    }
                },
            )
        if request.url.path == "/rest/api/2/issue/OPS-1":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        await client.set_valid_incident("OPS-1", True)
    finally:
        await client.aclose()

    assert requests == [
        {
            "method": "GET",
            "path": "/rest/api/2/issue/createmeta/OPS/issuetypes",
            "body": None,
        },
        {
            "method": "GET",
            "path": "/rest/api/2/issue/createmeta/OPS/issuetypes/10001",
            "body": None,
        },
        {
            "method": "PUT",
            "path": "/rest/api/2/issue/OPS-1",
            "body": {"fields": {"customfield_12345": {"id": "201"}}},
        },
    ]


@pytest.mark.asyncio
async def test_updates_validity_with_end_field_when_configured(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.read()) if request.method == "PUT" else None,
            }
        )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes":
            return httpx.Response(
                200,
                json={"values": [{"id": "10001", "name": "Incident"}]},
            )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001":
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "customfield_12345": {"allowedValues": [{"id": "202", "value": "Ложный"}]}
                    }
                },
            )
        if request.url.path == "/rest/api/2/issue/OPS-1":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        replace(settings, jira_end_field="customfield_56789"),
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        await client.set_validity(
            "OPS-1",
            "Ложный",
            ended_at=datetime(2026, 5, 29, 22, 30, tzinfo=UTC),
        )
    finally:
        await client.aclose()

    assert requests[-1] == {
        "method": "PUT",
        "path": "/rest/api/2/issue/OPS-1",
        "body": {
            "fields": {
                "customfield_12345": {"id": "202"},
                "customfield_56789": "2026-05-30T01:30:00.000+0300",
            }
        },
    }


@pytest.mark.asyncio
async def test_updates_end_field_without_validity(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.read()) if request.method == "PUT" else None,
            }
        )
        if request.url.path == "/rest/api/2/issue/OPS-1":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        replace(settings, jira_end_field="customfield_56789"),
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        await client.set_end_time(
            "OPS-1",
            datetime(2026, 5, 29, 22, 30, tzinfo=UTC),
        )
    finally:
        await client.aclose()

    assert requests == [
        {
            "method": "PUT",
            "path": "/rest/api/2/issue/OPS-1",
            "body": {
                "fields": {
                    "customfield_56789": "2026-05-30T01:30:00.000+0300",
                }
            },
        }
    ]


@pytest.mark.asyncio
async def test_llm_client_uses_openai_compatible_chat_completions(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.read()),
            }
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "[INC] 15.11.2023 - Отчет"}}]},
        )

    client = PostmortemLlmClient(
        replace(
            settings,
            llm_api_token="llm-token",
            llm_base_url="https://corellm.wb.ru/deepseek/v1",
        ),
        http_client=httpx.AsyncClient(
            base_url="https://corellm.wb.ru/deepseek/v1/",
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        report = await client.generate_postmortem("thread transcript")
    finally:
        await client.aclose()

    assert report == "[INC] 15.11.2023 - Отчет"
    assert requests[0]["method"] == "POST"
    assert requests[0]["path"] == "/deepseek/v1/chat/completions"
    assert requests[0]["body"]["model"] == "deepseek-chat"
    assert requests[0]["body"]["messages"][1]["content"] == "thread transcript"
    assert "temperature" not in requests[0]["body"]


@pytest.mark.asyncio
async def test_llm_client_assembles_streamed_sse_deltas(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append({"body": json.loads(request.read())})
        sse = (
            'data: {"choices":[{"delta":{"content":"[INC] "}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"15.11"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":" - Отчет"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse.encode("utf-8"),
        )

    client = PostmortemLlmClient(
        replace(settings, llm_api_token="llm-token"),
        http_client=httpx.AsyncClient(
            base_url="https://corellm.wb.ru/deepseek/v1/",
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        report = await client.generate_postmortem("thread transcript")
    finally:
        await client.aclose()

    assert report == "[INC] 15.11 - Отчет"
    assert requests[0]["body"]["stream"] is True


@pytest.mark.asyncio
async def test_llm_client_wraps_transport_errors_as_api_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = PostmortemLlmClient(
        replace(settings, llm_api_token="llm-token", api_retry_attempts=1),
        http_client=httpx.AsyncClient(
            base_url="https://corellm.wb.ru/deepseek/v1/",
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        with pytest.raises(ApiError) as excinfo:
            await client.generate_postmortem("thread transcript")
    finally:
        await client.aclose()

    assert excinfo.value.retryable is True
    assert "ConnectError" in str(excinfo.value)


@pytest.mark.asyncio
async def test_llm_preflight_uses_small_openai_compatible_request(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.read()),
            }
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    client = PostmortemLlmClient(
        replace(
            settings,
            llm_api_token="llm-token",
            llm_base_url="https://corellm.wb.ru/deepseek/v1",
            llm_model="DeepSeek-V3.1 Terminus",
            llm_max_tokens=8000,
        ),
        http_client=httpx.AsyncClient(
            base_url="https://corellm.wb.ru/deepseek/v1/",
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        result = await client.preflight_check()
    finally:
        await client.aclose()

    assert result["llm_model"] == "DeepSeek-V3.1 Terminus"
    assert result["llm_response_length"] == 2
    assert requests[0]["method"] == "POST"
    assert requests[0]["path"] == "/deepseek/v1/chat/completions"
    assert requests[0]["body"]["model"] == "DeepSeek-V3.1 Terminus"
    assert requests[0]["body"]["messages"][0]["role"] == "user"
    assert requests[0]["body"]["max_tokens"] == 16
    assert "temperature" not in requests[0]["body"]


def test_builds_incident_channel_message(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    ticket, _ = service.repository.create_or_get_alert(
        post,
        message_url=service.mattermost.permalink(post.id),
        channel_name="alerts",
    )
    ticket.jira_issue_key = "OPS-1"
    ticket.jira_issue_url = "https://jira.example.com/browse/OPS-1"

    message = format_incident_message(
        ticket,
        confirmed_by="@validator",
        confirmed_at=datetime(2026, 5, 29, 22, 30, tzinfo=UTC),
    )

    assert "CPU usage is above 95%" in message
    assert "Исходный алерт" in message
    assert "OPS-1" in message
    assert "@validator" in message
    assert "Время подтверждения: 30.05.2026 01:30" in message


def _build_service(settings):
    engine = create_database_engine(settings.database_url)
    init_db(engine)
    repository = AlertTicketRepository(create_session_factory(engine))
    return IncidentBotService(
        settings=settings,
        repository=repository,
        mattermost_client=FakeMattermostClient(),
        jira_client=FakeJiraClient(),
    )


def _issue_replies(service, post_id, *, issue_key="OPS-1"):
    return [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post_id
        and (created["props"] or {}).get("jira_issue_key") == issue_key
    ]


def _issue_reply(service, post_id, *, issue_key="OPS-1"):
    replies = _issue_replies(service, post_id, issue_key=issue_key)
    assert len(replies) == 1
    return replies[0]


@pytest.mark.asyncio
async def test_issue_reply_has_action_buttons_when_public_url_set(settings):
    service = _build_service(replace(settings, service_public_url="https://bot.example.com/"))
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)

    # One message with two stacked blocks: the blue main block ("Создана задача"
    # notice + validity menu + incident/summary buttons) and a gray feedback block.
    assert reply["message"] == ""
    attachments = reply["props"]["attachments"]
    assert len(attachments) == 2

    # Block 1: the notice, validity menu, and incident/summary buttons, blue.
    controls_attachment = attachments[0]
    assert controls_attachment["color"] == "#3B82F6"
    assert "title" not in controls_attachment
    assert "title_link" not in controls_attachment
    assert controls_attachment["text"] == (
        "**Создана задача: [OPS-1](https://jira.example.com/browse/OPS-1)**"
    )
    controls_actions = controls_attachment["actions"]
    assert [action["id"] for action in controls_actions] == [
        "validity",
        "incident",
        "summary",
    ]
    validity = controls_actions[0]
    assert validity["integration"]["url"] == ("https://bot.example.com/mattermost/actions/alert")
    assert validity["integration"]["context"] == {
        "action": "validity",
        "alert_post_id": post.id,
    }
    assert validity["name"] == "Выбрать валидность ▼"
    assert validity["type"] == "select"
    assert validity["options"] == [
        {"text": "Ложный", "value": "false"},
        {"text": "Ожидаемый", "value": "expected"},
        {"text": "Валидный", "value": "valid"},
    ]
    assert controls_actions[1]["name"] == "🚨 Инцидент"
    assert controls_actions[1]["style"] == "primary"
    assert controls_actions[2]["name"] == "📝 Summary"
    assert controls_actions[2]["style"] == "default"

    # Block 2: feedback, in its own gray block below.
    feedback_attachment = attachments[1]
    assert feedback_attachment["color"] == "#4B5563"
    assert "text" not in feedback_attachment
    feedback_actions = feedback_attachment["actions"]
    assert [action["id"] for action in feedback_actions] == ["feedback"]
    assert feedback_actions[0]["name"] == "💬 Обратная связь по алерту"
    assert feedback_actions[0]["style"] == "default"
    assert feedback_actions[0]["integration"]["context"] == {
        "action": "feedback",
        "alert_post_id": post.id,
    }


@pytest.mark.asyncio
async def test_issue_reply_has_no_buttons_without_public_url(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)
    assert "Создана задача Jira" in reply["message"]
    assert "attachments" not in reply["props"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selected_option,expected_label",
    [
        ("false", "Ложный"),
        ("expected", "Ожидаемый"),
        ("valid", "Валидный"),
    ],
)
async def test_action_menu_sets_validity(service, selected_option, expected_label):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.handle_alert_action(
        action="validity",
        alert_post_id=post.id,
        user_id="clicker",
        selected_option=selected_option,
    )

    ticket = service.repository.get_by_post_id(post.id)
    assert service.jira.validity_updates == [("OPS-1", expected_label)]
    assert ticket.validity_label == expected_label
    assert ticket.valid_incident is False
    assert ticket.incident_post_id is None
    assert expected_label in result.message


@pytest.mark.asyncio
async def test_legacy_action_button_still_sets_validity(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.handle_alert_action(
        action="false", alert_post_id=post.id, user_id="clicker"
    )

    ticket = service.repository.get_by_post_id(post.id)
    assert service.jira.validity_updates == [("OPS-1", "Ложный")]
    assert ticket.validity_label == "Ложный"
    assert "Ложный" in result.message


@pytest.mark.asyncio
async def test_incident_button_confirms_incident(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.handle_alert_action(
        action="incident", alert_post_id=post.id, user_id="clicker"
    )

    ticket = service.repository.get_by_post_id(post.id)
    assert ticket.valid_incident is True
    assert ticket.incident_post_id is not None
    incident_posts = [
        created
        for created in service.mattermost.created_posts
        if created["channel_id"] == "incidents-channel"
    ]
    assert len(incident_posts) == 1
    assert "Инцидент заведён" in result.message


@pytest.mark.asyncio
async def test_incident_button_swaps_to_confirmed(settings):
    service = _build_service(replace(settings, service_public_url="https://bot.example.com"))
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.handle_alert_action(
        action="incident", alert_post_id=post.id, user_id="closer"
    )

    assert result.update_attachments is not None
    controls = result.update_attachments[0]
    names = [a.get("name") for a in controls["actions"]]
    assert "✅ Подтверждён" in names
    assert "🚨 Инцидент" not in names
    assert "📝 Summary" in names
    # Validity moves to the incident card, so the alert menu is gone after confirm.
    assert not any(a["id"] == "validity" for a in controls["actions"])
    # The alert thread gets the status notice with the incident-channel link.
    status_replies = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == post.id and "Ссылка на сообщение" in c["message"]
    ]
    assert len(status_replies) == 1


@pytest.mark.asyncio
async def test_summary_button_posts_thread_reply(service):
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.handle_alert_action(
        action="summary", alert_post_id=post.id, user_id="clicker"
    )

    assert len(service.llm.summary_prompts) == 1
    summary_replies = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post.id and "Саммари треда" in created["message"]
    ]
    assert len(summary_replies) == 1
    assert "всё сломалось" in summary_replies[0]["message"]
    assert "опубликовано" in result.message


@pytest.mark.asyncio
async def test_summary_button_without_llm_is_noop(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.handle_alert_action(
        action="summary", alert_post_id=post.id, user_id="clicker"
    )

    summary_replies = [
        created
        for created in service.mattermost.created_posts
        if "Саммари треда" in created["message"]
    ]
    assert summary_replies == []
    assert "LLM не настроен" in result.message


@pytest.mark.asyncio
async def test_feedback_button_opens_dialog(settings):
    service = _build_service(replace(settings, service_public_url="https://bot.example.com/"))
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.handle_alert_action(
        action="feedback",
        alert_post_id=post.id,
        user_id="clicker",
        trigger_id="trigger-1",
    )

    assert "Открыта форма" in result.message
    assert service.mattermost.opened_dialogs == [
        {
            "trigger_id": "trigger-1",
            "url": "https://bot.example.com/mattermost/dialogs/feedback",
            "dialog": {
                "callback_id": "alert_feedback",
                "title": "Обратная связь",
                "introduction_text": "Оставьте комментарий по этому алерту.",
                "elements": [
                    {
                        "display_name": "Комментарий",
                        "name": "feedback",
                        "type": "textarea",
                        "placeholder": "Что стоит улучшить?",
                        "max_length": 3000,
                    }
                ],
                "submit_label": "Отправить",
                "state": json.dumps({"alert_post_id": post.id}, ensure_ascii=False),
            },
        }
    ]
    assert service.jira.validity_updates == []


@pytest.mark.asyncio
async def test_feedback_dialog_submission_stores_feedback_and_posts_notice(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    service.mattermost.display_names["clicker"] = "@clicker"
    await service.handle_alert_post(post)

    result = await service.handle_feedback_dialog_submission(
        user_id="clicker",
        state=json.dumps({"alert_post_id": post.id}),
        submission={"feedback": "Кнопки стали понятнее"},
    )

    assert result.message == ""
    feedback = service.repository.list_feedback(post.id)
    assert len(feedback) == 1
    assert feedback[0].user_id == "clicker"
    assert feedback[0].user_display_name == "@clicker"
    assert feedback[0].message == "Кнопки стали понятнее"
    notices = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post.id
        and "Получили обратную связь от @clicker" in created["message"]
    ]
    assert len(notices) == 1


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
        action="incident", alert_post_id=post.id, user_id="u-bob"
    )

    assert "Недостаточно прав" in result.message
    assert service.jira.valid_updates == []


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
async def test_postmortem_checkmark_preserves_false_validity(service):
    """A confirmed incident marked Ложный keeps that validity after the PM checkmark.

    Validity (the Jira field value) and confirmation (valid_incident) are
    independent axes: the checkmark sets end-time + generates the postmortem but
    must not re-stamp Валидный over an explicit Ложный.
    """
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    # Confirm as incident (posts to incident channel, stamps Валидный once).
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    # Turns out false: set validity to Ложный via the lightweight reaction.
    await service.handle_reaction(
        ReactionEvent(
            post_id=post.id, user_id="validator", emoji_name="man_gesturing_no", create_at=2
        )
    )
    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"

    ticket = service.repository.get_by_post_id(post.id)
    result = await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="closer",
            emoji_name="white_check_mark",
            create_at=1_700_000_200_000,
        )
    )

    assert result.status == "incident_ended"
    # Postmortem ran (end-time set, report commented) ...
    assert service.jira.end_updates == [("OPS-1", datetime_from_mattermost_ms(1_700_000_200_000))]
    assert len(service.llm.prompts) == 1
    # ... but validity was NOT re-stamped Валидный: set_valid_incident stays the
    # single confirm-time call, and the field value remains Ложный.
    assert service.jira.valid_updates == [("OPS-1", True)]
    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"


def _manual_incident_ticket(service, *, issue_key="OPS-9", validity_label=None):
    root = MattermostPost(
        id="incidentroot00000000000001",
        channel_id="incidents-channel",
        user_id="human-user",
        message="Лежим в проде, 500-ки на /pay",
        create_at=1_700_000_000_000,
        channel_name="incidents",
    )
    service.repository.create_or_get_incident_thread(
        root, message_url=service.mattermost.permalink(root.id), channel_name="incidents"
    )
    service.repository.attach_jira_issue(
        root.id, issue_key, f"https://jira.example.com/browse/{issue_key}"
    )
    if validity_label is not None:
        service.repository.set_validity_label(root.id, validity_label)
    return service.repository.get_by_post_id(root.id)


@pytest.mark.asyncio
async def test_postmortem_preserves_explicit_false_validity_on_manual_ticket(service):
    """Manual incident (valid_incident stays False) marked Ложный keeps it after end/PM."""
    ticket = _manual_incident_ticket(service, validity_label="Ложный")
    assert ticket.valid_incident is False

    await service._ensure_postmortem_jira_issue(
        ticket,
        summary="summary",
        description="description",
        ended_at=datetime_from_mattermost_ms(1_700_000_300_000),
        reacted_by_user_id="closer",
    )

    # No Валидный stamp; end-time still set.
    assert service.jira.valid_updates == []
    assert service.jira.end_updates == [("OPS-9", datetime_from_mattermost_ms(1_700_000_300_000))]


@pytest.mark.asyncio
async def test_postmortem_defaults_to_valid_when_no_validity_chosen(service):
    """Without an explicit validity, the end/PM step still defaults to Валидный."""
    ticket = _manual_incident_ticket(service, validity_label=None)

    await service._ensure_postmortem_jira_issue(
        ticket,
        summary="summary",
        description="description",
        ended_at=datetime_from_mattermost_ms(1_700_000_300_000),
        reacted_by_user_id="closer",
    )

    assert service.jira.valid_updates == [("OPS-9", True)]


def _incident_service(settings):
    return _build_service(replace(settings, service_public_url="https://bot.example.com"))


def _manual_post(
    post_id="incidentroot00000000000002", *, user_id="human", props=None, root_id=None
):
    return MattermostPost(
        id=post_id,
        channel_id="incidents-channel",
        user_id=user_id,
        message="Лежим в проде, 500 на /pay",
        create_at=1_700_000_000_000,
        channel_name="incidents",
        root_id=root_id,
        props=props,
    )


@pytest.mark.asyncio
async def test_manual_incident_post_offers_create_button(settings):
    service = _incident_service(settings)
    post = _manual_post()
    service.mattermost.posts[post.id] = post

    await service.handle_manual_incident_post(post)

    replies = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    assert len(replies) == 1
    attachments = replies[0]["props"]["attachments"]
    assert attachments[0]["actions"][0]["id"] == "create_task"

    # Idempotent: a redelivered event does not post a second card.
    await service.handle_manual_incident_post(post)
    replies = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    assert len(replies) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "post",
    [
        _manual_post(user_id="bot-user"),
        _manual_post(props={"from_bot": "true"}),
        _manual_post(props={"from_webhook": "true"}),
        _manual_post(root_id="someroottttttttttttttttttt"),
    ],
)
async def test_manual_incident_ignores_bots_and_replies(settings, post):
    service = _incident_service(settings)
    await service.handle_manual_incident_post(post)
    assert service.mattermost.created_posts == []


@pytest.mark.asyncio
async def test_manual_incident_no_controls_without_public_url(settings):
    service = _build_service(settings)
    post = _manual_post()
    await service.handle_manual_incident_post(post)
    assert service.mattermost.created_posts == []


@pytest.mark.asyncio
async def test_manual_incident_card_pings_duty(settings):
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            mattermost_duty_mention=":look: @sre-ads-duty",
        )
    )
    post = _manual_post()
    service.mattermost.posts[post.id] = post

    await service.handle_manual_incident_post(post)

    card = next(c for c in service.mattermost.created_posts if c["root_id"] == post.id)
    # The mention lives in the message text (renders above the card) so the ping fires.
    assert card["message"] == ":look: @sre-ads-duty"
    assert card["props"]["attachments"][0]["actions"][0]["id"] == "create_task"


@pytest.mark.asyncio
async def test_incident_create_task_creates_jira_and_updates_card(settings):
    service = _incident_service(settings)
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)

    result = await service.handle_incident_action(
        action="create_task", incident_post_id=post.id, user_id="opener"
    )

    ticket = service.repository.get_by_incident_post_id(post.id)
    assert ticket.jira_issue_key == "OPS-1"
    assert result.update_attachments is not None
    card = result.update_attachments[0]
    # The Jira link lives in the main message, so the card carries no task text.
    assert "text" not in card
    ids = [a["id"] for a in card["actions"]]
    assert ids == ["validity", "end_incident", "summary"]


@pytest.mark.asyncio
async def test_manual_incident_false_then_end_preserves_validity(settings):
    """Manual flow: create task → Ложный → Завершение keeps Ложный and runs the PM."""
    service = _incident_service(settings)
    service.llm = FakeLlmClient()
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)
    await service.handle_incident_action(
        action="create_task", incident_post_id=post.id, user_id="opener"
    )

    await service.handle_incident_action(
        action="validity", incident_post_id=post.id, user_id="closer", selected_option="false"
    )
    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"

    result = await service.handle_incident_action(
        action="end_incident", incident_post_id=post.id, user_id="closer"
    )

    assert "завершён" in result.message.lower()
    assert len(service.llm.prompts) == 1
    # Postmortem set end-time but never re-stamped Валидный over the explicit Ложный.
    assert service.jira.valid_updates == []
    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"
    assert [key for key, _ in service.jira.end_updates] == ["OPS-1"]


@pytest.mark.asyncio
async def test_incident_summary_button_posts_light_summary(settings):
    service = _incident_service(settings)
    service.llm = FakeLlmClient()
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    service.repository.create_or_get_incident_thread(
        post, message_url=service.mattermost.permalink(post.id), channel_name="incidents"
    )

    result = await service.handle_incident_action(
        action="summary", incident_post_id=post.id, user_id="closer"
    )

    assert len(service.llm.summary_prompts) == 1
    assert "опубликовано" in result.message
    # Light summary does not touch Jira (no PM comment).
    assert service.jira.generic_comments == []


def test_endpoint_routes_incident_create_task(settings):
    service = _build_service(replace(settings, service_public_url="https://bot.example.com"))
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
    assert service.repository.get_by_incident_post_id(post.id).jira_issue_key == "OPS-1"


@pytest.mark.asyncio
async def test_pending_work_ignores_uncreated_manual_card(settings):
    """The pre-created card row stays keyless: the background loop must not
    auto-create a Jira issue for it (that would defeat the button gating)."""
    service = _incident_service(settings)
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)

    await service.process_pending_work()

    ticket = service.repository.get_by_incident_post_id(post.id)
    assert ticket.jira_issue_key is None
    assert service.jira.created_payloads == []


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
        if c["root_id"] == post.id and (c["props"] or {}).get("attachments")
    ]
    assert len(cards) == 1
    assert cards[0]["props"]["attachments"][0]["actions"][0]["id"] == "create_task"


@pytest.mark.asyncio
async def test_confirmed_alert_incident_gets_controls_card(settings):
    """An alert confirmed as an incident gets the same controls in the incident
    channel, but without "Создать задачу" (the Jira issue already exists)."""
    service = _incident_service(settings)
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )

    ticket = service.repository.get_by_post_id(post.id)
    assert ticket.incident_post_id is not None
    cards = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == ticket.incident_post_id and (c["props"] or {}).get("attachments")
    ]
    assert len(cards) == 1
    card = cards[0]["props"]["attachments"][0]
    ids = [a["id"] for a in card["actions"]]
    assert ids == ["validity", "end_incident", "summary"]
    # Alert-originated card shows the "Создана задача" header like the alert card.
    assert card["text"] == "**Создана задача: [OPS-1](https://jira.example.com/browse/OPS-1)**"


@pytest.mark.asyncio
async def test_incident_card_validity_on_alert_incident(settings):
    """Validity from the incident card resolves the alert-originated ticket
    (incident_post_id != mattermost_post_id) and sets the Jira field."""
    service = _incident_service(settings)
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    ticket = service.repository.get_by_post_id(post.id)
    assert ticket.incident_post_id != ticket.mattermost_post_id

    result = await service.handle_incident_action(
        action="validity",
        incident_post_id=ticket.incident_post_id,
        user_id="closer",
        selected_option="false",
    )

    assert "Ложный" in result.message
    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"


@pytest.mark.asyncio
async def test_completing_alert_incident_updates_title_to_done(settings):
    service = _incident_service(settings)
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    ticket = service.repository.get_by_post_id(post.id)
    incident_post_id = ticket.incident_post_id

    def info_text():
        return service.mattermost.posts[incident_post_id].props["attachments"][0]["text"]

    assert "##### 🔴 Инцидент открыт" in info_text()

    await service.handle_incident_action(
        action="end_incident", incident_post_id=incident_post_id, user_id="closer"
    )

    assert "##### 🟢 Инцидент закрыт" in info_text()
    assert "##### 🔴 Инцидент открыт" not in info_text()


@pytest.mark.asyncio
async def test_end_button_swaps_to_completed(settings):
    service = _incident_service(settings)
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    ticket = service.repository.get_by_post_id(post.id)

    result = await service.handle_incident_action(
        action="end_incident", incident_post_id=ticket.incident_post_id, user_id="closer"
    )

    assert result.update_attachments is not None
    actions = result.update_attachments[0]["actions"]
    names = [a.get("name") for a in actions]
    assert "✅ Завершено" in names
    assert "🏁 Завершить" not in names
    # Validity menu and summary remain after completion.
    assert any(a["id"] == "validity" for a in actions)
    assert "📝 Саммари" in names


def test_incident_and_end_buttons_require_confirmation():
    from mm_jira_bot.actions import (
        build_alert_controls_attachment,
        build_incident_controls_attachment,
    )

    alert = build_alert_controls_attachment(
        title="OPS-1", title_link="u", alert_post_id="p", callback_url="http://x/cb"
    )
    incident_btn = next(a for a in alert["actions"] if a.get("id") == "incident")
    assert incident_btn["confirm"]["ok_text"] == "Завести"

    inc = build_incident_controls_attachment(incident_post_id="p", callback_url="http://x/cb")
    end_btn = next(a for a in inc["actions"] if a.get("id") == "end_incident")
    assert end_btn["confirm"]["ok_text"] == "Завершить"
    # Summary stays a plain one-click button.
    summary_btn = next(a for a in inc["actions"] if a.get("id") == "summary")
    assert "confirm" not in summary_btn
