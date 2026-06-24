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
from mm_jira_bot.retry import ApiError
from mm_jira_bot.service._shared import _PROMPT_KEY_SUMMARY
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


# --- recreate: unknown post + fatal Jira failure ---------------------------


def test_debug_admin_recreate_unknown_post_id_returns_404(service, settings):
    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        response = client.post("/debug/admin/api/alerts/doesnotexist00000000000001/jira/recreate")

    assert response.status_code == 404
    assert response.json()["status"] == "not_found"


def test_debug_admin_recreate_fatal_jira_failure_returns_502(service, settings):
    post = make_alert()
    service.repository.create_or_get_alert(
        post,
        message_url=service.mattermost.permalink(post.id),
        channel_name="alerts",
    )

    async def boom(ticket):
        raise ApiError("jira is down")

    service._create_jira_issue = boom

    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        response = client.post(f"/debug/admin/api/alerts/{post.id}/jira/recreate")

    assert response.status_code == 502
    assert response.json()["status"] == "error"
    assert response.json()["jira_issue_key"] is None


async def test_debug_admin_force_recreate_failure_preserves_old_key(service, settings):
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

    async def boom(ticket):
        raise ApiError("jira create exploded")

    service._create_jira_issue = boom

    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        response = client.post(f"/debug/admin/api/alerts/{post.id}/jira/recreate?force=true")

    body = response.json()
    ticket = service.repository.get_by_post_id(post.id)
    assert response.status_code == 502
    assert body["status"] == "error"
    assert body["previous_jira_issue_key"] == "OPS-1"
    assert ticket is not None
    assert ticket.jira_issue_key == "OPS-1"


# --- create-from-link: always HTTP 200, status carries the outcome ---------


def test_debug_admin_create_from_link_invalid_is_http_200_status(service, settings):
    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        response = client.post(
            "/debug/admin/api/alerts/create-from-link", json={"link": "not a link"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "invalid_link"


def test_debug_admin_create_from_link_created_then_exists_both_http_200(service, settings):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    link = service.mattermost.permalink(post.id)

    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        first = client.post("/debug/admin/api/alerts/create-from-link", json={"link": link})
        second = client.post("/debug/admin/api/alerts/create-from-link", json={"link": link})

    assert first.status_code == 200
    assert first.json()["status"] == "created"
    assert first.json()["ok"] is True
    assert second.status_code == 200
    assert second.json()["status"] == "exists"
    assert second.json()["ok"] is True
    assert len(service.jira.created_payloads) == 1


# --- logs: no buffer + clamping --------------------------------------------


def test_debug_admin_logs_no_buffer(service, settings, monkeypatch):
    import mm_jira_bot.debug_admin as debug_admin

    monkeypatch.setattr(debug_admin, "get_log_buffer", lambda: None)
    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        response = client.get("/debug/admin/api/logs")

    assert response.status_code == 200
    assert response.json() == {"logs": [], "available": False}


def test_debug_admin_logs_limit_clamped(service, settings, monkeypatch):
    import mm_jira_bot.debug_admin as debug_admin

    captured: list[int] = []

    class _FakeBuffer:
        def records(self, *, limit: int, min_levelno: int = 0):
            captured.append(limit)
            return []

    monkeypatch.setattr(debug_admin, "get_log_buffer", lambda: _FakeBuffer())
    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        high = client.get("/debug/admin/api/logs?limit=99999")
        low = client.get("/debug/admin/api/logs?limit=0")

    assert high.status_code == 200
    assert low.status_code == 200
    assert high.json()["available"] is True
    assert captured == [2000, 1]


# --- alerts limit footgun: response clamped, underlying call unclamped -----


def test_debug_admin_alerts_limit_response_clamped_but_query_unclamped(service, settings):
    captured: dict[str, int] = {}

    def fake_list_alerts(*, limit, status=None, validity=None):
        captured["limit"] = limit
        return []

    service.repository.list_alerts = fake_list_alerts

    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        response = client.get("/debug/admin/api/alerts?limit=9999")

    assert response.status_code == 200
    # Response advertises the clamped ceiling …
    assert response.json()["limit"] == 200
    # … but the repository was queried with the raw unclamped value (footgun).
    assert captured["limit"] == 9999


# --- settings validation errors --------------------------------------------


def test_debug_admin_reset_unknown_key_returns_404(service, settings):
    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        response = client.post("/debug/admin/api/settings/bogus/reset")

    assert response.status_code == 404


def test_debug_admin_save_without_value_returns_422(service, settings):
    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        response = client.post(f"/debug/admin/api/settings/{_PROMPT_KEY_SUMMARY}", json={})

    assert response.status_code == 422


def test_debug_admin_alerts_bad_query_coercion_returns_422(service, settings):
    app = create_app(replace(settings, debug_admin_enabled=True), service=service)
    with TestClient(app) as client:
        bad_limit = client.get("/debug/admin/api/alerts?limit=abc")
        bad_force = client.post("/debug/admin/api/alerts/anypost/jira/recreate?force=maybe")

    assert bad_limit.status_code == 422
    assert bad_force.status_code == 422


# --- debug_create_from_link guards (service level) -------------------------


async def test_debug_create_from_link_fresh_creates(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.debug_create_from_link(service.mattermost.permalink(post.id))

    assert result.ok is True
    assert result.status == "created"
    assert result.jira_issue_key == "OPS-1"


async def test_debug_create_from_link_repeat_returns_exists_without_second_issue(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    link = service.mattermost.permalink(post.id)

    await service.debug_create_from_link(link)
    result = await service.debug_create_from_link(link)

    assert result.ok is True
    assert result.status == "exists"
    assert result.jira_issue_key == "OPS-1"
    assert len(service.jira.created_payloads) == 1


async def test_debug_create_from_link_resolved_repost_skipped(service):
    post = make_alert(message="**✅ CPU usage is above 95%**")
    service.mattermost.posts[post.id] = post

    result = await service.debug_create_from_link(service.mattermost.permalink(post.id))

    assert result.ok is False
    assert result.status == "skipped"
    assert len(service.jira.created_payloads) == 0


async def test_debug_create_from_link_garbage_returns_invalid_link(service):
    result = await service.debug_create_from_link("just some words")

    assert result.ok is False
    assert result.status == "invalid_link"


async def test_debug_create_from_link_post_lookup_failure_returns_post_not_found(service):
    post = make_alert()

    async def boom(post_id):
        raise ApiError("mattermost 500")

    service.mattermost.get_post = boom

    result = await service.debug_create_from_link(service.mattermost.permalink(post.id))

    assert result.ok is False
    assert result.status == "post_not_found"
    assert result.mattermost_post_id == post.id
