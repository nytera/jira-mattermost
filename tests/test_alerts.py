from __future__ import annotations

import json
from dataclasses import replace

import pytest
from support import (
    POST_ID,
    _build_service,
    _issue_reply,
    _reply_text,
    make_alert,
)

from mm_jira_bot.actions import (
    NOTICE_ATTACHMENT_COLOR,
)
from mm_jira_bot.domain import (
    ConfirmationStatus,
    ReactionEvent,
    datetime_from_mattermost_ms,
)
from mm_jira_bot.formatting import (
    alert_signature,
    is_resolved_alert,
)
from mm_jira_bot.jira import VALID_INCIDENT_FALSE_VALUE
from mm_jira_bot.retry import ApiError


@pytest.mark.asyncio
async def test_creates_jira_issue_for_new_mattermost_message(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is not None
    assert ticket.jira_issue_key == "OPS-1"
    assert ticket.mattermost_alert_title == "CPU usage is above 95%"
    assert len(service.jira.created_payloads) == 1


@pytest.mark.asyncio
async def test_ignores_human_message_in_alert_channel(service):
    post = make_alert(post_id="humanalertpost00000000001", is_bot=False)
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is None
    assert service.repository.get_by_post_id(post.id) is None
    assert service.jira.created_payloads == []


@pytest.mark.asyncio
async def test_processes_webhook_slack_attachment_alert(service):
    # Grafana posts via incoming webhook with type "slack_attachment"; this is a
    # real alert and must be processed, not skipped as a system message.
    post = make_alert(post_type="slack_attachment")
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is not None
    assert ticket.jira_issue_key == "OPS-1"


@pytest.mark.asyncio
async def test_skips_system_message_in_alert_channel(service):
    post = make_alert(post_id="systemmsgpost000000000001", post_type="system_join_channel")
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is None
    assert service.repository.get_by_post_id(post.id) is None
    assert service.jira.created_payloads == []


@pytest.mark.asyncio
async def test_stores_grafana_alert_title(service):
    post = make_alert(
        message=(
            "[Деньги | Минус-слова vs Общее | выше на 70% [Crit]]"
            "(https://grafana.wb.ru/alerting/grafana/alert-id/view)\n"
            "State: Alerting"
        )
    )
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is not None
    assert ticket.mattermost_alert_title == ("Деньги | Минус-слова vs Общее | выше на 70% [Crit]")


@pytest.mark.asyncio
async def test_uses_stub_jira_issue_when_creation_disabled(settings):
    service = _build_service(
        replace(
            settings,
            jira_create_enabled=False,
            jira_stub_issue_key="ADSDEV-12024",
            service_public_url="https://bot.example.com/",
            interactive_buttons_enabled=True,
        )
    )
    post = make_alert()
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is not None
    assert ticket.jira_issue_key == f"ADSDEV-12024-{post.id[:12]}"
    assert ticket.jira_issue_url == (f"https://jira.example.com/browse/ADSDEV-12024-{post.id[:12]}")
    assert len(service.jira.created_payloads) == 0
    assert ticket.jira_issue_key is not None
    reply = _issue_reply(service, post.id, issue_key=ticket.jira_issue_key)
    assert reply["message"] == ""
    attachment = reply["props"]["attachments"][0]
    assert "title" not in attachment
    assert "title_link" not in attachment
    assert attachment["text"] == (
        "**Создана задача: [ADSDEV-12024](https://jira.example.com/browse/ADSDEV-12024)**"
    )
    assert ticket.jira_issue_key not in attachment["text"]


@pytest.mark.asyncio
async def test_reuses_display_stub_jira_issue_without_db_conflict(settings):
    service = _build_service(
        replace(
            settings,
            jira_create_enabled=False,
            jira_stub_issue_key="ADSDEV-12024",
        )
    )
    # Distinct messages → distinct signatures, so both are independent root
    # alerts (not a root + expected repeat) and each gets its own stub key.
    first_post = make_alert(post_id="firststubalertpost00000001", message="Disk above 90%")
    second_post = make_alert(post_id="secondstubalertpost0000002", message="Memory above 80%")
    service.mattermost.posts[first_post.id] = first_post
    service.mattermost.posts[second_post.id] = second_post

    first_ticket = await service.handle_alert_post(first_post)
    second_ticket = await service.handle_alert_post(second_post)

    assert first_ticket is not None
    assert second_ticket is not None
    assert first_ticket.jira_issue_key != second_ticket.jira_issue_key
    assert len(service.jira.created_payloads) == 0
    assert first_ticket.jira_issue_key is not None
    assert second_ticket.jira_issue_key is not None
    first_reply = _issue_reply(service, first_post.id, issue_key=first_ticket.jira_issue_key)
    second_reply = _issue_reply(service, second_post.id, issue_key=second_ticket.jira_issue_key)
    assert "Создана задача Jira: [ADSDEV-12024]" in _reply_text(first_reply)
    assert "Создана задача Jira: [ADSDEV-12024]" in _reply_text(second_reply)


@pytest.mark.asyncio
async def test_skips_alert_thread_reply(service):
    post = replace(
        make_alert(post_id="threadreplypost000000000001"),
        root_id=POST_ID,
    )
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is None
    assert len(service.jira.created_payloads) == 0
    assert service.repository.get_by_post_id(post.id) is None


@pytest.mark.asyncio
async def test_skips_resolved_alert_post(service):
    post = make_alert(message="✅ CPU usage is above 95% :: crit")
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is None
    assert len(service.jira.created_payloads) == 0
    assert service.repository.get_by_post_id(post.id) is None


@pytest.mark.asyncio
async def test_skips_resolved_alert_wrapped_in_markdown(service):
    # Grafana sometimes emits the resolved title in bold (``**✅ …**``); the
    # leading ``**`` must not hide the marker and turn a resolve into a firing.
    post = make_alert(message="**✅ Совпадения и различия по Advert Status [Crit] [MM]**")
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is None
    assert len(service.jira.created_payloads) == 0
    assert service.repository.get_by_post_id(post.id) is None


def test_is_resolved_alert_detects_marker_anywhere_on_first_line():
    # A check mark anywhere on the first non-empty line means resolved — markdown
    # wrappers and padding around the marker are irrelevant.
    assert is_resolved_alert("✅ Title")
    assert is_resolved_alert("**✅ Title**")
    assert is_resolved_alert("**:white_check_mark: Title**")
    assert is_resolved_alert("> ✅ Title")
    # A firing alert (even bold-wrapped) must never read as resolved.
    assert not is_resolved_alert("🔴 Title")
    assert not is_resolved_alert("**🔴 Title**")


@pytest.mark.asyncio
async def test_does_not_create_two_issues_for_same_post_id(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)
    await service.handle_alert_post(post)

    assert len(service.jira.created_payloads) == 1


def test_alert_signature_symmetry_link_vs_plain():
    firing = "🔴 [DiskFull](https://grafana.wb.ru/alerting/grafana/abc123/view)\nState: Alerting"
    resolve = "✅ DiskFull"
    assert alert_signature(firing) == alert_signature(resolve)


def test_alert_signature_symmetry_plain():
    assert alert_signature("🔴 CPU usage is above 95%") == alert_signature(
        "✅ CPU usage is above 95%"
    )


@pytest.mark.asyncio
async def test_first_firing_is_root(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket.root_post_id is None
    assert service.mattermost.reactions == []
    assert service.jira.links == []
    assert service.jira.descriptions == []


@pytest.mark.asyncio
async def test_repeat_firing_marked_expected(service):
    root_post = make_alert(post_id="rootpost00000000000000001")
    repeat_post = make_alert(post_id="repeatpost000000000000001")
    for post in (root_post, repeat_post):
        service.mattermost.posts[post.id] = post

    await service.handle_alert_post(root_post)
    await service.handle_alert_post(repeat_post)

    root = service.repository.get_by_post_id(root_post.id)
    repeat = service.repository.get_by_post_id(repeat_post.id)
    assert repeat.root_post_id == root_post.id
    assert repeat.jira_issue_key == "OPS-2"
    # Reaction lands on the repeat message, not the root.
    assert (repeat_post.id, "arrows_counterclockwise") in service.mattermost.reactions
    # Validity "Ожидаемый" on the repeat issue.
    assert ("OPS-2", "Ожидаемый") in service.jira.validity_updates
    assert repeat.validity_label == "Ожидаемый"
    # Description carries the root links.
    description = dict(service.jira.descriptions)["OPS-2"]
    assert root.mattermost_message_url in description
    assert "OPS-1" in description
    # A real Jira "is child of" link from repeat to root.
    assert ("OPS-2", "OPS-1") in service.jira.links
    # And a repeat-alert notice in the thread.
    assert any(
        _reply_text(reply)
        == (
            ":arrows_counterclockwise: **Повторный алерт**\n"
            "Тикет прилинкован к [корневой задаче]"
            "(https://jira.example.com/browse/OPS-1) "
            "(корневая задача первого алерта).\n"
            f"[Корневой алерт]({root.mattermost_message_url})"
        )
        for reply in service.mattermost.created_posts
    )


@pytest.mark.asyncio
async def test_repeat_firing_skips_duty_ping_and_cheat_sheet(settings):
    # A repeat firing is auto-marked expected, so the on-call ping and the duty
    # cheat-sheet are suppressed in its thread — only the root thread gets them.
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com/",
            interactive_buttons_enabled=True,
            mattermost_duty_mention=":look: @sre-ads-duty",
        )
    )
    root_post = make_alert(post_id="rootpost00000000000000010")
    repeat_post = make_alert(post_id="repeatpost000000000000010")
    for post in (root_post, repeat_post):
        service.mattermost.posts[post.id] = post

    await service.handle_alert_post(root_post)
    await service.handle_alert_post(repeat_post)

    # Root thread: duty pinged above the box, cheat-sheet posted.
    assert _issue_reply(service, root_post.id, issue_key="OPS-1")["message"] == (
        ":look: @sre-ads-duty"
    )
    assert any(
        c["root_id"] == root_post.id and "Памятка дежурному" in _reply_text(c)
        for c in service.mattermost.created_posts
    )

    # Repeat thread: "Создана задача" box stays, but no duty ping and no cheat-sheet.
    repeat_replies = [c for c in service.mattermost.created_posts if c["root_id"] == repeat_post.id]
    box = [c for c in repeat_replies if "Создана задача" in _reply_text(c)]
    assert len(box) == 1
    assert box[0]["message"] == ""
    assert all("@sre-ads-duty" not in _reply_text(c) for c in repeat_replies)
    assert all("Памятка дежурному" not in _reply_text(c) for c in repeat_replies)


@pytest.mark.asyncio
async def test_resolve_closes_episode_creates_nothing(service):
    root_post = make_alert(post_id="rootpost00000000000000002")
    repeat_post = make_alert(post_id="repeatpost000000000000002")
    resolve_post = make_alert(
        post_id="resolvepost00000000000002", message="✅ CPU usage is above 95%"
    )
    for post in (root_post, repeat_post, resolve_post):
        service.mattermost.posts[post.id] = post

    await service.handle_alert_post(root_post)
    await service.handle_alert_post(repeat_post)
    issues_before = len(service.jira.created_payloads)
    result = await service.handle_alert_post(resolve_post)

    assert result is None
    assert len(service.jira.created_payloads) == issues_before
    assert service.repository.get_by_post_id(resolve_post.id) is None
    assert service.repository.get_by_post_id(root_post.id).resolved_at is not None


@pytest.mark.asyncio
async def test_refiring_after_resolve_becomes_new_root(service):
    first = make_alert(post_id="firstpost0000000000000001")
    resolve = make_alert(post_id="resolvepost00000000000003", message="✅ CPU usage is above 95%")
    second = make_alert(post_id="secondpost000000000000001")
    for post in (first, resolve, second):
        service.mattermost.posts[post.id] = post

    await service.handle_alert_post(first)
    await service.handle_alert_post(resolve)
    reactions_before = list(service.mattermost.reactions)
    await service.handle_alert_post(second)

    assert service.repository.get_by_post_id(second.id).root_post_id is None
    assert service.mattermost.reactions == reactions_before


@pytest.mark.asyncio
async def test_repeat_redelivery_no_duplicate_link(service):
    root_post = make_alert(post_id="rootpost00000000000000003")
    repeat_post = make_alert(post_id="repeatpost000000000000003")
    for post in (root_post, repeat_post):
        service.mattermost.posts[post.id] = post

    await service.handle_alert_post(root_post)
    await service.handle_alert_post(repeat_post)
    await service.handle_alert_post(repeat_post)

    assert service.jira.links.count(("OPS-2", "OPS-1")) == 1
    notices = [
        reply
        for reply in service.mattermost.created_posts
        if "Повторный алерт" in _reply_text(reply)
    ]
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_bot_own_expected_reaction_is_ignored(service):
    # The bot's own "Ожидаемый" reaction echoes back over the websocket; it must
    # not re-enter the validity path or post an unauthorized notice.
    result = await service.handle_reaction(
        ReactionEvent(
            post_id="anypost", user_id="bot-user", emoji_name="arrows_counterclockwise", create_at=1
        )
    )
    assert result.status.name == "IGNORED"
    assert service.jira.validity_updates == []
    assert service.mattermost.created_posts == []


@pytest.mark.asyncio
async def test_resolve_without_active_root_is_noop(service):
    resolve_post = make_alert(
        post_id="resolvepost00000000000004", message="✅ CPU usage is above 95%"
    )
    service.mattermost.posts[resolve_post.id] = resolve_post

    result = await service.handle_alert_post(resolve_post)

    assert result is None
    assert len(service.jira.created_payloads) == 0


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
    assert service.jira.validity_end_updates == [("OPS-1", datetime_from_mattermost_ms(1))]
    assert ticket.validity_label == "Ложный"
    # Lightweight path: not a confirmed incident, nothing posted to incidents channel.
    assert ticket.valid_incident is False
    assert ticket.incident_post_id is None
    incident_posts = [
        created
        for created in service.mattermost.created_posts
        if created["channel_id"] == "incidents-channel" and created["root_id"] is None
    ]
    assert incident_posts == []
    validity_replies = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post.id and "Ложный" in _reply_text(created)
    ]
    assert len(validity_replies) == 1


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
    assert service.jira.validity_end_updates == [("OPS-1", datetime_from_mattermost_ms(1))]


@pytest.mark.asyncio
async def test_last_validity_reaction_wins(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="v", emoji_name="incident", create_at=1)
    )
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="v", emoji_name="man_gesturing_no", create_at=2)
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
        ReactionEvent(post_id=post.id, user_id="v", emoji_name="man_gesturing_no", create_at=1)
    )
    result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="v", emoji_name="man_gesturing_no", create_at=2)
    )

    assert result.status == "validity_set"
    assert service.jira.validity_updates == [("OPS-1", "Ложный")]
    validity_replies = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post.id and "Валидность" in _reply_text(created)
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
async def test_checkmark_in_alert_channel_creates_no_issue(service):
    # A checkmark belongs to the incident channel; in the alert channel it must be
    # ignored and must NOT create a Jira issue as a side effect.
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="v", emoji_name="white_check_mark", create_at=1)
    )

    assert result.status == "ignored"
    assert service.jira.created_payloads == []
    assert service.repository.get_by_post_id(post.id) is None


@pytest.mark.asyncio
async def test_replies_in_alert_thread_when_issue_created(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)
    assert reply["channel_id"] == "alerts-channel"
    assert "OPS-1" in _reply_text(reply)
    assert "https://jira.example.com/browse/OPS-1" in _reply_text(reply)


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
        created for created in service.mattermost.created_posts if created["root_id"] == post.id
    ]
    # Issue-created reply + duty cheat-sheet + one status-change reply; no
    # duplicate status reply on retry.
    status_replies = [r for r in thread_replies if "Инцидент заведен" in _reply_text(r)]
    assert len(status_replies) == 1
    status_reply = status_replies[0]
    assert status_reply["channel_id"] == "alerts-channel"
    # The notice renders as a boxed attachment, not a bare message: text moves
    # into a single NOTICE-colored block with fallback set for push/preview.
    assert status_reply["message"] == ""
    box = status_reply["props"]["attachments"][0]
    assert box["color"] == NOTICE_ATTACHMENT_COLOR
    assert "Инцидент заведен" in box["text"]
    assert "Ссылка на инцидент" in box["text"]
    assert box["fallback"]

    incident_posts = [
        created
        for created in service.mattermost.created_posts
        if created["channel_id"] == "incidents-channel" and created["root_id"] is None
    ]
    assert len(incident_posts) == 1
    info_text = incident_posts[0]["props"]["attachments"][0]["text"]
    assert "Подтвердил: @validator" in info_text


@pytest.mark.asyncio
async def test_feedback_button_opens_dialog(settings):
    service = _build_service(replace(settings, service_public_url="https://bot.example.com/"))
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.handle_alert_action(
        action="feedback",
        alert_post_id=post.id,
        user_id="clicker",
        trigger_id="trigger-1",
    )

    assert "Открыта форма" in result.message
    assert service.mattermost.opened_dialogs == [
        {
            "trigger_id": "trigger-1",
            "url": "https://bot.example.com/mattermost/dialogs/feedback",
            "dialog": {
                "callback_id": "alert_feedback",
                "title": "Обратная связь",
                "introduction_text": "Оставьте комментарий по этому алерту.",
                "elements": [
                    {
                        "display_name": "Комментарий",
                        "name": "feedback",
                        "type": "textarea",
                        "placeholder": "Что стоит улучшить?",
                        "max_length": 3000,
                    }
                ],
                "submit_label": "Отправить",
                "state": json.dumps({"alert_post_id": post.id}, ensure_ascii=False),
            },
        }
    ]
    assert service.jira.validity_updates == []


@pytest.mark.asyncio
async def test_feedback_dialog_submission_stores_feedback_and_posts_notice(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    service.mattermost.display_names["clicker"] = "@clicker"
    await service.handle_alert_post(post)

    result = await service.handle_feedback_dialog_submission(
        user_id="clicker",
        state=json.dumps({"alert_post_id": post.id}),
        submission={"feedback": "Кнопки стали понятнее"},
    )

    assert result.message == ""
    feedback = service.repository.list_feedback(post.id)
    assert len(feedback) == 1
    assert feedback[0].user_id == "clicker"
    assert feedback[0].user_display_name == "@clicker"
    assert feedback[0].message == "Кнопки стали понятнее"
    notices = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post.id
        and "Получили обратную связь от @clicker" in _reply_text(created)
    ]
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_service_marks_ticket_error_on_permanent_jira_create_failure(service):
    # A non-retryable Jira create failure must not invent an issue: the ticket is
    # left without a key, and the thread gets no "Создана задача" reply pointing at
    # a nonexistent issue. (Mirrors the TTF-failure characterization pattern: a
    # raising fake client method, asserting the side effect is suppressed.)
    async def _boom(post, *, message_url, channel_name):
        raise ApiError("jira down", retryable=False)

    service.jira.create_issue = _boom
    post = make_alert()
    service.mattermost.posts[post.id] = post

    ticket = await service.handle_alert_post(post)

    assert ticket is not None
    # No Jira issue was created and none was attached to the ticket.
    assert service.jira.created_payloads == []
    assert ticket.jira_issue_key is None
    # Actual behavior: the create-failure path records the failure on
    # ``creation_status``/``last_error`` (NOT ``confirmation_status``, which stays
    # at its default "none"). See caveat in the return summary.
    assert ticket.creation_status == "failed_jira"
    assert ticket.last_error
    assert ticket.confirmation_status == "none"
    assert ticket.confirmation_status != ConfirmationStatus.ERROR
    # The except branch posts nothing, so there is no thread reply at all and in
    # particular none announcing a (nonexistent) issue.
    assert service.mattermost.created_posts == []
    assert not any(
        "Создана задача" in _reply_text(created) for created in service.mattermost.created_posts
    )


@pytest.mark.asyncio
async def test_apply_validity_label_pending_vs_error(service):
    # PENDING branch: a ticket without a Jira key yet → PENDING_JIRA, and
    # apply_validity_label itself must not touch set_last_error.
    real_create_issue = service.jira.create_issue

    async def _boom(post, *, message_url, channel_name):
        raise ApiError("jira down", retryable=False)

    service.jira.create_issue = _boom
    pending_post = make_alert(
        post_id="pendingvaliditypost0000001", message="Pending disk above 90%"
    )
    service.mattermost.posts[pending_post.id] = pending_post
    await service.handle_alert_post(pending_post)

    pending_ticket = service.repository.get_by_post_id(pending_post.id)
    assert pending_ticket.jira_issue_key is None

    # Spy on set_last_error from this point on, isolating the apply path. (The
    # create-failure setup above already wrote last_error via mark_jira_create_failed,
    # so we count fresh calls rather than assert the field is None.)
    last_error_calls: list[tuple] = []
    real_set_last_error = service.repository.set_last_error

    def _spy(post_id, error):
        last_error_calls.append((post_id, error))
        return real_set_last_error(post_id, error)

    service.repository.set_last_error = _spy

    pending_result = await service.apply_validity_label(
        pending_post.id, validity_label=VALID_INCIDENT_FALSE_VALUE, source="action"
    )

    assert pending_result.status == ConfirmationStatus.PENDING_JIRA
    assert last_error_calls == []
    assert service.jira.validity_updates == []

    # ERROR branch: a successfully created ticket (last_error reset to None on
    # attach), then set_validity raises → ERROR, last_error persisted, and the
    # validity_label is NOT written.
    ok_post = make_alert(post_id="errorvaliditypost000000001", message="Error memory above 80%")
    service.mattermost.posts[ok_post.id] = ok_post
    service.jira.create_issue = real_create_issue
    await service.handle_alert_post(ok_post)

    ok_ticket = service.repository.get_by_post_id(ok_post.id)
    assert ok_ticket.jira_issue_key is not None
    assert ok_ticket.last_error is None

    async def _validity_boom(issue_key, option_value, *, ended_at=None):
        raise ApiError("validity field rejected", retryable=False)

    service.jira.set_validity = _validity_boom

    error_result = await service.apply_validity_label(
        ok_post.id, validity_label=VALID_INCIDENT_FALSE_VALUE, source="action"
    )

    assert error_result.status == ConfirmationStatus.ERROR
    refreshed = service.repository.get_by_post_id(ok_post.id)
    assert refreshed.last_error == "validity field rejected"
    assert refreshed.validity_label is None
    assert service.jira.validity_updates == []
