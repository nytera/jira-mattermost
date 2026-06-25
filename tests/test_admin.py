"""Service-level tests for ``AdminMixin`` (the UI-driven operations behind
``admin_api.py``): create-from-link, Jira recreate, and the lifecycle wrappers
(confirm / end / validity / postmortem / summary).
"""

from __future__ import annotations

import pytest
from support import (
    FakeLlmClient,
    _confirmed_incident,
    make_alert,
)

from mm_jira_bot.domain import ConfirmationStatus
from mm_jira_bot.retry import ApiError

# --------------------------------------------------------------------------- #
# admin_create_from_link
# --------------------------------------------------------------------------- #


async def test_create_from_link_fresh_creates(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.admin_create_from_link(service.mattermost.permalink(post.id))

    assert result.ok is True
    assert result.status == "created"
    assert result.jira_issue_key == "OPS-1"


async def test_create_from_link_repeat_returns_exists_without_second_issue(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    link = service.mattermost.permalink(post.id)

    await service.admin_create_from_link(link)
    result = await service.admin_create_from_link(link)

    assert result.ok is True
    assert result.status == "exists"
    assert result.jira_issue_key == "OPS-1"
    assert len(service.jira.created_payloads) == 1


async def test_create_from_link_resolved_repost_skipped(service):
    post = make_alert(message="**✅ CPU usage is above 95%**")
    service.mattermost.posts[post.id] = post

    result = await service.admin_create_from_link(service.mattermost.permalink(post.id))

    assert result.ok is False
    assert result.status == "skipped"
    assert len(service.jira.created_payloads) == 0


async def test_create_from_link_garbage_returns_invalid_link(service):
    result = await service.admin_create_from_link("just some words")

    assert result.ok is False
    assert result.status == "invalid_link"


async def test_create_from_link_outside_alert_channel_skipped(service):
    post = make_alert(channel_id="some-other-channel")
    service.mattermost.posts[post.id] = post

    result = await service.admin_create_from_link(service.mattermost.permalink(post.id))

    assert result.ok is False
    assert result.status == "skipped"
    assert len(service.jira.created_payloads) == 0


async def test_create_from_link_post_lookup_failure_returns_post_not_found(service):
    post = make_alert()

    async def boom(post_id):
        raise ApiError("mattermost 500")

    service.mattermost.get_post = boom

    result = await service.admin_create_from_link(service.mattermost.permalink(post.id))

    assert result.ok is False
    assert result.status == "post_not_found"
    assert result.mattermost_post_id == post.id


# --------------------------------------------------------------------------- #
# admin_recreate_jira_issue
# --------------------------------------------------------------------------- #


async def test_recreate_creates_issue_for_ticket_without_one(service):
    post = make_alert()
    service.repository.create_or_get_alert(
        post,
        message_url=service.mattermost.permalink(post.id),
        channel_name="alerts",
    )

    result = await service.admin_recreate_jira_issue(post.id)

    assert result.ok is True
    assert result.status == "created"
    assert result.jira_issue_key == "OPS-1"


async def test_recreate_unknown_post_returns_not_found(service):
    result = await service.admin_recreate_jira_issue("doesnotexist00000000000001")

    assert result.ok is False
    assert result.status == "not_found"


async def test_recreate_without_force_conflicts_existing_issue(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.admin_recreate_jira_issue(post.id)

    assert result.ok is False
    assert result.status == "conflict"
    assert len(service.jira.created_payloads) == 1


async def test_recreate_fatal_jira_failure_preserves_state(service):
    post = make_alert()
    service.repository.create_or_get_alert(
        post,
        message_url=service.mattermost.permalink(post.id),
        channel_name="alerts",
    )

    async def boom(ticket):
        raise ApiError("jira is down")

    service._create_jira_issue = boom

    result = await service.admin_recreate_jira_issue(post.id)

    assert result.ok is False
    assert result.status == "error"
    assert result.jira_issue_key is None


# --------------------------------------------------------------------------- #
# admin_confirm_incident
# --------------------------------------------------------------------------- #


async def test_confirm_unknown_post_returns_not_found(service):
    result = await service.admin_confirm_incident("doesnotexist00000000000001")

    assert result.status == ConfirmationStatus.NOT_FOUND


async def test_confirm_publishes_and_confirms_incident(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.admin_confirm_incident(post.id)

    ticket = service.repository.get_by_post_id(post.id)
    assert result.status == ConfirmationStatus.CONFIRMED
    assert ticket is not None
    assert ticket.valid_incident is True
    assert ticket.incident_post_id is not None
    # The synthetic admin actor falls back to the "admin-ui" label when unset.
    assert ticket.confirmed_by_user_id == "admin-ui"


async def test_confirm_attributes_to_configured_admin_user(service):
    from dataclasses import replace

    service.settings = replace(service.settings, admin_mm_user_id="real-admin-id")
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    await service.admin_confirm_incident(post.id)

    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.confirmed_by_user_id == "real-admin-id"


# --------------------------------------------------------------------------- #
# admin_set_validity
# --------------------------------------------------------------------------- #


async def test_set_validity_updates_jira_field(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.admin_set_validity(post.id, validity_label="Ложный")

    ticket = service.repository.get_by_post_id(post.id)
    assert result.status == ConfirmationStatus.VALIDITY_SET
    assert ticket is not None
    assert ticket.validity_label == "Ложный"
    assert ("OPS-1", "Ложный") in service.jira.validity_updates


async def test_set_validity_without_jira_issue_is_pending(service):
    result = await service.admin_set_validity("doesnotexist00000000000001", validity_label="Ложный")

    assert result.status == ConfirmationStatus.PENDING_JIRA


# --------------------------------------------------------------------------- #
# admin_end_incident / admin_generate_postmortem (incident-checkmark path)
# --------------------------------------------------------------------------- #


async def test_end_unknown_post_returns_not_found(service):
    result = await service.admin_end_incident("doesnotexist00000000000001")

    assert result.status == ConfirmationStatus.NOT_FOUND


async def test_end_unpublished_incident_returns_not_found(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)  # alert exists, but no incident published

    result = await service.admin_end_incident(post.id)

    assert result.status == ConfirmationStatus.NOT_FOUND
    assert "не опубликован" in result.message


async def test_end_finalizes_incident_and_generates_postmortem(service):
    service.llm = FakeLlmClient()
    ticket = await _confirmed_incident(service)

    result = await service.admin_end_incident(ticket.mattermost_post_id)

    refreshed = service.repository.get_by_post_id(ticket.mattermost_post_id)
    assert result.status == ConfirmationStatus.INCIDENT_ENDED
    assert refreshed is not None
    # The checkmark/END flow's ticket-level "finalized" signal is the postmortem
    # comment (resolved_at is only set by alert-episode auto-resolution).
    assert refreshed.postmortem_comment_added is True


async def test_generate_postmortem_unknown_post_returns_not_found(service):
    result = await service.admin_generate_postmortem("doesnotexist00000000000001")

    assert result.status == ConfirmationStatus.NOT_FOUND


async def test_generate_postmortem_creates_then_is_idempotent(service):
    service.llm = FakeLlmClient()
    ticket = await _confirmed_incident(service)

    first = await service.admin_generate_postmortem(ticket.mattermost_post_id)
    postmortem_count = len(service.llm.prompts)
    second = await service.admin_generate_postmortem(ticket.mattermost_post_id)

    assert first.status == ConfirmationStatus.INCIDENT_ENDED
    assert postmortem_count == 1
    # The second call must not regenerate the postmortem (comment is additive).
    assert second.status == ConfirmationStatus.INCIDENT_ENDED
    assert "left unchanged" in second.message
    assert len(service.llm.prompts) == 1


def _spy_on_end_time_resolution(service):
    """Replace _resolve_incident_end_time with a recorder; returns the call log."""
    calls: list = []

    async def recorder(post, *, reacted_by_user_id, reaction_ended_at, ticket):
        calls.append(reaction_ended_at)
        return reaction_ended_at

    service._resolve_incident_end_time = recorder
    return calls


async def test_end_with_explicit_time_bypasses_llm_inference(service):
    from datetime import timedelta

    from mm_jira_bot.domain import backend_now

    service.llm = FakeLlmClient()
    ticket = await _confirmed_incident(service)
    calls = _spy_on_end_time_resolution(service)

    explicit = backend_now() - timedelta(hours=3)
    result = await service.admin_end_incident(ticket.mattermost_post_id, ended_at=explicit)

    # An admin-supplied END time is authoritative — LLM inference is skipped.
    assert result.status == ConfirmationStatus.INCIDENT_ENDED
    assert calls == []


async def test_end_without_explicit_time_uses_llm_inference(service):
    service.llm = FakeLlmClient()
    ticket = await _confirmed_incident(service)
    calls = _spy_on_end_time_resolution(service)

    await service.admin_end_incident(ticket.mattermost_post_id)

    # No explicit time -> fall back to LLM inference (like a checkmark reaction).
    assert len(calls) == 1


async def test_generate_postmortem_uses_llm_inference(service):
    service.llm = FakeLlmClient()
    ticket = await _confirmed_incident(service)
    calls = _spy_on_end_time_resolution(service)

    await service.admin_generate_postmortem(ticket.mattermost_post_id)

    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# admin_generate_summary
# --------------------------------------------------------------------------- #


async def test_generate_summary_returns_message(service):
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.admin_generate_summary(post.id)

    assert result.message
    assert len(service.llm.summary_prompts) == 1


async def test_generate_summary_post_lookup_failure_returns_message(service):
    post = make_alert()

    async def boom(post_id):
        raise ApiError("mattermost 500")

    service.mattermost.get_post = boom

    result = await service.admin_generate_summary(post.id)

    assert "Не удалось прочитать" in result.message


@pytest.mark.parametrize("missing_post_id", ["", "x"])
async def test_set_validity_short_post_id_is_pending(service, missing_post_id):
    result = await service.admin_set_validity(missing_post_id, validity_label="Ложный")

    assert result.status == ConfirmationStatus.PENDING_JIRA
