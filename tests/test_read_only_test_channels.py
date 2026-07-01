"""Shadow (read-only) test channels as a live sandbox.

In read-only mode the configured test alert/incident channels are treated as a
first-class, **live** sandbox: an alert pushed into the test alert channel drives a
full incident thread in the test *incident* channel (not the audit mirror), while
Jira stays stubbed — a test alert never creates a real issue. These tests run the
service over the ``support`` fakes; the client-level live-write suppression bypass
is covered in ``test_read_only.py``.
"""

from __future__ import annotations

from dataclasses import replace

from support import _build_service, make_alert

from mm_jira_bot.domain import ReactionEvent


def _shadow_with_test_channels(settings):
    return _build_service(
        replace(
            settings,
            read_only_mode=True,
            mattermost_test_alert_channel_id="test-alert",
            mattermost_test_incident_channel_id="test-incident",
        )
    )


async def _confirm_incident_from_test_alert(service, post_id: str = "talert-1"):
    alert = make_alert(post_id=post_id, channel_id="test-alert")
    service.mattermost.posts[alert.id] = alert
    await service.handle_alert_post(alert)
    await service.handle_reaction(
        ReactionEvent(post_id=alert.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    ticket = service.repository.get_by_post_id(alert.id)
    assert ticket is not None
    return alert, ticket


async def test_incident_from_test_alert_routes_to_test_incident_channel(settings):
    service = _shadow_with_test_channels(settings)
    _alert, ticket = await _confirm_incident_from_test_alert(service)

    incident_posts = [
        c for c in service.mattermost.created_posts if c["post"].id == ticket.incident_post_id
    ]
    assert len(incident_posts) == 1
    # The incident message lands in the test incident channel, not the real one.
    assert incident_posts[0]["channel_id"] == "test-incident"
    # Its thread replies (duty cheat-sheet) go to the same channel.
    thread_replies = [
        c for c in service.mattermost.created_posts if c["root_id"] == ticket.incident_post_id
    ]
    assert thread_replies and all(c["channel_id"] == "test-incident" for c in thread_replies)


async def test_test_alert_keeps_jira_stubbed(settings):
    service = _shadow_with_test_channels(settings)
    _alert, ticket = await _confirm_incident_from_test_alert(service)
    # Test traffic never creates a real Jira issue: the key is a shadow stub and no
    # create payload reached the Jira client.
    assert ticket.jira_issue_key is not None and ticket.jira_issue_key.startswith("ADS-TEST")
    assert service.jira.created_payloads == []


async def test_bot_incident_post_in_test_channel_does_not_spawn_manual_incident(settings):
    """The live incident post echoes back over the websocket from a channel the bot
    processes; a bot-authored root post must be ignored (no manual incident)."""
    service = _shadow_with_test_channels(settings)
    _alert, ticket = await _confirm_incident_from_test_alert(service)
    incident_post = service.mattermost.posts[ticket.incident_post_id]

    before = len(service.mattermost.created_posts)
    await service.handle_manual_incident_post(incident_post)
    # No duty ping / cheat-sheet spawned: the bot ignores its own post.
    assert len(service.mattermost.created_posts) == before
