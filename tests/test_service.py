from __future__ import annotations

from dataclasses import replace
from datetime import timezone

import pytest
from fastapi.testclient import TestClient

from mm_jira_bot.config import Settings
from mm_jira_bot.domain import JiraIssue, MattermostPost, ReactionEvent, utc_now
from mm_jira_bot.formatting import format_incident_message
from mm_jira_bot.jira import build_jira_issue_payload
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

    async def create_post(self, *, channel_id: str, message: str, props: dict | None = None):
        post = MattermostPost(
            id=f"incidentpost{len(self.created_posts):014d}",
            channel_id=channel_id,
            user_id="bot-user",
            message=message,
            create_at=1_700_000_100_000,
        )
        self.created_posts.append(
            {"channel_id": channel_id, "message": message, "props": props, "post": post}
        )
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

    async def create_issue(self, post, *, message_url: str, channel_name: str | None):
        key = f"OPS-{len(self.created_payloads) + 1}"
        self.created_payloads.append(
            {"post": post, "message_url": message_url, "channel_name": channel_name}
        )
        self.valid_by_issue[key] = False
        return JiraIssue(key=key, url=f"https://jira.example.com/browse/{key}")

    async def get_valid_incident(self, issue_key: str):
        return self.valid_by_issue.get(issue_key, False)

    async def set_valid_incident(self, issue_key: str, value: bool):
        self.valid_updates.append((issue_key, value))
        self.valid_by_issue[issue_key] = value

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
        jira_email="bot@example.com",
        jira_api_token="jira-token",
        jira_project_key="OPS",
        jira_issue_type="Incident",
        jira_valid_incident_field_id="customfield_12345",
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


def make_alert(post_id: str = POST_ID, channel_id: str = "alerts-channel") -> MattermostPost:
    return MattermostPost(
        id=post_id,
        channel_id=channel_id,
        user_id="author-user",
        message="CPU usage is above 95%",
        create_at=1_700_000_000_000,
        channel_name="alerts",
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
    assert len(service.jira.comments) == 1


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

    assert len(service.mattermost.created_posts) == 1
    assert len(service.jira.comments) == 1


def test_builds_jira_payload(settings):
    post = make_alert()
    payload = build_jira_issue_payload(
        settings,
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    fields = payload["fields"]
    assert fields["project"] == {"key": "OPS"}
    assert fields["issuetype"] == {"name": "Incident"}
    assert fields["customfield_12345"] is False
    assert "Mattermost alert:" in fields["summary"]
    assert fields["description"]["type"] == "doc"


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
        confirmed_by_user_id="validator",
        confirmed_at=utc_now().astimezone(timezone.utc),
    )

    assert "CPU usage is above 95%" in message
    assert "Original alert" in message
    assert "OPS-1" in message
    assert "validator" in message
