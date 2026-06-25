"""HTTP-level tests for the admin API (``admin_api.py``): Bearer-token auth, the
JSON routes, and SPA-mount route precedence.

The app is built through ``create_app`` so the routes register exactly as in
production (admin routes added last, behind the ``admin_ui_enabled`` flag).
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from support import (
    FakeLlmClient,
    make_alert,
)

from mm_jira_bot.service._shared import _PROMPT_KEY_SUMMARY
from mm_jira_bot.web import create_app

TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _admin_app(service, *, token: str | None = TOKEN):
    """Enable the admin UI and build the app.

    The Bearer dependency closes over ``service.settings`` (not the settings
    passed to ``create_app``), so the token has to live on the service.
    """
    service.settings = replace(service.settings, admin_ui_enabled=True, admin_ui_token=token)
    return create_app(service.settings, service=service)


# --------------------------------------------------------------------------- #
# Feature flag + auth
# --------------------------------------------------------------------------- #


def test_admin_api_disabled_by_default_returns_404(service, settings):
    app = create_app(settings, service=service)
    with TestClient(app) as client:
        response = client.get("/admin/api/stats", headers=AUTH)

    assert response.status_code == 404


def test_admin_api_requires_token_header(service, settings):
    app = _admin_app(service)
    with TestClient(app) as client:
        missing = client.get("/admin/api/stats")
        wrong = client.get("/admin/api/stats", headers={"Authorization": "Bearer nope"})
        ok = client.get("/admin/api/stats", headers=AUTH)

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 200


def test_admin_api_enabled_without_token_returns_503(service, settings):
    app = _admin_app(service, token=None)
    with TestClient(app) as client:
        response = client.get("/admin/api/stats", headers=AUTH)

    assert response.status_code == 503


# --------------------------------------------------------------------------- #
# stats / summary
# --------------------------------------------------------------------------- #


def test_stats_returns_dashboard_shape(service, settings):
    post = make_alert()
    service.repository.create_or_get_alert(
        post,
        message_url=service.mattermost.permalink(post.id),
        channel_name="alerts",
    )
    app = _admin_app(service)
    with TestClient(app) as client:
        body = client.get("/admin/api/stats", headers=AUTH).json()

    assert body["total"] == 1
    for key in (
        "open",
        "closed",
        "pending_jira",
        "failed",
        "mtta_seconds",
        "mttr_seconds",
        "by_validity_label",
        "timeseries_daily",
        "top_channels",
    ):
        assert key in body


def test_summary_endpoint_returns_counts(service, settings):
    post = make_alert()
    service.repository.create_or_get_alert(
        post,
        message_url=service.mattermost.permalink(post.id),
        channel_name="alerts",
    )
    app = _admin_app(service)
    with TestClient(app) as client:
        body = client.get("/admin/api/summary", headers=AUTH).json()

    assert body["total"] == 1


# --------------------------------------------------------------------------- #
# alerts list / detail
# --------------------------------------------------------------------------- #


def test_alerts_list_and_detail(service, settings):
    post = make_alert(message="CPU usage is above 95%\nsecond line")
    service.repository.create_or_get_alert(
        post,
        message_url=service.mattermost.permalink(post.id),
        channel_name="alerts",
    )
    app = _admin_app(service)
    with TestClient(app) as client:
        listing = client.get("/admin/api/alerts", headers=AUTH).json()
        detail = client.get(f"/admin/api/alerts/{post.id}", headers=AUTH)

    assert listing["alerts"][0]["mattermost_post_id"] == post.id
    assert detail.status_code == 200
    assert detail.json()["mattermost_message_text"] == post.message


def test_alert_detail_unknown_returns_404(service, settings):
    app = _admin_app(service)
    with TestClient(app) as client:
        response = client.get("/admin/api/alerts/doesnotexist00000000000001", headers=AUTH)

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# settings roundtrip
# --------------------------------------------------------------------------- #


def test_settings_roundtrip(service, settings):
    app = _admin_app(service)
    with TestClient(app) as client:
        prompts = client.get("/admin/api/settings", headers=AUTH).json()["prompts"]
        by_key = {p["key"]: p for p in prompts}
        assert by_key[_PROMPT_KEY_SUMMARY]["source"] == "default"

        saved = client.post(
            f"/admin/api/settings/{_PROMPT_KEY_SUMMARY}",
            headers=AUTH,
            json={"value": "мой промпт"},
        ).json()
        summary = next(p for p in saved["prompts"] if p["key"] == _PROMPT_KEY_SUMMARY)
        assert summary["source"] == "db" and summary["value"] == "мой промпт"

        reset = client.post(f"/admin/api/settings/{_PROMPT_KEY_SUMMARY}/reset", headers=AUTH).json()
        summary = next(p for p in reset["prompts"] if p["key"] == _PROMPT_KEY_SUMMARY)
        assert summary["source"] == "default"

        unknown = client.post("/admin/api/settings/bogus", headers=AUTH, json={"value": "x"})
        assert unknown.status_code == 404


# --------------------------------------------------------------------------- #
# create-from-link / recreate status codes
# --------------------------------------------------------------------------- #


def test_create_from_link_invalid_is_http_200_with_status(service, settings):
    app = _admin_app(service)
    with TestClient(app) as client:
        response = client.post(
            "/admin/api/alerts/create-from-link", headers=AUTH, json={"link": "not a link"}
        )

    assert response.status_code == 200
    assert response.json()["status"] == "invalid_link"


def test_recreate_status_codes(service, settings):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    app = _admin_app(service)
    with TestClient(app) as client:
        # create then re-create without force -> 409 conflict
        created = client.post(
            "/admin/api/alerts/create-from-link", headers=AUTH, json={"link": post.id}
        )
        conflict = client.post(f"/admin/api/alerts/{post.id}/jira/recreate", headers=AUTH)
        unknown = client.post(
            "/admin/api/alerts/doesnotexist00000000000001/jira/recreate", headers=AUTH
        )

    assert created.status_code == 200
    assert conflict.status_code == 409
    assert unknown.status_code == 404


# --------------------------------------------------------------------------- #
# lifecycle endpoints
# --------------------------------------------------------------------------- #


def test_lifecycle_unknown_post_status_codes(service, settings):
    app = _admin_app(service)
    missing = "doesnotexist00000000000001"
    with TestClient(app) as client:
        confirm = client.post(f"/admin/api/alerts/{missing}/confirm", headers=AUTH)
        end = client.post(f"/admin/api/alerts/{missing}/end", headers=AUTH)
        postmortem = client.post(f"/admin/api/alerts/{missing}/postmortem", headers=AUTH)
        validity = client.post(
            f"/admin/api/alerts/{missing}/validity", headers=AUTH, json={"validity_label": "Ложный"}
        )

    assert confirm.status_code == 404
    assert end.status_code == 404
    assert postmortem.status_code == 404
    # validity on an unknown post degrades to PENDING_JIRA -> HTTP 200
    assert validity.status_code == 200
    assert validity.json()["status"] == "pending_jira"


def test_confirm_endpoint_publishes_incident(service, settings):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    app = _admin_app(service)
    with TestClient(app) as client:
        client.post("/admin/api/alerts/create-from-link", headers=AUTH, json={"link": post.id})
        response = client.post(f"/admin/api/alerts/{post.id}/confirm", headers=AUTH)

    ticket = service.repository.get_by_post_id(post.id)
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    assert ticket is not None and ticket.valid_incident is True


def test_summary_endpoint_returns_message(service, settings):
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post

    app = _admin_app(service)
    with TestClient(app) as client:
        client.post("/admin/api/alerts/create-from-link", headers=AUTH, json={"link": post.id})
        response = client.post(f"/admin/api/alerts/{post.id}/summary", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["message"]


# --------------------------------------------------------------------------- #
# logs
# --------------------------------------------------------------------------- #


def test_logs_no_buffer(service, settings, monkeypatch):
    import mm_jira_bot.admin_api as admin_api

    monkeypatch.setattr(admin_api, "get_log_buffer", lambda: None)
    app = _admin_app(service)
    with TestClient(app) as client:
        response = client.get("/admin/api/logs", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"logs": [], "available": False}


# --------------------------------------------------------------------------- #
# SPA mount: route precedence (build present) vs no-op (build absent)
# --------------------------------------------------------------------------- #


def _write_build(static_dir):
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "index.html").write_text("<!doctype html><title>ADMIN_SPA</title>")
    (static_dir / "assets").mkdir(exist_ok=True)
    (static_dir / "assets" / "app.js").write_text("console.log('admin')")


def test_spa_mount_does_not_shadow_api_or_health(service, settings, tmp_path, monkeypatch):
    import mm_jira_bot.admin_api as admin_api

    static_dir = tmp_path / "admin_static"
    _write_build(static_dir)
    monkeypatch.setattr(admin_api, "_ADMIN_STATIC_DIR", static_dir)

    app = _admin_app(service)
    with TestClient(app) as client:
        # API route still wins over the catch-all …
        stats = client.get("/admin/api/stats", headers=AUTH)
        # … a real SPA client path falls through to index.html …
        spa = client.get("/admin/incidents")
        index = client.get("/admin")
        asset = client.get("/admin/assets/app.js")
        # … and an unrelated route is untouched.
        health = client.get("/healthz")

    assert stats.status_code == 200 and "total" in stats.json()
    assert spa.status_code == 200 and "ADMIN_SPA" in spa.text
    assert index.status_code == 200 and "ADMIN_SPA" in index.text
    assert asset.status_code == 200 and "admin" in asset.text
    assert health.status_code == 200


def test_spa_mount_noop_when_build_missing(service, settings):
    # No admin_static build in the repo -> mount_admin_ui is a no-op; GET /admin
    # must 404 (route never registered), never 500.
    app = _admin_app(service)
    with TestClient(app) as client:
        response = client.get("/admin")

    assert response.status_code == 404


@pytest.mark.parametrize("bad_query", ["abc", "9.5"])
def test_alerts_bad_limit_coercion_returns_422(service, settings, bad_query):
    app = _admin_app(service)
    with TestClient(app) as client:
        response = client.get(f"/admin/api/alerts?limit={bad_query}", headers=AUTH)

    assert response.status_code == 422
