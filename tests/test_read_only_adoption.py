"""Read-only (shadow) mode — Increment 2: adopting prod artifacts.

The shadow watches prod-bot posts in the **real** alert/incident channels and
adopts the real Jira key (replacing the ``ADS-TEST`` stub) and the real incident
post id, so the audit mirror shows real links and a ✅ on the real prod incident
post resolves to the shadow's ticket. These tests run the service over the
``support`` fakes (no real transport needed — adoption is pure service/DB logic).
"""

from __future__ import annotations

from dataclasses import replace

from support import _build_service, _reply_text, make_alert

from mm_jira_bot.domain import MattermostPost
from mm_jira_bot.repository import AlertTicket


def _shadow(settings, **overrides):
    return _build_service(replace(settings, read_only_mode=True, **overrides))


def _ticket(service, post_id: str) -> AlertTicket:
    ticket = service.repository.get_by_post_id(post_id)
    assert ticket is not None
    return ticket


async def _seed_stub_ticket(service, post_id: str = "alert-1") -> MattermostPost:
    """Run the shadow's normal alert path so the ticket exists with a stub key."""
    alert = make_alert(post_id=post_id)
    service.mattermost.posts[alert.id] = alert
    await service.handle_alert_post(alert)
    key = _ticket(service, alert.id).jira_issue_key
    assert key is not None and key.startswith("ADS-TEST")
    return alert


def _prod_post(channel_id: str, *, post_id: str, alert_post_id: str, root_id=None, **props):
    return MattermostPost(
        id=post_id,
        channel_id=channel_id,
        user_id="prod-bot",
        message="",
        create_at=1_700_000_200_000,
        root_id=root_id,
        props={"mattermost_alert_post_id": alert_post_id, **props},
    )


def _adoption_notes(service, needle: str, *, channel_id: str | None = None):
    return [
        c
        for c in service.mattermost.created_posts
        if needle.lower() in _reply_text(c).lower()
        and (channel_id is None or c["channel_id"] == channel_id)
    ]


# --- Jira key adoption -------------------------------------------------------


async def test_adopts_real_jira_key_from_prod_alert_reply(settings):
    service = _shadow(settings)
    alert = await _seed_stub_ticket(service)

    reply = _prod_post(
        "alerts-channel", post_id="prod-reply", alert_post_id=alert.id, jira_issue_key="OPS-42"
    )
    consumed = await service._observe_prod_artifact(reply)

    assert consumed is True
    ticket = _ticket(service, alert.id)
    assert ticket.jira_issue_key == "OPS-42"
    assert (ticket.jira_issue_url or "").endswith("/browse/OPS-42")
    notes = _adoption_notes(service, "Усыновлён реальный Jira")
    assert len(notes) == 1
    assert "OPS-42" in _reply_text(notes[0])
    assert notes[0]["root_id"] == alert.id


async def test_jira_adoption_is_idempotent_first_wins(settings):
    service = _shadow(settings)
    alert = await _seed_stub_ticket(service)

    await service._observe_prod_artifact(
        _prod_post("alerts-channel", post_id="r1", alert_post_id=alert.id, jira_issue_key="OPS-42")
    )
    # A later prod notice (different key) must NOT re-adopt — the stub is gone.
    await service._observe_prod_artifact(
        _prod_post("alerts-channel", post_id="r2", alert_post_id=alert.id, jira_issue_key="OPS-99")
    )

    assert _ticket(service, alert.id).jira_issue_key == "OPS-42"
    assert len(_adoption_notes(service, "Усыновлён реальный Jira")) == 1


async def test_no_stub_yet_skips_adoption(settings):
    """If the shadow hasn't created its own stub yet (no jira_issue_key), adoption
    is skipped — a later prod notice re-attempts once the stub exists."""
    service = _shadow(settings)
    alert = make_alert(post_id="alert-x")
    service.mattermost.posts[alert.id] = alert
    service.repository.create_or_get_alert(
        alert, message_url=service.mattermost.permalink(alert.id), channel_name="alerts"
    )
    assert _ticket(service, alert.id).jira_issue_key is None

    await service._observe_prod_artifact(
        _prod_post("alerts-channel", post_id="r1", alert_post_id=alert.id, jira_issue_key="OPS-7")
    )
    assert _ticket(service, alert.id).jira_issue_key is None
    assert _adoption_notes(service, "Усыновлён реальный Jira") == []


# --- Incident post adoption + two-field lookup -------------------------------


async def test_adopts_prod_incident_post_and_two_field_lookup(settings):
    service = _shadow(settings)
    alert = await _seed_stub_ticket(service)

    incident = _prod_post(
        "incidents-channel",
        post_id="prod-incident",
        alert_post_id=alert.id,
        jira_issue_key="OPS-42",
    )
    consumed = await service._observe_prod_artifact(incident)

    assert consumed is True
    ticket = _ticket(service, alert.id)
    assert ticket.prod_incident_post_id == "prod-incident"
    # The same post adopts the real Jira key too.
    assert ticket.jira_issue_key == "OPS-42"
    # The ✅ on the real prod incident post must resolve to this ticket.
    found = service.repository.get_by_incident_post_id("prod-incident")
    assert found is not None and found.mattermost_post_id == alert.id
    notes = _adoption_notes(service, "усыновлён с прода", channel_id="incidents-channel")
    assert len(notes) == 1
    assert notes[0]["root_id"] == "prod-incident"


async def test_incident_adoption_is_idempotent(settings):
    service = _shadow(settings)
    alert = await _seed_stub_ticket(service)
    incident = _prod_post("incidents-channel", post_id="prod-incident", alert_post_id=alert.id)

    await service._observe_prod_artifact(incident)
    await service._observe_prod_artifact(incident)

    assert len(_adoption_notes(service, "усыновлён с прода")) == 1


class _AliasRecorder:
    def __init__(self) -> None:
        self.aliases: list[tuple[str, str]] = []

    def adopt_alias(self, original_id: str, alias_id: str) -> None:
        self.aliases.append((original_id, alias_id))


async def test_adopt_incident_post_uses_fresh_incident_post_id(settings):
    """Regression: the alias must use the CURRENT incident_post_id, not the stale
    snapshot the observer captured — the shadow's own confirm may have published its
    incident message concurrently (same ✅-on-alert cause)."""
    service = _shadow(settings)
    alert = await _seed_stub_ticket(service)
    recorder = _AliasRecorder()
    service.mattermost.audit = recorder

    # Stale snapshot: no incident_post_id on this captured object...
    stale = _ticket(service, alert.id)
    assert stale.incident_post_id is None
    # ...but the row gains one concurrently (the shadow's own confirm path).
    service.repository.set_incident_message(alert.id, "readonly-shadow-inc", "url")

    await service._adopt_prod_incident_post(stale, "prod-incident")

    # The fresh re-read picks up the concurrently-set id (stale snapshot would skip).
    assert recorder.aliases == [("readonly-shadow-inc", "prod-incident")]


async def test_incident_thread_reply_does_not_adopt_incident_post(settings):
    """Only the incident ROOT post carries the prod incident post id; a thread
    reply (root_id set) must not be recorded as one."""
    service = _shadow(settings)
    alert = await _seed_stub_ticket(service)

    reply = _prod_post(
        "incidents-channel",
        post_id="prod-incident-reply",
        alert_post_id=alert.id,
        root_id="prod-incident",
    )
    await service._observe_prod_artifact(reply)

    assert _ticket(service, alert.id).prod_incident_post_id is None


# --- Channel gate & guards ---------------------------------------------------


async def test_test_channel_prod_artifact_is_not_adopted(settings):
    service = _shadow(settings, mattermost_test_alert_channel_id="test-alert")
    alert = await _seed_stub_ticket(service)

    consumed = await service._observe_prod_artifact(
        _prod_post("test-alert", post_id="r1", alert_post_id=alert.id, jira_issue_key="OPS-42")
    )

    # Positive gate: only the REAL alert/incident channels are observed.
    assert consumed is False
    key = _ticket(service, alert.id).jira_issue_key
    assert key is not None and key.startswith("ADS-TEST")


async def test_post_without_correlation_prop_falls_through(settings):
    service = _shadow(settings)
    plain = MattermostPost(
        id="raw-alert",
        channel_id="alerts-channel",
        user_id="webhook",
        message="CPU high",
        create_at=1,
        props={"from_webhook": "true"},
    )
    assert await service._observe_prod_artifact(plain) is False


async def test_uncorrelated_prod_artifact_is_consumed_without_adoption(settings):
    """A prod artifact the shadow can't correlate (never saw the alert) is consumed
    so it doesn't fall through, but nothing is adopted."""
    service = _shadow(settings)
    consumed = await service._observe_prod_artifact(
        _prod_post("alerts-channel", post_id="r1", alert_post_id="unknown", jira_issue_key="OPS-1")
    )
    assert consumed is True
    assert service.repository.get_by_post_id("unknown") is None


async def test_observer_is_noop_outside_read_only(settings):
    service = _build_service(settings)  # read_only_mode=False
    consumed = await service._observe_prod_artifact(
        _prod_post("alerts-channel", post_id="r1", alert_post_id="alert-1", jira_issue_key="OPS-1")
    )
    assert consumed is False
