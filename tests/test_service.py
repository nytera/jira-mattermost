from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

import mm_jira_bot.jira as jira_module
import mm_jira_bot.jira_payload as jira_payload_module
from mm_jira_bot.actions import (
    DUTY_HELP_ATTACHMENT_COLOR,
    NOTICE_ATTACHMENT_COLOR,
    OPS_ALERT_COLOR,
)
from mm_jira_bot.config import Settings, _csv_env, load_dotenv_file
from mm_jira_bot.domain import (
    ConfirmationResult,
    JiraIssue,
    MattermostPost,
    ReactionEvent,
    datetime_from_mattermost_ms,
)
from mm_jira_bot.formatting import (
    alert_signature,
    format_alert_duty_help,
    format_incident_duty_help,
    format_incident_message,
)
from mm_jira_bot.jira_payload import build_jira_issue_payload
from mm_jira_bot.llm import PostmortemLlmClient
from mm_jira_bot.logging import get_logger
from mm_jira_bot.mattermost import MattermostClient
from mm_jira_bot.metrics import TicketStatsCollector, errors_total
from mm_jira_bot.ops import OpsLogHandler, OpsNotifier
from mm_jira_bot.postmortem import (
    DEFAULT_POSTMORTEM_PROMPT,
    DEFAULT_SUMMARY_PROMPT,
    build_incident_report_prompt,
    build_postmortem_comment,
    extract_postmortem_summary,
    format_incident_closed_notice,
    markdown_to_jira_wiki,
)
from mm_jira_bot.repository import (
    AlertTicketRepository,
    create_database_engine,
    create_session_factory,
    init_db,
)
from mm_jira_bot.retry import ApiError
from mm_jira_bot.service import IncidentBotService, parse_post_id_from_text
from mm_jira_bot.summary import (
    format_thread_summary_reply,
    format_thread_summary_streaming,
    neutralize_mentions,
)
from mm_jira_bot.web import create_app, run_startup_preflight

POST_ID = "abcdefghijklmnopqrstuvwx01"


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    return cast(dict[str, object], cast(Any, record).extra_fields)


class FakeMattermostClient:
    def __init__(self) -> None:
        self.posts: dict[str, MattermostPost] = {}
        self.created_posts: list[dict] = []
        self.opened_dialogs: list[dict] = []
        self.updated_posts: list[dict] = []
        self.display_names: dict[str, str] = {}
        self.username_to_id: dict[str, str] = {}
        self.usernames_lookups: list[list[str]] = []
        self.group_name_to_id: dict[str, str] = {}
        self.group_members: dict[str, set[str]] = {}
        self.group_lookups: list[list[str]] = []
        self.reactions: list[tuple[str, str]] = []

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        self.reactions.append((post_id, emoji_name))

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

    async def get_group_ids_by_names(self, names: list[str]) -> dict[str, str]:
        self.group_lookups.append(list(names))
        return {
            name: self.group_name_to_id[name] for name in names if name in self.group_name_to_id
        }

    async def get_group_member_ids(self, group_id: str) -> set[str]:
        return set(self.group_members.get(group_id, set()))

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
        self.time_to_fix_updates: list[tuple[str, int]] = []
        self.validity_by_issue: dict[str, str] = {}
        self.descriptions: list[tuple[str, str]] = []
        self.postmortem_payloads: list[dict] = []
        self.generic_comments: list[tuple[str, str]] = []
        self.links: list[tuple[str, str]] = []

    async def link_child_of(self, child_key: str, parent_key: str) -> None:
        self.links.append((child_key, parent_key))

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

    async def set_time_to_fix(self, issue_key: str, minutes: int):
        self.time_to_fix_updates.append((issue_key, minutes))

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

    async def generate_summary(self, prompt: str, *, on_progress=None) -> str:
        self.summary_prompts.append(prompt)
        if on_progress is not None:
            await on_progress(self.summary)
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
    is_bot: bool = True,
) -> MattermostPost:
    post_props = {"from_webhook": "true"} if is_bot else {}
    if props:
        post_props.update(props)
    return MattermostPost(
        id=post_id,
        channel_id=channel_id,
        user_id="author-user",
        message=message,
        create_at=1_700_000_000_000,
        channel_name="alerts",
        props=post_props,
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
async def test_creates_jira_issue_for_new_mattermost_message(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is not None
    assert ticket.jira_issue_key == "OPS-1"
    assert ticket.mattermost_alert_title == "CPU usage is above 95%"
    assert len(service.jira.created_payloads) == 1


@pytest.mark.asyncio
async def test_ignores_human_message_in_alert_channel(service):
    post = make_alert(post_id="humanalertpost00000000001", is_bot=False)
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is None
    assert service.repository.get_by_post_id(post.id) is None
    assert service.jira.created_payloads == []


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
            interactive_buttons_enabled=True,
        )
    )
    post = make_alert()
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is not None
    assert ticket.jira_issue_key == f"ADSDEV-12024-{post.id[:12]}"
    assert ticket.jira_issue_url == (f"https://jira.example.com/browse/ADSDEV-12024-{post.id[:12]}")
    assert len(service.jira.created_payloads) == 0
    assert ticket.jira_issue_key is not None
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
    # Distinct messages → distinct signatures, so both are independent root
    # alerts (not a root + expected repeat) and each gets its own stub key.
    first_post = make_alert(post_id="firststubalertpost00000001", message="Disk above 90%")
    second_post = make_alert(post_id="secondstubalertpost0000002", message="Memory above 80%")
    service.mattermost.posts[first_post.id] = first_post
    service.mattermost.posts[second_post.id] = second_post

    first_ticket = await service.handle_alert_post(first_post)
    second_ticket = await service.handle_alert_post(second_post)

    assert first_ticket is not None
    assert second_ticket is not None
    assert first_ticket.jira_issue_key != second_ticket.jira_issue_key
    assert len(service.jira.created_payloads) == 0
    assert first_ticket.jira_issue_key is not None
    assert second_ticket.jira_issue_key is not None
    first_reply = _issue_reply(service, first_post.id, issue_key=first_ticket.jira_issue_key)
    second_reply = _issue_reply(service, second_post.id, issue_key=second_ticket.jira_issue_key)
    assert "Создана задача Jira: [ADSDEV-12024]" in _reply_text(first_reply)
    assert "Создана задача Jira: [ADSDEV-12024]" in _reply_text(second_reply)


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


def test_alert_signature_symmetry_link_vs_plain():
    firing = "🔴 [DiskFull](https://grafana.wb.ru/alerting/grafana/abc123/view)\nState: Alerting"
    resolve = "✅ DiskFull"
    assert alert_signature(firing) == alert_signature(resolve)


def test_alert_signature_symmetry_plain():
    assert alert_signature("🔴 CPU usage is above 95%") == alert_signature(
        "✅ CPU usage is above 95%"
    )


@pytest.mark.asyncio
async def test_first_firing_is_root(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket.root_post_id is None
    assert service.mattermost.reactions == []
    assert service.jira.links == []
    assert service.jira.descriptions == []


@pytest.mark.asyncio
async def test_repeat_firing_marked_expected(service):
    root_post = make_alert(post_id="rootpost00000000000000001")
    repeat_post = make_alert(post_id="repeatpost000000000000001")
    for post in (root_post, repeat_post):
        service.mattermost.posts[post.id] = post

    await service.handle_alert_post(root_post)
    await service.handle_alert_post(repeat_post)

    root = service.repository.get_by_post_id(root_post.id)
    repeat = service.repository.get_by_post_id(repeat_post.id)
    assert repeat.root_post_id == root_post.id
    assert repeat.jira_issue_key == "OPS-2"
    # Reaction lands on the repeat message, not the root.
    assert (repeat_post.id, "arrows_counterclockwise") in service.mattermost.reactions
    # Validity "Ожидаемый" on the repeat issue.
    assert ("OPS-2", "Ожидаемый") in service.jira.validity_updates
    assert repeat.validity_label == "Ожидаемый"
    # Description carries the root links.
    description = dict(service.jira.descriptions)["OPS-2"]
    assert root.mattermost_message_url in description
    assert "OPS-1" in description
    # A real Jira "is child of" link from repeat to root.
    assert ("OPS-2", "OPS-1") in service.jira.links
    # And a repeat-alert notice in the thread.
    assert any(
        _reply_text(reply)
        == (
            ":arrows_counterclockwise: **Повторный алерт**\n"
            "Тикет прилинкован к [корневой задаче]"
            "(https://jira.example.com/browse/OPS-1) "
            "(корневая задача первого алерта).\n"
            f"[Корневой алерт]({root.mattermost_message_url})"
        )
        for reply in service.mattermost.created_posts
    )


@pytest.mark.asyncio
async def test_repeat_firing_skips_duty_ping_and_cheat_sheet(settings):
    # A repeat firing is auto-marked expected, so the on-call ping and the duty
    # cheat-sheet are suppressed in its thread — only the root thread gets them.
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com/",
            interactive_buttons_enabled=True,
            mattermost_duty_mention=":look: @sre-ads-duty",
        )
    )
    root_post = make_alert(post_id="rootpost00000000000000010")
    repeat_post = make_alert(post_id="repeatpost000000000000010")
    for post in (root_post, repeat_post):
        service.mattermost.posts[post.id] = post

    await service.handle_alert_post(root_post)
    await service.handle_alert_post(repeat_post)

    # Root thread: duty pinged above the box, cheat-sheet posted.
    assert _issue_reply(service, root_post.id, issue_key="OPS-1")["message"] == (
        ":look: @sre-ads-duty"
    )
    assert any(
        c["root_id"] == root_post.id and "Памятка дежурному" in _reply_text(c)
        for c in service.mattermost.created_posts
    )

    # Repeat thread: "Создана задача" box stays, but no duty ping and no cheat-sheet.
    repeat_replies = [c for c in service.mattermost.created_posts if c["root_id"] == repeat_post.id]
    box = [c for c in repeat_replies if "Создана задача" in _reply_text(c)]
    assert len(box) == 1
    assert box[0]["message"] == ""
    assert all("@sre-ads-duty" not in _reply_text(c) for c in repeat_replies)
    assert all("Памятка дежурному" not in _reply_text(c) for c in repeat_replies)


@pytest.mark.asyncio
async def test_resolve_closes_episode_creates_nothing(service):
    root_post = make_alert(post_id="rootpost00000000000000002")
    repeat_post = make_alert(post_id="repeatpost000000000000002")
    resolve_post = make_alert(
        post_id="resolvepost00000000000002", message="✅ CPU usage is above 95%"
    )
    for post in (root_post, repeat_post, resolve_post):
        service.mattermost.posts[post.id] = post

    await service.handle_alert_post(root_post)
    await service.handle_alert_post(repeat_post)
    issues_before = len(service.jira.created_payloads)
    result = await service.handle_alert_post(resolve_post)

    assert result is None
    assert len(service.jira.created_payloads) == issues_before
    assert service.repository.get_by_post_id(resolve_post.id) is None
    assert service.repository.get_by_post_id(root_post.id).resolved_at is not None


@pytest.mark.asyncio
async def test_refiring_after_resolve_becomes_new_root(service):
    first = make_alert(post_id="firstpost0000000000000001")
    resolve = make_alert(post_id="resolvepost00000000000003", message="✅ CPU usage is above 95%")
    second = make_alert(post_id="secondpost000000000000001")
    for post in (first, resolve, second):
        service.mattermost.posts[post.id] = post

    await service.handle_alert_post(first)
    await service.handle_alert_post(resolve)
    reactions_before = list(service.mattermost.reactions)
    await service.handle_alert_post(second)

    assert service.repository.get_by_post_id(second.id).root_post_id is None
    assert service.mattermost.reactions == reactions_before


@pytest.mark.asyncio
async def test_repeat_redelivery_no_duplicate_link(service):
    root_post = make_alert(post_id="rootpost00000000000000003")
    repeat_post = make_alert(post_id="repeatpost000000000000003")
    for post in (root_post, repeat_post):
        service.mattermost.posts[post.id] = post

    await service.handle_alert_post(root_post)
    await service.handle_alert_post(repeat_post)
    await service.handle_alert_post(repeat_post)

    assert service.jira.links.count(("OPS-2", "OPS-1")) == 1
    notices = [
        reply
        for reply in service.mattermost.created_posts
        if "Повторный алерт" in _reply_text(reply)
    ]
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_bot_own_expected_reaction_is_ignored(service):
    # The bot's own "Ожидаемый" reaction echoes back over the websocket; it must
    # not re-enter the validity path or post an unauthorized notice.
    result = await service.handle_reaction(
        ReactionEvent(
            post_id="anypost", user_id="bot-user", emoji_name="arrows_counterclockwise", create_at=1
        )
    )
    assert result.status.name == "IGNORED"
    assert service.jira.validity_updates == []
    assert service.mattermost.created_posts == []


@pytest.mark.asyncio
async def test_resolve_without_active_root_is_noop(service):
    resolve_post = make_alert(
        post_id="resolvepost00000000000004", message="✅ CPU usage is above 95%"
    )
    service.mattermost.posts[resolve_post.id] = resolve_post

    result = await service.handle_alert_post(resolve_post)

    assert result is None
    assert len(service.jira.created_payloads) == 0


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
        if created["channel_id"] == "incidents-channel" and created["root_id"] is None
    ]
    assert len(incident_posts) == 1
    incident_post = incident_posts[0]
    post_attachments = incident_post["props"]["attachments"]
    # Red info block first, then the forwarded alert attachment(s) (a copy).
    info_block = post_attachments[0]
    assert info_block["color"] == "#EF4444"
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
    assert "## Извлечённые уроки" in prompt
    assert "### Что было сделано хорошо / В чём повезло" in prompt
    assert "### Что пошло не так / В чём не повезло" in prompt
    assert "## Action Items (на обсуждение)" in prompt
    assert "Action Items — это предложения на обсуждение" in prompt
    assert "до 10 слов и до 80 символов" in prompt
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
    # The comment is converted to Jira wiki markup (Markdown headings → h2.).
    assert "h2. Хронология" in comment
    assert "##Хронология" not in comment
    # The incident thread gets the fact-based summary (own LLM prompt), posted as
    # a "Генерация саммари…" placeholder that is then edited into the final reply.
    placeholders = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == ticket.incident_post_id
        and "Генерация саммари" in _reply_text(created)
    ]
    assert len(placeholders) == 1
    # The placeholder is edited through the 1/3·2/3·3/3 status steps, then into the
    # final summary (last edit).
    edits = [
        u for u in service.mattermost.updated_posts if u["post_id"] == placeholders[0]["post"].id
    ]
    assert any("Шаг 1/3" in _reply_text(u) for u in edits)
    final = edits[-1]
    assert "Саммари треда" in _reply_text(final)
    assert "всё сломалось" in _reply_text(final)
    # The Jira link no longer rides inside the summary; closure is announced in a
    # standalone green box posted as a separate reply.
    assert "Полный постмортем отправлен в Jira" not in _reply_text(final)
    closed = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == ticket.incident_post_id
        and "Инцидент закрыт" in _reply_text(created)
    ]
    assert len(closed) == 1
    assert "ПМ:" in _reply_text(closed[0])
    assert "INC" in _reply_text(closed[0])  # link text is the task title (brackets escaped)
    assert closed[0]["props"]["attachments"][0]["color"] == "#22C55E"


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
    placeholders = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == root.id and "Генерация саммари" in _reply_text(created)
    ]
    assert len(placeholders) == 1
    edits = [
        u for u in service.mattermost.updated_posts if u["post_id"] == placeholders[0]["post"].id
    ]
    assert any("Шаг 1/3" in _reply_text(u) for u in edits)
    final = edits[-1]
    assert "Саммари треда" in _reply_text(final)
    assert "Полный постмортем отправлен в Jira" not in _reply_text(final)
    closed = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == root.id and "Инцидент закрыт" in _reply_text(created)
    ]
    assert len(closed) == 1
    assert "ПМ: [\\[INC\\] 15.11.2023 - Ошибки API]" in _reply_text(closed[0])
    assert closed[0]["props"]["attachments"][0]["color"] == "#22C55E"


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


def test_format_thread_summary_streaming_marks_in_progress():
    rendered = format_thread_summary_streaming("  частичный текст  ")
    assert rendered.startswith("📝 **Саммари треда** _(генерируется…)_")
    assert "частичный текст" in rendered
    assert rendered.endswith("частичный текст")  # surrounding whitespace trimmed


def test_format_incident_closed_notice_links_task_by_title():
    notice = format_incident_closed_notice(
        jira_issue_title="[INC] 15.11.2023 - Ошибки API",
        jira_issue_url="https://jira.example.com/browse/OPS-1",
    )
    # Brackets in the title are escaped so the leading "[INC]" does not nest inside
    # the markdown link text and break rendering.
    assert notice == (
        "🟢 **Инцидент закрыт**\n"
        "ПМ: [\\[INC\\] 15.11.2023 - Ошибки API](https://jira.example.com/browse/OPS-1)"
    )


def test_format_incident_closed_notice_degrades_without_url():
    notice = format_incident_closed_notice(jira_issue_title="OPS-1", jira_issue_url=None)
    assert notice == "🟢 **Инцидент закрыт**\nПМ: OPS-1"


@pytest.mark.asyncio
async def test_summary_stream_callback_throttles_edits(service, monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr("mm_jira_bot.service.coordinator.perf_counter", lambda: clock["t"])
    edits: list[str] = []

    async def _record(reply_id, post_id, *, message, base_props, event):
        edits.append(message)

    monkeypatch.setattr(service, "_edit_summary_reply", _record)
    on_progress = service._make_summary_stream_callback(
        reply_id="reply1", post_id="root1", base_props={}, event="evt"
    )

    # Below both thresholds (≈10 chars, 0.5s after the seed) → no edit yet.
    clock["t"] = 100.5
    await on_progress("a" * 10)
    assert edits == []
    # Crossing the char threshold (≥80 new chars) forces an edit.
    clock["t"] = 100.6
    await on_progress("a" * 100)
    assert len(edits) == 1
    assert "генерируется" in edits[0]
    # Crossing the time threshold (≥1.5s) forces the next edit.
    clock["t"] = 102.5
    await on_progress("a" * 120)
    assert len(edits) == 2
    # A shrinking buffer (retry restart) forces a re-render regardless of thresholds.
    clock["t"] = 102.6
    await on_progress("x" * 5)
    assert len(edits) == 3


class _FakeStreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


@pytest.mark.asyncio
async def test_collect_stream_emits_cumulative_progress(settings):
    # End-to-end wiring of the SSE collector: each delta fires on_progress with the
    # CUMULATIVE text, and the joined result is returned.
    client = PostmortemLlmClient(settings, http_client=httpx.AsyncClient())
    lines = [
        'data: {"choices":[{"delta":{"content":"Привет"}}]}',
        "",  # non-data line is skipped
        'data: {"choices":[{"delta":{"content":", "}}]}',
        'data: {"choices":[{"delta":{"content":"мир"}}]}',
        "data: [DONE]",
    ]
    seen: list[str] = []

    async def record(text: str) -> None:
        seen.append(text)

    try:
        result = await client._collect_stream(_FakeStreamResponse(lines), on_progress=record)
    finally:
        await client.aclose()

    assert result == "Привет, мир"
    assert seen == ["Привет", "Привет, ", "Привет, мир"]  # cumulative, per delta


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
        if created["channel_id"] == "incidents-channel" and created["root_id"] is None
    ]
    assert incident_posts == []
    validity_replies = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post.id and "Ложный" in _reply_text(created)
    ]
    assert len(validity_replies) == 1


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
        if created["root_id"] == post.id and "Валидность" in _reply_text(created)
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
async def test_checkmark_in_alert_channel_creates_no_issue(service):
    # A checkmark belongs to the incident channel; in the alert channel it must be
    # ignored and must NOT create a Jira issue as a side effect.
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="v", emoji_name="white_check_mark", create_at=1)
    )

    assert result.status == "ignored"
    assert service.jira.created_payloads == []
    assert service.repository.get_by_post_id(post.id) is None


@pytest.mark.asyncio
async def test_replies_in_alert_thread_when_issue_created(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)
    assert reply["channel_id"] == "alerts-channel"
    assert "OPS-1" in _reply_text(reply)
    assert "https://jira.example.com/browse/OPS-1" in _reply_text(reply)


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
    # Issue-created reply + duty cheat-sheet + one status-change reply; no
    # duplicate status reply on retry.
    status_replies = [r for r in thread_replies if "Инцидент заведен" in _reply_text(r)]
    assert len(status_replies) == 1
    status_reply = status_replies[0]
    assert status_reply["channel_id"] == "alerts-channel"
    # The notice renders as a boxed attachment, not a bare message: text moves
    # into a single NOTICE-colored block with fallback set for push/preview.
    assert status_reply["message"] == ""
    box = status_reply["props"]["attachments"][0]
    assert box["color"] == NOTICE_ATTACHMENT_COLOR
    assert "Инцидент заведен" in box["text"]
    assert "Ссылка на инцидент" in box["text"]
    assert box["fallback"]

    incident_posts = [
        created
        for created in service.mattermost.created_posts
        if created["channel_id"] == "incidents-channel" and created["root_id"] is None
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


def _capture_bot_logs(records: list[logging.LogRecord]):
    """Attach a record collector to ``mm_jira_bot``.

    ``create_app`` runs ``configure_logging`` which clears root handlers (and so
    pytest's ``caplog``), so capture on the bot logger after the app is built.
    """
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("mm_jira_bot")
    logger.addHandler(handler)
    return logger, handler


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
        if created["channel_id"] == "incidents-channel" and created["root_id"] is None
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
        if created["channel_id"] == "incidents-channel" and created["root_id"] is None
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


def _reply_text(reply):
    """Visible text of a bot thread reply, whether it is a bare message or a
    boxed attachment notice (plain notices render as a single colored block)."""
    if reply.get("message"):
        return reply["message"]
    attachments = (reply.get("props") or {}).get("attachments") or []
    return attachments[0].get("text", "") if attachments else ""


@pytest.mark.asyncio
async def test_issue_reply_has_action_buttons_when_public_url_set(settings):
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com/",
            interactive_buttons_enabled=True,
        )
    )
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
    # Boxed notice (no SERVICE_PUBLIC_URL), but no interactive button/menu controls.
    assert "Создана задача Jira" in _reply_text(reply)
    assert all("actions" not in a for a in reply["props"].get("attachments", []))


@pytest.mark.asyncio
async def test_issue_reply_has_no_buttons_when_interactive_buttons_disabled(settings):
    # Emoji-only mode: SERVICE_PUBLIC_URL is set but the toggle is off, so the
    # issue-created reply degrades to the plain text fallback (no cards).
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=False,
        )
    )
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)
    # Emoji-only mode still boxes the notice, just without interactive controls.
    assert "Создана задача Jira" in _reply_text(reply)
    assert all("actions" not in a for a in reply["props"].get("attachments", []))


@pytest.mark.asyncio
async def test_firing_alert_pings_duty_above_box_with_buttons(settings):
    # Interactive mode: the duty @mention is a bare message above the box so the
    # ping fires; the "Создана задача" notice stays inside the attachment.
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com/",
            interactive_buttons_enabled=True,
            mattermost_duty_mention=":look: @sre-ads-duty",
        )
    )
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)
    assert reply["message"] == ":look: @sre-ads-duty"
    assert reply["props"]["attachments"][0]["text"] == (
        "**Создана задача: [OPS-1](https://jira.example.com/browse/OPS-1)**"
    )


@pytest.mark.asyncio
async def test_firing_alert_pings_duty_above_box_emoji_only(settings):
    # Emoji-only mode: notice is boxed, duty @mention stays bare above it.
    service = _build_service(replace(settings, mattermost_duty_mention=":look: @sre-ads-duty"))
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)
    assert reply["message"] == ":look: @sre-ads-duty"
    assert "Создана задача Jira" in reply["props"]["attachments"][0]["text"]


@pytest.mark.asyncio
async def test_firing_alert_no_duty_ping_when_unset(service):
    # No MATTERMOST_DUTY_MENTION → message stays empty (no regression).
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)
    assert reply["message"] == ""


@pytest.mark.asyncio
async def test_manual_incident_no_card_when_interactive_buttons_disabled(settings):
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=False,
        )
    )
    post = _manual_post()
    service.mattermost.posts[post.id] = post

    await service.handle_manual_incident_post(post)

    replies = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    # Emoji-only mode: only the duty cheat-sheet notice, no interactive create-task card.
    assert len(replies) == 1
    assert "Памятка дежурному" in _reply_text(replies[0])
    assert all("actions" not in a for a in (replies[0]["props"] or {}).get("attachments", []))


@pytest.mark.asyncio
async def test_manual_incident_pings_duty_in_emoji_only_mode(settings):
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=False,
            mattermost_duty_mention=":look: @sre-ads-duty",
        )
    )
    post = _manual_post()
    service.mattermost.posts[post.id] = post

    await service.handle_manual_incident_post(post)

    replies = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    # Bare duty @mention first, then the duty cheat-sheet notice.
    assert len(replies) == 2
    assert replies[0]["message"] == ":look: @sre-ads-duty"
    assert not (replies[0]["props"] or {}).get("attachments")
    assert "Памятка дежурному" in _reply_text(replies[1])

    # Idempotent: a redelivered event does not post a second ping or cheat-sheet.
    await service.handle_manual_incident_post(post)
    replies = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    assert len(replies) == 2


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
        if created["channel_id"] == "incidents-channel" and created["root_id"] is None
    ]
    assert len(incident_posts) == 1
    assert "Инцидент заведён" in result.message


@pytest.mark.asyncio
async def test_incident_button_swaps_to_confirmed(settings):
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=True,
        )
    )
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
        if c["root_id"] == post.id and "Ссылка на инцидент" in _reply_text(c)
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
    # The bot first posts a "Генерация саммари…" placeholder, then edits it in place.
    placeholders = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post.id and "Генерация саммари" in _reply_text(created)
    ]
    assert len(placeholders) == 1
    updates = [
        u for u in service.mattermost.updated_posts if u["post_id"] == placeholders[0]["post"].id
    ]
    assert len(updates) == 1
    assert "Саммари треда" in _reply_text(updates[0])
    assert "всё сломалось" in _reply_text(updates[0])
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
        if "Саммари треда" in _reply_text(created)
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
        and "Получили обратную связь от @clicker" in _reply_text(created)
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
    return _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=True,
        )
    )


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
    # The create-task card plus the duty cheat-sheet notice below it.
    assert len(replies) == 2
    card = next(r for r in replies if (r["props"] or {}).get("attachments", [{}])[0].get("actions"))
    assert card["props"]["attachments"][0]["actions"][0]["id"] == "create_task"

    # Idempotent: a redelivered event does not post a second card or cheat-sheet.
    await service.handle_manual_incident_post(post)
    replies = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    assert len(replies) == 2


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
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)
    replies = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    # No SERVICE_PUBLIC_URL → no create-task card, only the duty cheat-sheet notice.
    assert len(replies) == 1
    assert "Памятка дежурному" in _reply_text(replies[0])
    assert all("actions" not in a for a in (replies[0]["props"] or {}).get("attachments", []))


@pytest.mark.asyncio
async def test_manual_incident_card_pings_duty(settings):
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=True,
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
    assert ticket is not None
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
async def test_pending_work_ignores_uncreated_manual_card(settings):
    """The pre-created card row stays keyless: the background loop must not
    auto-create a Jira issue for it (that would defeat the button gating)."""
    service = _incident_service(settings)
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)

    await service.process_pending_work()

    ticket = service.repository.get_by_incident_post_id(post.id)
    assert ticket is not None
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
    assert ticket is not None
    assert ticket.incident_post_id is not None
    cards = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == ticket.incident_post_id
        and any(a.get("actions") for a in (c["props"] or {}).get("attachments", []))
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
    assert ticket is not None
    assert ticket.incident_post_id != ticket.mattermost_post_id
    assert ticket.incident_post_id is not None

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
    assert ticket is not None
    assert ticket.incident_post_id is not None
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
    assert ticket is not None
    assert ticket.incident_post_id is not None

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


# --- Duty cheat-sheet -------------------------------------------------------


def test_alert_duty_help_lists_configured_reactions():
    text = format_alert_duty_help(
        incident_emoji="incident",
        false_emoji="man_gesturing_no",
        expected_emoji="arrows_counterclockwise",
        summary_emoji="memo",
    )
    assert ":incident:" in text
    assert ":man_gesturing_no:" in text
    assert ":arrows_counterclockwise:" in text
    assert ":memo:" in text
    assert "завести инцидент" in text
    assert "саммари" in text.lower()
    # No button hints, as buttons are off by default.
    assert "кнопк" not in text.lower()


def test_alert_duty_help_uses_custom_emoji_names():
    text = format_alert_duty_help(
        incident_emoji="fire",
        false_emoji="no_entry",
        expected_emoji="repeat",
        summary_emoji="scroll",
    )
    assert ":fire:" in text and ":no_entry:" in text and ":repeat:" in text and ":scroll:" in text


def test_incident_duty_help_lists_checkmark_reaction():
    text = format_incident_duty_help(
        false_emoji="man_gesturing_no",
        expected_emoji="arrows_counterclockwise",
        summary_emoji="memo",
    )
    assert "✅" in text
    # Validity reactions here close the incident + postmortem (not the alert's
    # label-only meaning), and the summary emoji is listed too.
    assert ":man_gesturing_no:" in text and ":arrows_counterclockwise:" in text
    assert ":memo:" in text
    assert "постмортем" in text.lower()
    assert "кнопк" not in text.lower()


@pytest.mark.asyncio
async def test_firing_alert_posts_duty_help(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    help_replies = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == post.id and "Памятка дежурному" in _reply_text(c)
    ]
    assert len(help_replies) == 1
    assert ":incident:" in _reply_text(help_replies[0])
    assert help_replies[0]["props"]["attachments"][0]["color"] == DUTY_HELP_ATTACHMENT_COLOR


@pytest.mark.asyncio
async def test_manual_incident_posts_duty_help(service):
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)
    help_replies = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == post.id and "Памятка дежурному" in _reply_text(c)
    ]
    assert len(help_replies) == 1
    assert "✅" in _reply_text(help_replies[0])


@pytest.mark.asyncio
async def test_duty_help_disabled_posts_nothing(settings):
    service = _build_service(replace(settings, duty_help_enabled=False))
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    assert not [
        c for c in service.mattermost.created_posts if "Памятка дежурному" in _reply_text(c)
    ]


@pytest.mark.asyncio
async def test_alert_originated_incident_posts_its_own_duty_help(service):
    # The incident-channel thread (alert-originated) gets its own cheat-sheet:
    # validity reactions here mean "close + postmortem", unlike the alert thread.
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    ticket = service.repository.get_by_post_id(post.id)
    incident_help = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == ticket.incident_post_id and "Памятка дежурному" in _reply_text(c)
    ]
    assert len(incident_help) == 1
    assert "постмортем" in _reply_text(incident_help[0]).lower()


# --- Postmortem comment Jira-wiki conversion --------------------------------


def test_markdown_to_jira_wiki_headings_bullets_bold_links():
    assert markdown_to_jira_wiki("## Сводка") == "h2. Сводка"
    assert markdown_to_jira_wiki("##Сводка") == "h2. Сводка"
    assert markdown_to_jira_wiki("### Уроки") == "h3. Уроки"
    assert markdown_to_jira_wiki("- пункт") == "* пункт"
    assert markdown_to_jira_wiki(" - пункт") == "* пункт"
    assert markdown_to_jira_wiki("**жирный**") == "*жирный*"
    assert markdown_to_jira_wiki("[OPS-1](https://j/OPS-1)") == "[OPS-1|https://j/OPS-1]"


def test_markdown_to_jira_wiki_converts_mentions_but_not_emails():
    assert markdown_to_jira_wiki("12:14 — @aminov.pavel3 откат") == "12:14 — [~aminov.pavel3] откат"
    assert markdown_to_jira_wiki("@ivanov и @petrov") == "[~ivanov] и [~petrov]"
    # A trailing sentence period is not swallowed into a dotted username.
    assert markdown_to_jira_wiki("откатил @aminov.pavel3.") == "откатил [~aminov.pavel3]."
    # Emails must stay intact (the char before @ is a word char).
    assert markdown_to_jira_wiki("write to user@host.ru") == "write to user@host.ru"


def test_markdown_to_jira_wiki_is_idempotent_on_wiki():
    already = "h2. Сводка\n* пункт\n*жирный*\n[OPS-1|https://j/OPS-1]\n[~ivanov]"
    assert markdown_to_jira_wiki(already) == already
    once = markdown_to_jira_wiki("## X\n- y\n**z**\n@ivanov")
    assert markdown_to_jira_wiki(once) == once


def test_thread_summary_strips_mentions_so_it_never_pings():
    assert neutralize_mentions("12:14 — @aminov.pavel3 откат") == "12:14 — aminov.pavel3 откат"
    assert "@" not in format_thread_summary_reply("сделал @ivanov")
    assert "@" not in format_thread_summary_streaming("сделал @ivanov")
    # Emails are left untouched.
    assert neutralize_mentions("user@host.ru") == "user@host.ru"


def test_postmortem_comment_is_jira_wiki_without_disturbing_summary():
    report = "[INC] 01.01.2026 - Сбой\n## Сводка\nТекст\n- пункт\n**жирный**"
    comment = build_postmortem_comment(
        report=report, incident_thread_url="https://t/1", postmortem_author="Автор"
    )
    assert "h2. Сводка" in comment
    assert "## Сводка" not in comment
    assert "* пункт" in comment
    assert "*жирный*" in comment and "**жирный**" not in comment
    # Summary extraction reads the untouched raw report.
    assert extract_postmortem_summary(report, fallback="f").startswith("[INC] 01.01.2026 - Сбой")


def test_incident_report_prompt_fills_placeholders_without_leftovers():
    prompt = build_incident_report_prompt(
        thread_url="https://t/1",
        participants=["Иван Иванов", "Пётр Петров"],
        postmortem_author="Сидор Сидоров",
        transcript="тело треда",
        max_chars=24000,
    )
    # Built from the unified default template when no override is given.
    assert prompt.startswith("Составь инцидентный отчёт по треду Mattermost")
    assert prompt.endswith("Тред:\nтело треда\n")
    assert "Тред инцидента: https://t/1" in prompt
    assert "Участники: Иван Иванов, Пётр Петров" in prompt
    assert "Автор отчёта: Сидор Сидоров" in prompt
    for token in ("{thread_url}", "{participants}", "{postmortem_author}", "{transcript}"):
        assert token not in prompt


def test_incident_report_prompt_empty_participants_placeholder():
    prompt = build_incident_report_prompt(
        thread_url="https://t/1",
        participants=[],
        postmortem_author="Автор",
        transcript="тело",
        max_chars=24000,
    )
    assert "Участники: не указано" in prompt


def test_incident_report_prompt_overridable_and_does_not_rescan_transcript():
    # A custom template wins, and brace-looking tokens inside the thread text are
    # never re-substituted (transcript is filled last).
    transcript = "юзер написал {participants} и {transcript}"
    rendered = build_incident_report_prompt(
        thread_url="https://t/1",
        participants=["Иван Иванов"],
        postmortem_author="Автор",
        transcript=transcript,
        max_chars=24000,
        template="X {thread_url} | {participants} | {postmortem_author} | {transcript}",
    )
    assert rendered == (
        "X https://t/1 | Иван Иванов | Автор | юзер написал {participants} и {transcript}"
    )


def test_incident_report_prompt_keeps_legacy_thread_url_placeholder():
    # Pre-existing LLM_POSTMORTEM_PROMPT files used {incident_thread_url}; the
    # builder still substitutes that alias so they don't emit a literal token.
    rendered = build_incident_report_prompt(
        thread_url="https://t/1",
        participants=["Иван Иванов"],
        postmortem_author="Автор",
        transcript="тело",
        max_chars=24000,
        template="ПМ {incident_thread_url} :: {transcript}",
    )
    assert rendered == "ПМ https://t/1 :: тело"


def test_summary_and_postmortem_share_one_default_template():
    assert DEFAULT_SUMMARY_PROMPT is DEFAULT_POSTMORTEM_PROMPT
    for token in ("{thread_url}", "{participants}", "{postmortem_author}", "{transcript}"):
        assert token in DEFAULT_POSTMORTEM_PROMPT


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


def test_debug_settings_endpoints_roundtrip(settings):
    service = _build_service(replace(settings, debug_admin_enabled=True))
    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        by_key = {p["key"]: p for p in client.get("/debug/admin/api/settings").json()["prompts"]}
        assert by_key["llm_summary_prompt"]["source"] == "default"
        assert by_key["llm_summary_prompt"]["value"] == DEFAULT_SUMMARY_PROMPT

        saved = client.post(
            "/debug/admin/api/settings/llm_summary_prompt", json={"value": "мой промпт"}
        ).json()
        summary = next(p for p in saved["prompts"] if p["key"] == "llm_summary_prompt")
        assert summary["source"] == "db" and summary["value"] == "мой промпт"
        assert service.repository.get_setting("llm_summary_prompt") == "мой промпт"

        reset = client.post("/debug/admin/api/settings/llm_summary_prompt/reset").json()
        summary = next(p for p in reset["prompts"] if p["key"] == "llm_summary_prompt")
        assert summary["source"] == "default"
        assert service.repository.get_setting("llm_summary_prompt") is None

        assert (
            client.post("/debug/admin/api/settings/bogus", json={"value": "x"}).status_code == 404
        )


# --- Time to fix ------------------------------------------------------------


async def _confirm_and_close_incident(service, *, closed_at_ms: int):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    ticket = service.repository.get_by_post_id(post.id)
    return await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="validator",
            emoji_name="white_check_mark",
            create_at=closed_at_ms,
        )
    )


@pytest.mark.asyncio
async def test_postmortem_sets_time_to_fix_minutes(settings):
    service = _build_service(replace(settings, jira_time_to_fix_field="customfield_99999"))
    service.llm = FakeLlmClient()
    # Alert at 1_700_000_000_000; closed 300_000 ms (5 min) later.
    await _confirm_and_close_incident(service, closed_at_ms=1_700_000_300_000)
    assert service.jira.time_to_fix_updates == [("OPS-1", 5)]


@pytest.mark.asyncio
async def test_time_to_fix_skipped_when_end_before_start(settings):
    service = _build_service(replace(settings, jira_time_to_fix_field="customfield_99999"))
    service.llm = FakeLlmClient()
    # Checkmark timestamped before the alert was created → non-positive duration.
    await _confirm_and_close_incident(service, closed_at_ms=1_699_999_000_000)
    assert service.jira.time_to_fix_updates == []


@pytest.mark.asyncio
async def test_time_to_fix_not_set_when_field_unconfigured(service):
    service.llm = FakeLlmClient()
    await _confirm_and_close_incident(service, closed_at_ms=1_700_000_300_000)
    assert service.jira.time_to_fix_updates == []


@pytest.mark.asyncio
async def test_time_to_fix_on_manual_checkmark_new_issue(settings):
    # Manual checkmark with no prior create_task → the new-issue postmortem branch.
    service = _build_service(replace(settings, jira_time_to_fix_field="customfield_99999"))
    service.llm = FakeLlmClient()
    root = MattermostPost(
        id="manualincidentroot000000099",
        channel_id="incidents-channel",
        user_id="author",
        message="Инцидент по росту 500.",
        create_at=1_700_000_000_000,
        channel_name="incidents",
    )
    service.mattermost.posts[root.id] = root
    await service.handle_reaction(
        ReactionEvent(
            post_id=root.id,
            user_id="closer",
            emoji_name="white_check_mark",
            create_at=1_700_000_300_000,
        )
    )
    assert service.jira.time_to_fix_updates == [("OPS-1", 5)]


@pytest.mark.asyncio
async def test_time_to_fix_on_manual_incident_close(settings):
    # create_task → end_incident → the existing-issue postmortem branch.
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=True,
            jira_time_to_fix_field="customfield_99999",
        )
    )
    service.llm = FakeLlmClient()
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)
    await service.handle_incident_action(
        action="create_task", incident_post_id=post.id, user_id="opener"
    )
    await service.handle_incident_action(
        action="end_incident", incident_post_id=post.id, user_id="closer"
    )
    # Exactly one write, positive minutes (no double-write across set_end_time sites).
    assert len(service.jira.time_to_fix_updates) == 1
    assert service.jira.time_to_fix_updates[0][0] == "OPS-1"
    assert service.jira.time_to_fix_updates[0][1] > 0


@pytest.mark.asyncio
async def test_time_to_fix_failure_does_not_block_closure(settings):
    service = _build_service(replace(settings, jira_time_to_fix_field="customfield_99999"))
    service.llm = FakeLlmClient()

    async def _boom(issue_key, minutes):
        raise ApiError("nope", retryable=False)

    service.jira.set_time_to_fix = _boom
    result = await _confirm_and_close_incident(service, closed_at_ms=1_700_000_300_000)
    assert result.status == "incident_ended"
    assert ("OPS-1", datetime_from_mattermost_ms(1_700_000_300_000)) in service.jira.end_updates


@pytest.mark.asyncio
async def test_set_time_to_fix_sends_minutes(settings):
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
        replace(settings, jira_time_to_fix_field="customfield_77777"),
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        await client.set_time_to_fix("OPS-1", 42)
    finally:
        await client.aclose()

    assert requests == [
        {
            "method": "PUT",
            "path": "/rest/api/2/issue/OPS-1",
            "body": {"fields": {"customfield_77777": 42}},
        }
    ]


@pytest.mark.asyncio
async def test_set_time_to_fix_skipped_when_field_unconfigured(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected when the field is not configured")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        await client.set_time_to_fix("OPS-1", 10)
    finally:
        await client.aclose()


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


# --- Incident validity emoji + postmortem idempotency -----------------------


async def _confirmed_incident(service):
    """Confirm an alert into an incident and return its ticket."""
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    return service.repository.get_by_post_id(post.id)


@pytest.mark.asyncio
async def test_false_reaction_in_incident_closes_with_postmortem(service):
    service.llm = FakeLlmClient()
    ticket = await _confirmed_incident(service)

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="closer",
            emoji_name="man_gesturing_no",
            create_at=1_700_000_200_000,
        )
    )

    assert result.status == "incident_ended"
    # Validity stamped Ложный, postmortem generated exactly once, end-time set.
    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"
    assert len(service.llm.prompts) == 1
    assert [key for key, _ in service.jira.generic_comments] == ["OPS-1"]
    assert [key for key, _ in service.jira.end_updates] == ["OPS-1"]
    assert service.repository.get_by_post_id(ticket.mattermost_post_id).postmortem_comment_added


@pytest.mark.asyncio
async def test_false_reaction_in_incident_sets_validity_without_llm(service):
    # No LLM (postmortem disabled) must still write the chosen validity to Jira.
    service.llm = None
    ticket = await _confirmed_incident(service)

    await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="closer",
            emoji_name="man_gesturing_no",
            create_at=1_700_000_200_000,
        )
    )

    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"
    # End-time set, but no postmortem comment (no LLM).
    assert [key for key, _ in service.jira.end_updates] == ["OPS-1"]
    assert service.jira.generic_comments == []


@pytest.mark.asyncio
async def test_validity_reaction_on_finalized_incident_only_flips_validity(service):
    service.llm = FakeLlmClient()
    ticket = await _confirmed_incident(service)
    # Close it once via checkmark (Валидный + postmortem).
    await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="closer",
            emoji_name="white_check_mark",
            create_at=1_700_000_200_000,
        )
    )
    assert len(service.jira.generic_comments) == 1

    # A later validity emoji just flips the field; it must not regenerate the PM.
    await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="auditor",
            emoji_name="arrows_counterclockwise",
            create_at=1_700_000_300_000,
        )
    )

    assert service.jira.validity_by_issue["OPS-1"] == "Ожидаемый"
    assert len(service.jira.generic_comments) == 1
    assert len(service.llm.prompts) == 1
    # The templated "validity changed" notice is posted in the incident thread.
    notices = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == ticket.incident_post_id and "Валидность обновлена" in _reply_text(c)
    ]
    assert len(notices) == 1
    assert "Ожидаемый" in _reply_text(notices[0])


@pytest.mark.asyncio
async def test_repeated_checkmark_does_not_duplicate_postmortem(service):
    service.llm = FakeLlmClient()
    ticket = await _confirmed_incident(service)
    reaction = ReactionEvent(
        post_id=ticket.incident_post_id,
        user_id="closer",
        emoji_name="white_check_mark",
        create_at=1_700_000_200_000,
    )

    await service.handle_reaction(reaction)
    await service.handle_reaction(reaction)

    # The PM comment is additive — a second checkmark must not duplicate it.
    assert len(service.jira.generic_comments) == 1
    assert len(service.llm.prompts) == 1


# --- Summary emoji ----------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_reaction_posts_thread_summary(service):
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="reader", emoji_name="memo", create_at=2)
    )

    assert "опубликовано" in result.message
    assert len(service.llm.summary_prompts) == 1
    # Summary is LLM-only; it never touches Jira.
    assert service.jira.generic_comments == []


@pytest.mark.asyncio
async def test_summary_reaction_works_in_incident_thread(service):
    service.llm = FakeLlmClient()
    ticket = await _confirmed_incident(service)

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id, user_id="reader", emoji_name="memo", create_at=3
        )
    )

    assert "опубликовано" in result.message
    assert len(service.llm.summary_prompts) == 1
    # No postmortem comment from a summary emoji.
    assert service.jira.generic_comments == []
