from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from support import (
    FakeLlmClient,
    _build_service,
    _issue_reply,
    _manual_post,
    _reply_text,
    make_alert,
)

from mm_jira_bot.colors import (
    DUTY_HELP_ATTACHMENT_COLOR,
)
from mm_jira_bot.domain import (
    ConfirmationResult,
    ConfirmationStatus,
    MattermostPost,
    ReactionEvent,
    backend_now,
    datetime_from_mattermost_ms,
)
from mm_jira_bot.formatting import (
    format_alert_duty_help,
    format_incident_duty_help,
    format_incident_message,
    format_incident_title,
)
from mm_jira_bot.retry import ApiError


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
    assert service.jira.validity_end_updates == []
    assert len(service.jira.comments) == 1
    assert len(service.jira.descriptions) == 1
    issue_key, description = service.jira.descriptions[0]
    assert issue_key == "OPS-1"
    assert "h2. Хронология" in description
    assert ticket.incident_message_url in description
    assert ticket.mattermost_message_url in description


@pytest.mark.asyncio
async def test_confirmed_incident_embeds_grafana_attachment(service):
    attachments = [
        {
            "color": "#F2495C",
            "title": "Деньги | Минус-слова vs Общее | выше на 70% [Crit]",
            "title_link": "http://grafana.wb.ru/alerting/grafana/alert-id/view",
            "text": "Runbook: https://wiki.example.com/runbook",
            "image_url": "http://grafana.wb.ru/render/d-solo/dashboard/panel.png",
            "footer": "Grafana v12.4.2",
        }
    ]
    post = make_alert(
        message="Деньги | Минус-слова vs Общее | выше на 70% [Crit]",
        props={"attachments": attachments},
    )
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )

    incident_posts = [
        created
        for created in service.mattermost.created_posts
        if created["channel_id"] == "incidents-channel" and created["root_id"] is None
    ]
    assert len(incident_posts) == 1
    incident_post = incident_posts[0]
    post_attachments = incident_post["props"]["attachments"]
    # Three boxes: title, detail, then the forwarded alert attachment(s) (a copy).
    title_block = post_attachments[0]
    info_block = post_attachments[1]
    assert title_block["color"] == "#EF4444"
    assert info_block["color"] == "#EF4444"
    assert post_attachments[2:] == attachments
    assert post_attachments[2] is not attachments[0]
    # Top box is just the alert name as a heading.
    assert title_block["text"] == "##### Деньги | Минус-слова vs Общее | выше на 70% [Crit]"
    info_text = info_block["text"]
    # Detail box: status line carries the Jira link, no separate "Задача Jira".
    assert info_text.startswith("**Новый инцидент** — [OPS-1]")
    assert "Задача Jira" not in info_text
    assert "Исходный алерт" in info_text
    # The full alert text is in the forwarded block, not duplicated in the
    # detail box (the alert name lives only in the title box).
    assert "Деньги | Минус-слова vs Общее" not in info_text
    assert incident_post["message"] == ""


@pytest.mark.asyncio
async def test_checkmark_on_incident_post_sets_end_time(service):
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
    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.incident_post_id is not None

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="closer",
            emoji_name="white_check_mark",
            create_at=1_700_000_200_000,
        )
    )

    assert result.status == "incident_ended"
    assert service.jira.end_updates == [("OPS-1", datetime_from_mattermost_ms(1_700_000_200_000))]
    assert service.jira.validity_end_updates == []


@pytest.mark.asyncio
async def test_checkmark_on_incident_thread_reply_is_ignored(service):
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
    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.incident_post_id is not None
    reply = MattermostPost(
        id="incidentreply000000000001",
        channel_id="incidents-channel",
        user_id="closer",
        message="done",
        create_at=1_700_000_150_000,
        root_id=ticket.incident_post_id,
    )
    service.mattermost.posts[reply.id] = reply

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=reply.id,
            user_id="closer",
            emoji_name="heavy_check_mark",
            create_at=1_700_000_250_000,
        )
    )

    assert result.status == "ignored"
    assert service.jira.end_updates == []


@pytest.mark.asyncio
async def test_checkmark_on_incident_thread_generates_postmortem_for_existing_issue(service):
    service.llm = FakeLlmClient()
    service.mattermost.display_names.update(
        {
            "validator": "Иван Иванов (@ivanov.ivan)",
            "closer": "Петр Петров (@petrov.petr)",
            "bot-user": "@incident-bot",
        }
    )
    post = make_alert(message="API 500 on checkout")
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
    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.incident_post_id is not None
    reply = MattermostPost(
        id="incidentreply000000000002",
        channel_id="incidents-channel",
        user_id="closer",
        message="Откатили релиз, ошибки ушли.",
        create_at=1_700_000_250_000,
        root_id=ticket.incident_post_id,
    )
    service.mattermost.posts[reply.id] = reply

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="validator",
            emoji_name="white_check_mark",
            create_at=1_700_000_300_000,
        )
    )

    assert result.status == "incident_ended"
    assert service.jira.end_updates == [("OPS-1", datetime_from_mattermost_ms(1_700_000_300_000))]
    assert len(service.jira.created_payloads) == 1
    # One LLM call at close (the closeout: end time + title), fed the transcript.
    assert len(service.llm.prompts) == 1
    prompt = service.llm.prompts[0]
    assert "API 500 on checkout" in prompt
    assert "Откатили релиз" in prompt
    issue_key, description = service.jira.descriptions[-1]
    assert issue_key == "OPS-1"
    assert "|*Авторы ПМ*|Иван Иванов (@ivanov.ivan)|" in description
    assert "|*Участники инцидента*|" in description
    assert "Иван Иванов (@ivanov.ivan)" in description
    assert "Петр Петров (@petrov.petr)" in description
    assert "[Основное сообщение инцидента|" in description
    assert ticket.incident_message_url in description
    assert ticket.mattermost_message_url in description
    assert "h2. Извлеченные уроки" in description
    assert "h2. Action Items" in description
    assert "h2. Хронология" in description
    assert "API начал отвечать 500." not in description
    assert "##Хронология" not in description
    # No postmortem comment and no auto thread summary on close (button-only now);
    # closure is announced in a standalone green box posted as a separate reply.
    assert service.jira.generic_comments == []
    assert not any(
        created["root_id"] == ticket.incident_post_id
        and ("Генерация саммари" in _reply_text(created) or "Саммари треда" in _reply_text(created))
        for created in service.mattermost.created_posts
    )
    closed = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == ticket.incident_post_id
        and "Инцидент закрыт" in _reply_text(created)
    ]
    assert len(closed) == 1
    assert "ПМ:" in _reply_text(closed[0])
    assert "INC" in _reply_text(closed[0])  # link text is the task title (brackets escaped)
    assert closed[0]["props"]["attachments"][0]["color"] == "#22C55E"


@pytest.mark.asyncio
async def test_checkmark_on_manual_incident_thread_creates_postmortem_issue(service):
    service.llm = FakeLlmClient()
    service.mattermost.display_names.update(
        {
            "author": "Анна Автор (@author.anna)",
            "closer": "Иван Иванов (@ivanov.ivan)",
        }
    )
    root = MattermostPost(
        id="manualincidentroot000000001",
        channel_id="incidents-channel",
        user_id="author",
        message="Создали инцидент по росту 500 на checkout.",
        create_at=1_700_000_100_000,
        channel_name="incidents",
    )
    reply = MattermostPost(
        id="manualincidentreply00000001",
        channel_id="incidents-channel",
        user_id="closer",
        message="Проблема была в релизе, откат помог.",
        create_at=1_700_000_200_000,
        root_id=root.id,
    )
    service.mattermost.posts[root.id] = root
    service.mattermost.posts[reply.id] = reply

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=root.id,
            user_id="closer",
            emoji_name="white_check_mark",
            create_at=1_700_000_300_000,
        )
    )

    assert result.status == "incident_ended"
    ticket = service.repository.get_by_incident_post_id(root.id)
    assert ticket is not None
    assert ticket.jira_issue_key == "OPS-1"
    assert ticket.valid_incident is True
    assert len(service.jira.postmortem_payloads) == 1
    payload = service.jira.postmortem_payloads[0]
    assert payload["summary"] == "[INC] 15.11.2023 - Ошибки API"
    assert "[Основное сообщение инцидента|" in payload["description"]
    assert "Анна Автор (@author.anna)" in payload["description"]
    assert "Иван Иванов (@ivanov.ivan)" in payload["description"]
    assert "API начал отвечать 500." not in payload["description"]
    assert service.jira.valid_updates == [("OPS-1", True)]
    assert service.jira.end_updates == [("OPS-1", datetime_from_mattermost_ms(1_700_000_300_000))]
    # No postmortem comment and no auto thread summary on close (button-only now):
    # the thread only gets the standalone green "closed" notice below.
    assert service.jira.generic_comments == []
    assert not any(
        created["root_id"] == root.id
        and ("Генерация саммари" in _reply_text(created) or "Саммари треда" in _reply_text(created))
        for created in service.mattermost.created_posts
    )
    closed = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == root.id and "Инцидент закрыт" in _reply_text(created)
    ]
    assert len(closed) == 1
    assert "ПМ: [\\[INC\\] 15.11.2023 - Ошибки API]" in _reply_text(closed[0])
    assert closed[0]["props"]["attachments"][0]["color"] == "#22C55E"


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

    incident_posts = [
        created
        for created in service.mattermost.created_posts
        if created["channel_id"] == "incidents-channel" and created["root_id"] is None
    ]
    assert len(incident_posts) == 1
    assert len(service.jira.comments) == 1


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

    # Top box: just the alert name as a heading.
    assert format_incident_title(ticket) == "##### CPU usage is above 95%"

    message = format_incident_message(
        ticket,
        author="@validator",
        alert_at=datetime(2026, 5, 29, 22, 30, tzinfo=UTC),
    )

    # Detail box: status line carries the Jira link (no separate "Задача Jira"
    # bullet), followed by source/author/alert-time bullets.
    assert (
        message.splitlines()[0]
        == "**Новый инцидент** — [OPS-1](https://jira.example.com/browse/OPS-1)"
    )
    assert "Задача Jira" not in message
    assert "Исходный алерт" in message
    assert "Автор: @validator" in message
    assert "Время алерта: 30.05.2026 01:30" in message


@pytest.mark.asyncio
async def test_issue_reply_has_no_buttons_without_public_url(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)
    # Plain boxed "Создана задача" notice — no interactive button/menu controls.
    assert "Создана задача Jira" in _reply_text(reply)
    assert all("actions" not in a for a in reply["props"].get("attachments", []))


@pytest.mark.asyncio
async def test_firing_alert_pings_duty_above_box_emoji_only(settings):
    # Emoji-only mode: notice is boxed, duty @mention stays bare above it.
    service = _build_service(replace(settings, mattermost_duty_mention=":look: @sre-ads-duty"))
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)
    assert reply["message"] == ":look: @sre-ads-duty"
    assert "Создана задача Jira" in reply["props"]["attachments"][0]["text"]


@pytest.mark.asyncio
async def test_firing_alert_no_duty_ping_when_unset(service):
    # No MATTERMOST_DUTY_MENTION → message stays empty (no regression).
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)
    assert reply["message"] == ""


@pytest.mark.asyncio
async def test_manual_incident_pings_duty_in_emoji_only_mode(settings):
    service = _build_service(replace(settings, mattermost_duty_mention=":look: @sre-ads-duty"))
    post = _manual_post()
    service.mattermost.posts[post.id] = post

    await service.handle_manual_incident_post(post)

    replies = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    # Bare duty @mention first, then the duty cheat-sheet notice.
    assert len(replies) == 2
    assert replies[0]["message"] == ":look: @sre-ads-duty"
    assert not (replies[0]["props"] or {}).get("attachments")
    assert "Памятка дежурному" in _reply_text(replies[1])

    # Idempotent: a redelivered event does not post a second ping or cheat-sheet.
    await service.handle_manual_incident_post(post)
    replies = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    assert len(replies) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "post",
    [
        _manual_post(user_id="bot-user"),
        _manual_post(props={"from_bot": "true"}),
        _manual_post(props={"from_webhook": "true"}),
        _manual_post(root_id="someroottttttttttttttttttt"),
    ],
)
async def test_manual_incident_ignores_bots_and_replies(settings, post):
    service = _build_service(settings)
    await service.handle_manual_incident_post(post)
    assert service.mattermost.created_posts == []


@pytest.mark.asyncio
async def test_manual_incident_no_controls_without_public_url(settings):
    service = _build_service(settings)
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)
    replies = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    # Emoji-only: no create-task card, only the duty cheat-sheet notice.
    assert len(replies) == 1
    assert "Памятка дежурному" in _reply_text(replies[0])
    assert all("actions" not in a for a in (replies[0]["props"] or {}).get("attachments", []))


@pytest.mark.asyncio
async def test_pending_work_ignores_uncreated_manual_card(settings):
    """The pre-created card row stays keyless: the background loop must not
    auto-create a Jira issue for it (that would defeat the button gating)."""
    service = _build_service(settings)
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)

    await service.process_pending_work()

    ticket = service.repository.get_by_incident_post_id(post.id)
    assert ticket is not None
    assert ticket.jira_issue_key is None
    assert service.jira.created_payloads == []


@pytest.mark.asyncio
async def test_completing_alert_incident_updates_title_to_done(settings):
    service = _build_service(settings)
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.incident_post_id is not None
    incident_post_id = ticket.incident_post_id

    def attachments():
        return service.mattermost.posts[incident_post_id].props["attachments"]

    # Status label lives in the detail box (index 1); title box (0) is the name.
    def info_text():
        return attachments()[1]["text"]

    assert "**Новый инцидент**" in info_text()

    await service.handle_reaction(
        ReactionEvent(
            post_id=incident_post_id,
            user_id="closer",
            emoji_name="white_check_mark",
            create_at=1_700_000_300_000,
        )
    )

    assert "**Закрытый инцидент**" in info_text()
    assert "**Новый инцидент**" not in info_text()
    # Every box flips to the done color, including the title box.
    assert all(a["color"] == "#22C55E" for a in attachments())


# --- Duty cheat-sheet -------------------------------------------------------


def test_alert_duty_help_lists_configured_reactions():
    text = format_alert_duty_help(
        incident_emoji="incident",
        false_emoji="man_gesturing_no",
        expected_emoji="arrows_counterclockwise",
        summary_emoji="memo",
    )
    assert ":incident:" in text
    assert ":man_gesturing_no:" in text
    assert ":arrows_counterclockwise:" in text
    assert ":memo:" in text
    assert "завести инцидент" in text
    assert "саммари" in text.lower()
    # No button hints, as buttons are off by default.
    assert "кнопк" not in text.lower()


def test_alert_duty_help_uses_custom_emoji_names():
    text = format_alert_duty_help(
        incident_emoji="fire",
        false_emoji="no_entry",
        expected_emoji="repeat",
        summary_emoji="scroll",
    )
    assert ":fire:" in text and ":no_entry:" in text and ":repeat:" in text and ":scroll:" in text


def test_incident_duty_help_lists_checkmark_reaction():
    text = format_incident_duty_help(
        false_emoji="man_gesturing_no",
        expected_emoji="arrows_counterclockwise",
        summary_emoji="memo",
    )
    assert "✅" in text
    # Validity reactions here close the incident (not the alert's label-only
    # meaning), and the summary emoji is listed too. No "постмортем": the narrative
    # is not produced on close, only via the summary reaction.
    assert ":man_gesturing_no:" in text and ":arrows_counterclockwise:" in text
    assert ":memo:" in text
    assert "завершить инцидент" in text.lower()
    assert "постмортем" not in text.lower()
    assert "кнопк" not in text.lower()


@pytest.mark.asyncio
async def test_firing_alert_posts_duty_help(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    help_replies = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == post.id and "Памятка дежурному" in _reply_text(c)
    ]
    assert len(help_replies) == 1
    assert ":incident:" in _reply_text(help_replies[0])
    assert help_replies[0]["props"]["attachments"][0]["color"] == DUTY_HELP_ATTACHMENT_COLOR


@pytest.mark.asyncio
async def test_manual_incident_posts_duty_help(service):
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)
    help_replies = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == post.id and "Памятка дежурному" in _reply_text(c)
    ]
    assert len(help_replies) == 1
    assert "✅" in _reply_text(help_replies[0])


@pytest.mark.asyncio
async def test_duty_help_disabled_posts_nothing(settings):
    service = _build_service(replace(settings, duty_help_enabled=False))
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    assert not [
        c for c in service.mattermost.created_posts if "Памятка дежурному" in _reply_text(c)
    ]


@pytest.mark.asyncio
async def test_alert_originated_incident_posts_its_own_duty_help(service):
    # The incident-channel thread (alert-originated) gets its own cheat-sheet:
    # validity reactions here mean "close the incident", unlike the alert thread.
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    ticket = service.repository.get_by_post_id(post.id)
    incident_help = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == ticket.incident_post_id and "Памятка дежурному" in _reply_text(c)
    ]
    assert len(incident_help) == 1
    assert "завершить инцидент" in _reply_text(incident_help[0]).lower()
    assert "постмортем" not in _reply_text(incident_help[0]).lower()


# --- confirm_incident branches ---------------------------------------------


@pytest.mark.asyncio
async def test_confirm_incident_already_confirmed_short_circuits(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    first = await service.confirm_incident(
        post.id, confirmed_by_user_id="validator", source="reaction"
    )
    assert first.status == ConfirmationStatus.CONFIRMED

    incident_posts_after_first = len(
        [
            c
            for c in service.mattermost.created_posts
            if c["channel_id"] == "incidents-channel" and c["root_id"] is None
        ]
    )
    comments_after_first = len(service.jira.comments)
    valid_after_first = list(service.jira.valid_updates)

    second = await service.confirm_incident(
        post.id, confirmed_by_user_id="validator", source="reaction"
    )

    assert second.status == ConfirmationStatus.ALREADY_CONFIRMED
    incident_posts_after_second = [
        c
        for c in service.mattermost.created_posts
        if c["channel_id"] == "incidents-channel" and c["root_id"] is None
    ]
    # No second incident post, no duplicate Jira comment / valid_incident write.
    assert len(incident_posts_after_second) == incident_posts_after_first
    assert len(service.jira.comments) == comments_after_first
    assert service.jira.valid_updates == valid_after_first


@pytest.mark.asyncio
async def test_confirm_incident_pending_jira_saves_pending(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    # Seed a keyless alert ticket directly so confirm_incident hits the
    # PENDING_JIRA branch (no Jira issue yet).
    service.repository.create_or_get_alert(
        post,
        message_url=service.mattermost.permalink(post.id),
        channel_name="alerts",
    )
    confirmed_at = backend_now()

    result = await service.confirm_incident(
        post.id,
        confirmed_by_user_id="validator",
        source="reaction",
        confirmed_at=confirmed_at,
    )

    assert result.status == ConfirmationStatus.PENDING_JIRA
    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.confirmation_status == "pending_confirmation"
    assert ticket.pending_confirmation_by_user_id == "validator"
    # The SQLite roundtrip drops tzinfo, so compare the naive wall-clock value.
    assert ticket.pending_confirmation_at is not None
    assert ticket.pending_confirmation_at.replace(tzinfo=None) == confirmed_at.replace(tzinfo=None)
    assert ticket.valid_incident is False
    # No incident post published while still pending.
    assert [
        c
        for c in service.mattermost.created_posts
        if c["channel_id"] == "incidents-channel" and c["root_id"] is None
    ] == []


@pytest.mark.asyncio
async def test_process_pending_work_completes_seeded_pending_confirmation(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    # A real keyed alert ticket (OPS-1) that is not yet confirmed.
    await service.handle_alert_post(post)
    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.jira_issue_key == "OPS-1"
    assert ticket.valid_incident is False

    confirmed_at = backend_now()
    service.repository.mark_pending_confirmation(post.id, "validator", confirmed_at)

    await service.process_pending_work()

    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.confirmation_status == "confirmed"
    assert ticket.valid_incident is True
    assert ticket.incident_post_id is not None
    assert service.jira.valid_updates == [("OPS-1", True)]


# --- apply_incident_end_time branches --------------------------------------


@pytest.mark.asyncio
async def test_apply_incident_end_time_ignored_and_error_branches(service):
    # 1) Unknown post: no incident mapping for this post id -> IGNORED.
    unknown = MattermostPost(
        id="unknownincidentpost00000001",
        channel_id="incidents-channel",
        user_id="someone",
        message="not an incident",
        create_at=1_700_000_000_000,
    )
    ignored_unknown = await service.apply_incident_end_time(
        unknown, ended_at=backend_now(), source="reaction"
    )
    assert ignored_unknown.status == ConfirmationStatus.IGNORED

    # 2) Unconfirmed / no Jira key: a manual incident thread row (incident_post_id
    #    == post.id, valid_incident False, no key) -> IGNORED, set_end_time untouched.
    manual = _manual_post()
    service.mattermost.posts[manual.id] = manual
    service.repository.create_or_get_incident_thread(
        manual,
        message_url=service.mattermost.permalink(manual.id),
        channel_name="incidents",
    )
    ignored_unconfirmed = await service.apply_incident_end_time(
        manual, ended_at=backend_now(), source="reaction"
    )
    assert ignored_unconfirmed.status == ConfirmationStatus.IGNORED
    assert service.jira.end_updates == []

    # 3) Confirmed incident whose Jira write raises -> ERROR + last_error persisted.
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.incident_post_id is not None
    incident_post = service.mattermost.posts[ticket.incident_post_id]

    async def _boom(issue_key, ended_at):
        raise ApiError("end-time write failed", status_code=500, retryable=True)

    service.jira.set_end_time = _boom

    error_result = await service.apply_incident_end_time(
        incident_post, ended_at=backend_now(), source="reaction"
    )
    assert error_result.status == ConfirmationStatus.ERROR
    refreshed = service.repository.get_by_incident_post_id(incident_post.id)
    assert refreshed is not None
    assert refreshed.last_error == "end-time write failed"


# --- handle_manual_incident_post early short-circuit -----------------------


@pytest.mark.asyncio
async def test_handle_manual_incident_post_short_circuits_before_creating_ticket(settings):
    # No interactive controls (no public url), no duty mention, duty help off:
    # nothing to post, so the method returns before creating any ticket row.
    service = _build_service(
        replace(settings, duty_help_enabled=False, mattermost_duty_mention=None)
    )
    post = _manual_post()
    service.mattermost.posts[post.id] = post

    await service.handle_manual_incident_post(post)

    assert service.repository.get_by_incident_post_id(post.id) is None
    assert service.mattermost.created_posts == []
    assert service.jira.created_payloads == []


# --- reaction self-echo + unauthorized notice ------------------------------


@pytest.mark.asyncio
async def test_bot_self_echo_ignored_and_unauthorized_notice(settings):
    service = _build_service(replace(settings, mattermost_authorized_usernames=("alice",)))
    service.mattermost.username_to_id = {"alice": "alice-id"}
    service.mattermost.display_names = {"intruder": "Мария Мир (@maria.mir)"}
    await service.resolve_authorized_users()

    post = make_alert()
    service.mattermost.posts[post.id] = post

    # The bot's own reaction echoes back over the websocket: ignored before any
    # get_post / unauthorized notice fires.
    bot_result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="bot-user", emoji_name="incident", create_at=1)
    )
    assert isinstance(bot_result, ConfirmationResult)
    assert bot_result.status == ConfirmationStatus.IGNORED
    assert service.mattermost.created_posts == []

    # An unauthorized human gets exactly one thread notice listing the allowed users.
    human_result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="intruder", emoji_name="incident", create_at=2)
    )
    assert isinstance(human_result, ConfirmationResult)
    assert human_result.status == ConfirmationStatus.IGNORED
    notices = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    assert len(notices) == 1
    notice = notices[0]
    # The body (allowed users) lands in the boxed attachment; the @mention is the
    # bare message so the ping fires.
    body = notice["props"]["attachments"][0]["text"]
    assert "@alice" in body
    assert "авторизованным" in body
    assert notice["message"] == "@maria.mir"
