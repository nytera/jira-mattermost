from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from support import (
    _build_service,
    make_alert,
)

from mm_jira_bot.domain import (
    ReactionEvent,
)
from mm_jira_bot.postmortem import (
    DEFAULT_SUMMARY_PROMPT,
)
from mm_jira_bot.web import create_app


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
