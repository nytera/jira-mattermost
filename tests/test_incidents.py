from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from support import (
    FakeLlmClient,
    _build_service,
    _incident_service,
    _issue_reply,
    _manual_post,
    _reply_text,
    make_alert,
)

from mm_jira_bot.actions import (
    DUTY_HELP_ATTACHMENT_COLOR,
)
from mm_jira_bot.domain import (
    MattermostPost,
    ReactionEvent,
    datetime_from_mattermost_ms,
)
from mm_jira_bot.formatting import (
    format_alert_duty_help,
    format_incident_duty_help,
    format_incident_message,
)


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
    # Red info block first, then the forwarded alert attachment(s) (a copy).
    info_block = post_attachments[0]
    assert info_block["color"] == "#EF4444"
    assert post_attachments[1:] == attachments
    assert post_attachments[1] is not attachments[0]
    info_text = info_block["text"]
    assert info_text.startswith("##### 🔴 Инцидент открыт")
    assert "Задача Jira: [OPS-1]" in info_text
    assert "Исходный алерт" in info_text
    # The alert text is in the forwarded block, not duplicated in the info block.
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
    assert len(service.llm.prompts) == 1
    prompt = service.llm.prompts[0]
    assert "API 500 on checkout" in prompt
    assert "Откатили релиз" in prompt
    assert "Иван Иванов (@ivanov.ivan)" in prompt
    assert "Петр Петров (@petrov.petr)" in prompt
    assert "## Извлечённые уроки" in prompt
    assert "### Что было сделано хорошо / В чём повезло" in prompt
    assert "### Что пошло не так / В чём не повезло" in prompt
    assert "## Action Items (на обсуждение)" in prompt
    assert "Action Items — это предложения на обсуждение" in prompt
    assert "до 10 слов и до 80 символов" in prompt
    assert "до 120 символов" in prompt
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
    assert service.jira.generic_comments
    comment_issue_key, comment = service.jira.generic_comments[-1]
    assert comment_issue_key == "OPS-1"
    assert "Постмортем сгенерирован" in comment
    assert "API начал отвечать 500." in comment
    # The comment is converted to Jira wiki markup (Markdown headings → h2.).
    assert "h2. Хронология" in comment
    assert "##Хронология" not in comment
    # The incident thread gets the fact-based summary (own LLM prompt), posted as
    # a "Генерация саммари…" placeholder that is then edited into the final reply.
    placeholders = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == ticket.incident_post_id
        and "Генерация саммари" in _reply_text(created)
    ]
    assert len(placeholders) == 1
    # The placeholder is edited through the 1/3·2/3·3/3 status steps, then into the
    # final summary (last edit).
    edits = [
        u for u in service.mattermost.updated_posts if u["post_id"] == placeholders[0]["post"].id
    ]
    assert any("Шаг 1/3" in _reply_text(u) for u in edits)
    final = edits[-1]
    assert "Саммари треда" in _reply_text(final)
    assert "всё сломалось" in _reply_text(final)
    # The Jira link no longer rides inside the summary; closure is announced in a
    # standalone green box posted as a separate reply.
    assert "Полный постмортем отправлен в Jira" not in _reply_text(final)
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
    assert service.jira.generic_comments
    assert "API начал отвечать 500." in service.jira.generic_comments[-1][1]
    placeholders = [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == root.id and "Генерация саммари" in _reply_text(created)
    ]
    assert len(placeholders) == 1
    edits = [
        u for u in service.mattermost.updated_posts if u["post_id"] == placeholders[0]["post"].id
    ]
    assert any("Шаг 1/3" in _reply_text(u) for u in edits)
    final = edits[-1]
    assert "Саммари треда" in _reply_text(final)
    assert "Полный постмортем отправлен в Jira" not in _reply_text(final)
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

    message = format_incident_message(
        ticket,
        confirmed_by="@validator",
        confirmed_at=datetime(2026, 5, 29, 22, 30, tzinfo=UTC),
    )

    assert "CPU usage is above 95%" in message
    assert "Исходный алерт" in message
    assert "OPS-1" in message
    assert "@validator" in message
    assert "Время подтверждения: 30.05.2026 01:30" in message


@pytest.mark.asyncio
async def test_issue_reply_has_action_buttons_when_public_url_set(settings):
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com/",
            interactive_buttons_enabled=True,
        )
    )
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)

    # One message with two stacked blocks: the blue main block ("Создана задача"
    # notice + validity menu + incident/summary buttons) and a gray feedback block.
    assert reply["message"] == ""
    attachments = reply["props"]["attachments"]
    assert len(attachments) == 2

    # Block 1: the notice, validity menu, and incident/summary buttons, blue.
    controls_attachment = attachments[0]
    assert controls_attachment["color"] == "#3B82F6"
    assert "title" not in controls_attachment
    assert "title_link" not in controls_attachment
    assert controls_attachment["text"] == (
        "**Создана задача: [OPS-1](https://jira.example.com/browse/OPS-1)**"
    )
    controls_actions = controls_attachment["actions"]
    assert [action["id"] for action in controls_actions] == [
        "validity",
        "incident",
        "summary",
    ]
    validity = controls_actions[0]
    assert validity["integration"]["url"] == ("https://bot.example.com/mattermost/actions/alert")
    assert validity["integration"]["context"] == {
        "action": "validity",
        "alert_post_id": post.id,
    }
    assert validity["name"] == "Выбрать валидность ▼"
    assert validity["type"] == "select"
    assert validity["options"] == [
        {"text": "Ложный", "value": "false"},
        {"text": "Ожидаемый", "value": "expected"},
        {"text": "Валидный", "value": "valid"},
    ]
    assert controls_actions[1]["name"] == "🚨 Инцидент"
    assert controls_actions[1]["style"] == "primary"
    assert controls_actions[2]["name"] == "📝 Summary"
    assert controls_actions[2]["style"] == "default"

    # Block 2: feedback, in its own gray block below.
    feedback_attachment = attachments[1]
    assert feedback_attachment["color"] == "#4B5563"
    assert "text" not in feedback_attachment
    feedback_actions = feedback_attachment["actions"]
    assert [action["id"] for action in feedback_actions] == ["feedback"]
    assert feedback_actions[0]["name"] == "💬 Обратная связь по алерту"
    assert feedback_actions[0]["style"] == "default"
    assert feedback_actions[0]["integration"]["context"] == {
        "action": "feedback",
        "alert_post_id": post.id,
    }


@pytest.mark.asyncio
async def test_issue_reply_has_no_buttons_without_public_url(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)
    # Boxed notice (no SERVICE_PUBLIC_URL), but no interactive button/menu controls.
    assert "Создана задача Jira" in _reply_text(reply)
    assert all("actions" not in a for a in reply["props"].get("attachments", []))


@pytest.mark.asyncio
async def test_issue_reply_has_no_buttons_when_interactive_buttons_disabled(settings):
    # Emoji-only mode: SERVICE_PUBLIC_URL is set but the toggle is off, so the
    # issue-created reply degrades to the plain text fallback (no cards).
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=False,
        )
    )
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)
    # Emoji-only mode still boxes the notice, just without interactive controls.
    assert "Создана задача Jira" in _reply_text(reply)
    assert all("actions" not in a for a in reply["props"].get("attachments", []))


@pytest.mark.asyncio
async def test_firing_alert_pings_duty_above_box_with_buttons(settings):
    # Interactive mode: the duty @mention is a bare message above the box so the
    # ping fires; the "Создана задача" notice stays inside the attachment.
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com/",
            interactive_buttons_enabled=True,
            mattermost_duty_mention=":look: @sre-ads-duty",
        )
    )
    post = make_alert()
    service.mattermost.posts[post.id] = post

    await service.handle_alert_post(post)

    reply = _issue_reply(service, post.id)
    assert reply["message"] == ":look: @sre-ads-duty"
    assert reply["props"]["attachments"][0]["text"] == (
        "**Создана задача: [OPS-1](https://jira.example.com/browse/OPS-1)**"
    )


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
async def test_manual_incident_no_card_when_interactive_buttons_disabled(settings):
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=False,
        )
    )
    post = _manual_post()
    service.mattermost.posts[post.id] = post

    await service.handle_manual_incident_post(post)

    replies = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    # Emoji-only mode: only the duty cheat-sheet notice, no interactive create-task card.
    assert len(replies) == 1
    assert "Памятка дежурному" in _reply_text(replies[0])
    assert all("actions" not in a for a in (replies[0]["props"] or {}).get("attachments", []))


@pytest.mark.asyncio
async def test_manual_incident_pings_duty_in_emoji_only_mode(settings):
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=False,
            mattermost_duty_mention=":look: @sre-ads-duty",
        )
    )
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
    "selected_option,expected_label",
    [
        ("false", "Ложный"),
        ("expected", "Ожидаемый"),
        ("valid", "Валидный"),
    ],
)
async def test_action_menu_sets_validity(service, selected_option, expected_label):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.handle_alert_action(
        action="validity",
        alert_post_id=post.id,
        user_id="clicker",
        selected_option=selected_option,
    )

    ticket = service.repository.get_by_post_id(post.id)
    assert service.jira.validity_updates == [("OPS-1", expected_label)]
    assert ticket.validity_label == expected_label
    assert ticket.valid_incident is False
    assert ticket.incident_post_id is None
    assert expected_label in result.message


@pytest.mark.asyncio
async def test_legacy_action_button_still_sets_validity(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.handle_alert_action(
        action="false", alert_post_id=post.id, user_id="clicker"
    )

    ticket = service.repository.get_by_post_id(post.id)
    assert service.jira.validity_updates == [("OPS-1", "Ложный")]
    assert ticket.validity_label == "Ложный"
    assert "Ложный" in result.message


@pytest.mark.asyncio
async def test_incident_button_confirms_incident(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.handle_alert_action(
        action="incident", alert_post_id=post.id, user_id="clicker"
    )

    ticket = service.repository.get_by_post_id(post.id)
    assert ticket.valid_incident is True
    assert ticket.incident_post_id is not None
    incident_posts = [
        created
        for created in service.mattermost.created_posts
        if created["channel_id"] == "incidents-channel" and created["root_id"] is None
    ]
    assert len(incident_posts) == 1
    assert "Инцидент заведён" in result.message


@pytest.mark.asyncio
async def test_incident_button_swaps_to_confirmed(settings):
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=True,
        )
    )
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.handle_alert_action(
        action="incident", alert_post_id=post.id, user_id="closer"
    )

    assert result.update_attachments is not None
    controls = result.update_attachments[0]
    names = [a.get("name") for a in controls["actions"]]
    assert "✅ Подтверждён" in names
    assert "🚨 Инцидент" not in names
    assert "📝 Summary" in names
    # Validity moves to the incident card, so the alert menu is gone after confirm.
    assert not any(a["id"] == "validity" for a in controls["actions"])
    # The alert thread gets the status notice with the incident-channel link.
    status_replies = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == post.id and "Ссылка на инцидент" in _reply_text(c)
    ]
    assert len(status_replies) == 1


@pytest.mark.asyncio
async def test_manual_incident_post_offers_create_button(settings):
    service = _incident_service(settings)
    post = _manual_post()
    service.mattermost.posts[post.id] = post

    await service.handle_manual_incident_post(post)

    replies = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    # The create-task card plus the duty cheat-sheet notice below it.
    assert len(replies) == 2
    card = next(r for r in replies if (r["props"] or {}).get("attachments", [{}])[0].get("actions"))
    assert card["props"]["attachments"][0]["actions"][0]["id"] == "create_task"

    # Idempotent: a redelivered event does not post a second card or cheat-sheet.
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
    service = _incident_service(settings)
    await service.handle_manual_incident_post(post)
    assert service.mattermost.created_posts == []


@pytest.mark.asyncio
async def test_manual_incident_no_controls_without_public_url(settings):
    service = _build_service(settings)
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)
    replies = [c for c in service.mattermost.created_posts if c["root_id"] == post.id]
    # No SERVICE_PUBLIC_URL → no create-task card, only the duty cheat-sheet notice.
    assert len(replies) == 1
    assert "Памятка дежурному" in _reply_text(replies[0])
    assert all("actions" not in a for a in (replies[0]["props"] or {}).get("attachments", []))


@pytest.mark.asyncio
async def test_manual_incident_card_pings_duty(settings):
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=True,
            mattermost_duty_mention=":look: @sre-ads-duty",
        )
    )
    post = _manual_post()
    service.mattermost.posts[post.id] = post

    await service.handle_manual_incident_post(post)

    card = next(c for c in service.mattermost.created_posts if c["root_id"] == post.id)
    # The mention lives in the message text (renders above the card) so the ping fires.
    assert card["message"] == ":look: @sre-ads-duty"
    assert card["props"]["attachments"][0]["actions"][0]["id"] == "create_task"


@pytest.mark.asyncio
async def test_incident_create_task_creates_jira_and_updates_card(settings):
    service = _incident_service(settings)
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)

    result = await service.handle_incident_action(
        action="create_task", incident_post_id=post.id, user_id="opener"
    )

    ticket = service.repository.get_by_incident_post_id(post.id)
    assert ticket is not None
    assert ticket.jira_issue_key == "OPS-1"
    assert result.update_attachments is not None
    card = result.update_attachments[0]
    # The Jira link lives in the main message, so the card carries no task text.
    assert "text" not in card
    ids = [a["id"] for a in card["actions"]]
    assert ids == ["validity", "end_incident", "summary"]


@pytest.mark.asyncio
async def test_manual_incident_false_then_end_preserves_validity(settings):
    """Manual flow: create task → Ложный → Завершение keeps Ложный and runs the PM."""
    service = _incident_service(settings)
    service.llm = FakeLlmClient()
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)
    await service.handle_incident_action(
        action="create_task", incident_post_id=post.id, user_id="opener"
    )

    await service.handle_incident_action(
        action="validity", incident_post_id=post.id, user_id="closer", selected_option="false"
    )
    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"

    result = await service.handle_incident_action(
        action="end_incident", incident_post_id=post.id, user_id="closer"
    )

    assert "завершён" in result.message.lower()
    assert len(service.llm.prompts) == 1
    # Postmortem set end-time but never re-stamped Валидный over the explicit Ложный.
    assert service.jira.valid_updates == []
    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"
    assert [key for key, _ in service.jira.end_updates] == ["OPS-1"]


@pytest.mark.asyncio
async def test_pending_work_ignores_uncreated_manual_card(settings):
    """The pre-created card row stays keyless: the background loop must not
    auto-create a Jira issue for it (that would defeat the button gating)."""
    service = _incident_service(settings)
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)

    await service.process_pending_work()

    ticket = service.repository.get_by_incident_post_id(post.id)
    assert ticket is not None
    assert ticket.jira_issue_key is None
    assert service.jira.created_payloads == []


@pytest.mark.asyncio
async def test_confirmed_alert_incident_gets_controls_card(settings):
    """An alert confirmed as an incident gets the same controls in the incident
    channel, but without "Создать задачу" (the Jira issue already exists)."""
    service = _incident_service(settings)
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )

    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.incident_post_id is not None
    cards = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == ticket.incident_post_id
        and any(a.get("actions") for a in (c["props"] or {}).get("attachments", []))
    ]
    assert len(cards) == 1
    card = cards[0]["props"]["attachments"][0]
    ids = [a["id"] for a in card["actions"]]
    assert ids == ["validity", "end_incident", "summary"]
    # Alert-originated card shows the "Создана задача" header like the alert card.
    assert card["text"] == "**Создана задача: [OPS-1](https://jira.example.com/browse/OPS-1)**"


@pytest.mark.asyncio
async def test_incident_card_validity_on_alert_incident(settings):
    """Validity from the incident card resolves the alert-originated ticket
    (incident_post_id != mattermost_post_id) and sets the Jira field."""
    service = _incident_service(settings)
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.incident_post_id != ticket.mattermost_post_id
    assert ticket.incident_post_id is not None

    result = await service.handle_incident_action(
        action="validity",
        incident_post_id=ticket.incident_post_id,
        user_id="closer",
        selected_option="false",
    )

    assert "Ложный" in result.message
    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"


@pytest.mark.asyncio
async def test_completing_alert_incident_updates_title_to_done(settings):
    service = _incident_service(settings)
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

    def info_text():
        return service.mattermost.posts[incident_post_id].props["attachments"][0]["text"]

    assert "##### 🔴 Инцидент открыт" in info_text()

    await service.handle_incident_action(
        action="end_incident", incident_post_id=incident_post_id, user_id="closer"
    )

    assert "##### 🟢 Инцидент закрыт" in info_text()
    assert "##### 🔴 Инцидент открыт" not in info_text()


@pytest.mark.asyncio
async def test_end_button_swaps_to_completed(settings):
    service = _incident_service(settings)
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

    result = await service.handle_incident_action(
        action="end_incident", incident_post_id=ticket.incident_post_id, user_id="closer"
    )

    assert result.update_attachments is not None
    actions = result.update_attachments[0]["actions"]
    names = [a.get("name") for a in actions]
    assert "✅ Завершено" in names
    assert "🏁 Завершить" not in names
    # Validity menu and summary remain after completion.
    assert any(a["id"] == "validity" for a in actions)
    assert "📝 Саммари" in names


def test_incident_and_end_buttons_require_confirmation():
    from mm_jira_bot.actions import (
        build_alert_controls_attachment,
        build_incident_controls_attachment,
    )

    alert = build_alert_controls_attachment(
        title="OPS-1", title_link="u", alert_post_id="p", callback_url="http://x/cb"
    )
    incident_btn = next(a for a in alert["actions"] if a.get("id") == "incident")
    assert incident_btn["confirm"]["ok_text"] == "Завести"

    inc = build_incident_controls_attachment(incident_post_id="p", callback_url="http://x/cb")
    end_btn = next(a for a in inc["actions"] if a.get("id") == "end_incident")
    assert end_btn["confirm"]["ok_text"] == "Завершить"
    # Summary stays a plain one-click button.
    summary_btn = next(a for a in inc["actions"] if a.get("id") == "summary")
    assert "confirm" not in summary_btn


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
    # Validity reactions here close the incident + postmortem (not the alert's
    # label-only meaning), and the summary emoji is listed too.
    assert ":man_gesturing_no:" in text and ":arrows_counterclockwise:" in text
    assert ":memo:" in text
    assert "постмортем" in text.lower()
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
    # validity reactions here mean "close + postmortem", unlike the alert thread.
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
    assert "постмортем" in _reply_text(incident_help[0]).lower()
