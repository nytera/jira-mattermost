"""Read-only mode: surface the Jira fields the shadow computes at incident/alert
close (end time, Time-to-Fix, validity) as a code block in the audit thread —
they would otherwise vanish into suppressed Jira writes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from support import _build_service, _confirmed_incident, _reply_text, make_alert

from mm_jira_bot.domain import ReactionEvent, incident_ttf_minutes, runtime_timezone
from mm_jira_bot.jira_payload import format_readonly_jira_params


def _params_posts(service):
    return [c for c in service.mattermost.created_posts if "Параметры Jira" in _reply_text(c)]


# --- pure helpers ------------------------------------------------------------


def test_incident_ttf_minutes_localizes_and_guards():
    tz = runtime_timezone()
    start = datetime(2026, 6, 30, 14, 0, tzinfo=tz)
    assert incident_ttf_minutes(None, start) is None
    assert incident_ttf_minutes(start, start) is None  # non-positive → None
    assert incident_ttf_minutes(start, start + timedelta(minutes=35)) == 35
    # A naive start is localized to the runtime tz (not assumed UTC).
    assert incident_ttf_minutes(start.replace(tzinfo=None), start + timedelta(minutes=35)) == 35


def test_format_readonly_jira_params_codeblock():
    tz = runtime_timezone()
    msg = format_readonly_jira_params(
        jira_issue_key="OPS-1",
        start=datetime(2026, 6, 30, 14, 0, tzinfo=tz),
        ended_at=datetime(2026, 6, 30, 14, 35, tzinfo=tz),
        ttf_minutes=35,
        validity_label="Валидный",
    )
    assert "```" in msg
    assert "OPS-1" in msg
    assert "Time to Fix" in msg and "35 мин" in msg
    assert "Валидный" in msg
    assert "не записаны" in msg


def test_format_readonly_jira_params_omits_missing_rows():
    msg = format_readonly_jira_params(
        jira_issue_key="OPS-1",
        start=None,
        ended_at=None,
        ttf_minutes=None,
        validity_label="Ложный",
    )
    assert "Старт" not in msg and "Конец" not in msg and "Time to Fix" not in msg
    assert "OPS-1" in msg and "Ложный" in msg


# --- integration: posted into the audit thread on close ----------------------


async def test_readonly_incident_close_posts_params_block(settings):
    service = _build_service(replace(settings, read_only_mode=True))
    ticket = await _confirmed_incident(service)

    await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="closer",
            emoji_name="white_check_mark",
            create_at=1_700_000_500_000,
        )
    )

    params = _params_posts(service)
    assert len(params) == 1
    text = _reply_text(params[0])
    assert "Конец" in text and "Time to Fix" in text
    assert params[0]["root_id"] == ticket.incident_post_id


async def test_readonly_alert_validity_posts_params_block(settings):
    service = _build_service(replace(settings, read_only_mode=True))
    alert = make_alert(post_id="alert-1")
    service.mattermost.posts[alert.id] = alert
    await service.handle_alert_post(alert)  # ticket + ADS-TEST stub key

    await service.apply_validity_label(alert.id, validity_label="Ложный", source="reaction")

    params = _params_posts(service)
    assert len(params) == 1
    text = _reply_text(params[0])
    assert "Ложный" in text and "Time to Fix" in text
    assert params[0]["root_id"] == alert.id


async def test_no_params_block_outside_read_only(settings):
    service = _build_service(settings)  # read_only_mode=False
    alert = make_alert(post_id="alert-1")
    service.mattermost.posts[alert.id] = alert
    await service.handle_alert_post(alert)

    await service.apply_validity_label(alert.id, validity_label="Ложный", source="reaction")

    assert _params_posts(service) == []
