"""Read-only (shadow) mode: write suppression, the audit-channel mirror, and the
backstop. The fakes in ``support.py`` do not implement the read-only redirect, so
these tests use the real :class:`MattermostClient` over an ``httpx.MockTransport``
that either records audit writes or fails on any unexpected (prod) write.
"""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from mm_jira_bot.audit import AuditMirror
from mm_jira_bot.llm import PostmortemLlmClient
from mm_jira_bot.mattermost import READONLY_POST_ID_PREFIX, MattermostClient
from mm_jira_bot.web import _assert_audit_channel_isolated


def _client(settings, handler) -> MattermostClient:
    return MattermostClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.mattermost_url,
            transport=httpx.MockTransport(handler),
        ),
    )


class _AuditTransport:
    """Captures audit-channel writes; fails on anything else."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.reactions: list[dict] = []
        self.updates: list[tuple[str, dict]] = []
        self._counter = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v4/posts":
            body = json.loads(request.content)
            self._counter += 1
            post_id = f"auditgen{self._counter:03d}"
            self.posts.append(body)
            return httpx.Response(
                201,
                json={
                    "id": post_id,
                    "channel_id": body["channel_id"],
                    "user_id": "bot-user",
                    "message": body.get("message", ""),
                    "create_at": 1,
                    "root_id": body.get("root_id"),
                },
            )
        if path == "/api/v4/reactions":
            self.reactions.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if path.endswith("/patch"):
            self.updates.append((path, json.loads(request.content)))
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected call: {request.method} {path}")


# --- suppression / backstop --------------------------------------------------


async def test_read_only_suppresses_writes_without_audit(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no HTTP expected: {request.method} {request.url.path}")

    client = _client(replace(settings, read_only_mode=True), handler)

    post = await client.create_post(channel_id="alerts-channel", message="hi")
    assert post.id.startswith(READONLY_POST_ID_PREFIX)
    # Redirected writes are dropped (no audit channel) and never hit the transport.
    await client.add_reaction("realpost", "memo")
    await client.update_post("realpost", message="x")
    await client.open_dialog(trigger_id="t", url="u", dialog={})
    # A read against a shadow-minted id short-circuits without HTTP.
    stub = await client.get_post(post.id)
    assert stub.id.startswith(READONLY_POST_ID_PREFIX)
    assert await client.get_thread_posts(post.id) == []
    await client.aclose()


async def test_read_only_backstop_raises_on_unbypassed_write(settings):
    client = _client(
        replace(settings, read_only_mode=True),
        lambda request: httpx.Response(200, json={}),
    )
    with pytest.raises(RuntimeError, match="read-only backstop"):
        await client._request("POST", "/api/v4/anything", error_message="e", event="ev")
    await client.aclose()


async def test_read_only_allows_post_reads_through_backstop(settings):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=[{"username": "alice", "id": "u1"}])

    client = _client(replace(settings, read_only_mode=True), handler)
    resolved = await client.get_user_ids_by_usernames(["alice"])
    assert resolved == {"alice": "u1"}
    assert seen == ["/api/v4/users/usernames"]
    await client.aclose()


# --- audit mirror ------------------------------------------------------------


def _mirrored_client(settings):
    transport = _AuditTransport()
    rs = replace(settings, read_only_mode=True, mattermost_audit_channel_id="audit-channel")
    client = _client(rs, transport)
    client.audit = AuditMirror(client, rs)
    return client, transport


async def test_audit_mirror_redirects_to_audit_and_strips_adoption_props(settings):
    client, transport = _mirrored_client(settings)
    post = await client.create_post(
        channel_id="alerts-channel",
        message="root",
        props={"mattermost_alert_post_id": "realalert", "jira_issue_key": "ADS-TEST"},
    )
    assert post.id.startswith(READONLY_POST_ID_PREFIX)
    assert len(transport.posts) == 1
    body = transport.posts[0]
    # Goes to the audit channel, not the original alert channel.
    assert body["channel_id"] == "audit-channel"
    # The correlation key is stripped so the shadow can't adopt its own audit post.
    assert "mattermost_alert_post_id" not in (body.get("props") or {})
    # A harmless display prop survives.
    assert body["props"]["jira_issue_key"] == "ADS-TEST"
    await client.aclose()


async def test_audit_mirror_threads_replies_under_same_root(settings):
    client, transport = _mirrored_client(settings)
    root = await client.create_post(channel_id="alerts-channel", message="root")
    reply = await client.create_post(channel_id="alerts-channel", message="reply", root_id=root.id)
    assert reply.id.startswith(READONLY_POST_ID_PREFIX)
    assert len(transport.posts) == 2
    # The root was posted with no thread; the reply lands under the root's audit id.
    assert transport.posts[0].get("root_id") is None
    assert transport.posts[1]["root_id"] == "auditgen001"
    await client.aclose()


async def test_audit_mirror_anchors_an_unseen_real_root(settings):
    client, transport = _mirrored_client(settings)
    # Reply whose root (a real alert post) was never mirrored: an anchor is made.
    await client.create_post(channel_id="alerts-channel", message="reply", root_id="realalert123")
    assert len(transport.posts) == 2
    anchor, reply = transport.posts
    assert anchor.get("root_id") is None
    assert "realalert123" in anchor["message"]
    assert reply["root_id"] == "auditgen001"
    assert reply["message"] == "reply"
    await client.aclose()


async def test_audit_mirror_reaction_targets_audit_post(settings):
    client, transport = _mirrored_client(settings)
    root = await client.create_post(channel_id="alerts-channel", message="root")
    await client.add_reaction(root.id, "arrows_counterclockwise")
    assert len(transport.reactions) == 1
    assert transport.reactions[0]["post_id"] == "auditgen001"
    assert transport.reactions[0]["emoji_name"] == "arrows_counterclockwise"
    await client.aclose()


async def test_audit_mirror_reaction_anchors_unseen_post(settings):
    client, transport = _mirrored_client(settings)
    await client.add_reaction("realalert999", "arrows_counterclockwise")
    # An anchor is created on demand, then the reaction targets it.
    assert len(transport.posts) == 1
    assert transport.reactions[0]["post_id"] == "auditgen001"
    await client.aclose()


async def test_audit_mirror_update_patches_mapped_post(settings):
    client, transport = _mirrored_client(settings)
    root = await client.create_post(channel_id="alerts-channel", message="root")
    await client.update_post(root.id, message="edited")
    assert len(transport.updates) == 1
    path, body = transport.updates[0]
    assert path == "/api/v4/posts/auditgen001/patch"
    assert body["message"] == "edited"
    await client.aclose()


# --- LLM runs in read-only ---------------------------------------------------


async def test_read_only_llm_runs_and_is_not_backstopped(settings):
    """The shadow generates its own summaries/postmortems, so the LLM POST must
    execute in read-only mode (it is read-only-safe) instead of tripping the
    write backstop."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("chat/completions")
        return httpx.Response(200, json={"choices": [{"message": {"content": "SUMMARY"}}]})

    client = PostmortemLlmClient(
        replace(settings, read_only_mode=True, llm_stream=False),
        http_client=httpx.AsyncClient(
            base_url=settings.llm_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    assert await client.generate_summary("prompt") == "SUMMARY"
    assert (await client.preflight_check())["llm_response_length"] == len("SUMMARY")
    await client.aclose()


# --- startup channel-isolation check -----------------------------------------


async def test_audit_channel_collision_refuses_start(settings):
    rs = replace(
        settings,
        read_only_mode=True,
        mattermost_audit_channel_id="alerts-channel",  # collides with the alert channel
    )
    with pytest.raises(RuntimeError, match="dedicated channel"):
        _assert_audit_channel_isolated(rs)


async def test_dedicated_audit_channel_passes_isolation_check(settings):
    rs = replace(
        settings,
        read_only_mode=True,
        mattermost_audit_channel_id="audit-channel",
        mattermost_test_alert_channel_id="test-alert",
    )
    _assert_audit_channel_isolated(rs)  # does not raise
