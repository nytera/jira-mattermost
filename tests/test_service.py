from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
import mm_jira_bot.jira as jira_module
import mm_jira_bot.jira_payload as jira_payload_module
from fastapi.testclient import TestClient

from mm_jira_bot.config import Settings, load_dotenv_file
from mm_jira_bot.domain import (
    JiraIssue,
    MattermostPost,
    ReactionEvent,
    datetime_from_mattermost_ms,
)
from mm_jira_bot.formatting import format_incident_message
from mm_jira_bot.jira_payload import build_jira_issue_payload
from mm_jira_bot.repository import (
    AlertTicketRepository,
    create_database_engine,
    create_session_factory,
    init_db,
)
from mm_jira_bot.service import IncidentBotService, parse_post_id_from_text
from mm_jira_bot.web import create_app


POST_ID = "abcdefghijklmnopqrstuvwx01"


class FakeMattermostClient:
    def __init__(self) -> None:
        self.posts: dict[str, MattermostPost] = {}
        self.created_posts: list[dict] = []

    def permalink(self, post_id: str) -> str:
        return f"https://mattermost.example.com/_redirect/pl/{post_id}"

    async def get_channel_name(self, channel_id: str) -> str:
        return "alerts"

    async def get_post(self, post_id: str) -> MattermostPost:
        return self.posts[post_id]

    async def get_user_display_name(self, user_id: str) -> str:
        return f"@{user_id}"

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

    async def create_issue(
        self,
        post,
        *,
        message_url: str,
        channel_name: str | None,
        author_name: str | None = None,
    ):
        key = f"OPS-{len(self.created_payloads) + 1}"
        self.created_payloads.append(
            {
                "post": post,
                "message_url": message_url,
                "channel_name": channel_name,
                "author_name": author_name,
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

    async def transition_issue(self, issue_key: str, transition_id: str):
        self.transitions.append((issue_key, transition_id))

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
) -> MattermostPost:
    return MattermostPost(
        id=post_id,
        channel_id=channel_id,
        user_id="author-user",
        message=message,
        create_at=1_700_000_000_000,
        channel_name="alerts",
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
    assert len(service.jira.created_payloads) == 1


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
    assert service.jira.end_updates == [
        ("OPS-1", datetime_from_mattermost_ms(1_700_000_200_000))
    ]
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
    assert service.jira.validity_end_updates == [
        ("OPS-1", datetime_from_mattermost_ms(1))
    ]
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
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post.id
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
    assert service.jira.validity_end_updates == [
        ("OPS-1", datetime_from_mattermost_ms(1))
    ]


@pytest.mark.asyncio
async def test_last_validity_reaction_wins(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="v", emoji_name="incident", create_at=1)
    )
    await service.handle_reaction(
        ReactionEvent(
            post_id=post.id, user_id="v", emoji_name="man_gesturing_no", create_at=2
        )
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
        ReactionEvent(
            post_id=post.id, user_id="v", emoji_name="man_gesturing_no", create_at=1
        )
    )
    result = await service.handle_reaction(
        ReactionEvent(
            post_id=post.id, user_id="v", emoji_name="man_gesturing_no", create_at=2
        )
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
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post.id
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
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post.id
    ]
    # One reply for issue creation, one for status change; no duplicates on retry.
    assert len(thread_replies) == 2
    status_reply = thread_replies[1]
    assert status_reply["channel_id"] == "alerts-channel"
    assert "OPS-1" in status_reply["message"]
    assert "Валидный" in status_reply["message"]
    assert "Сообщение в канале инцидентов" in status_reply["message"]

    incident_posts = [
        created
        for created in service.mattermost.created_posts
        if created["channel_id"] == "incidents-channel"
    ]
    assert len(incident_posts) == 1
    assert "Подтвердил: `@validator`" in incident_posts[0]["message"]


def test_extracts_post_id_from_mattermost_permalink():
    assert (
        parse_post_id_from_text(f"https://mattermost.example.com/team/pl/{POST_ID}")
        == POST_ID
    )
    assert (
        parse_post_id_from_text(f"https://mattermost.example.com/_redirect/pl/{POST_ID}")
        == POST_ID
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
    assert "second line" in list_response.json()["alerts"][0]["mattermost_message_preview"]
    assert detail_response.status_code == 200
    assert detail_response.json()["mattermost_message_text"] == post.message


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
async def test_debug_admin_recreate_without_force_conflicts_existing_issue(
    service, settings
):
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
        response = client.post(
            f"/debug/admin/api/alerts/{post.id}/jira/recreate?force=true"
        )

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
    assert "h3. 🔔 Алерт из Band" in description
    assert "{quote}\nCPU usage is above 95%\nsecond line\n{quote}" in description
    assert "|Время сообщения|15.11.2023 01:13|" in description
    assert (
        "|Исходное сообщение|[Открыть в Band|"
        "https://mattermost.example.com/_redirect/pl/post]|" in description
    )
    assert f"{{{{{POST_ID}}}}}" in description


def test_summary_uses_first_non_empty_line_without_leading_emoji(settings):
    message = (
        "\n"
        "🔴 Доля рекламных кликов :: Платформа :: Ниже на 10% :: crit\n"
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


def test_summary_uses_alert_title_after_emoji_only_line(settings):
    message = (
        "🔴\n"
        "Деньги | Минус-слова vs Общее | выше на 70% [Crit]\n"
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
        lambda: datetime(2026, 5, 29, 22, 30, tzinfo=timezone.utc),
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
                        "customfield_34567": {
                            "allowedValues": [{"id": "301", "value": "Да"}]
                        },
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
                        "customfield_23456": {"allowedValues": [{"id": "201", "value": "Crit alert"}]},
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
                        "customfield_12345": {
                            "allowedValues": [{"id": "201", "value": "Валидный"}]
                        }
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
        }
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
                        "customfield_12345": {
                            "allowedValues": [{"id": "202", "value": "Ложный"}]
                        }
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
            ended_at=datetime(2026, 5, 29, 22, 30, tzinfo=timezone.utc),
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
            datetime(2026, 5, 29, 22, 30, tzinfo=timezone.utc),
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
        confirmed_at=datetime(2026, 5, 29, 22, 30, tzinfo=timezone.utc),
    )

    assert "CPU usage is above 95%" in message
    assert "Исходный алерт" in message
    assert "OPS-1" in message
    assert "@validator" in message
    assert "Время подтверждения: `2026-05-30T01:30:00+03:00`" in message
