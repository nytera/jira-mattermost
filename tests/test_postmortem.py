from __future__ import annotations

from dataclasses import replace

import pytest
from support import (
    FakeLlmClient,
    _build_service,
    _confirm_and_close_incident,
    _confirmed_incident,
    _manual_post,
    _reply_text,
    make_alert,
)

from mm_jira_bot.domain import (
    MattermostPost,
    ReactionEvent,
    datetime_from_mattermost_ms,
)
from mm_jira_bot.postmortem import (
    DEFAULT_POSTMORTEM_PROMPT,
    DEFAULT_SUMMARY_PROMPT,
    build_incident_report_prompt,
    build_postmortem_comment,
    extract_postmortem_summary,
    format_incident_closed_notice,
    markdown_to_jira_wiki,
)
from mm_jira_bot.retry import ApiError


def test_postmortem_summary_limits_llm_title_words_and_chars():
    report = (
        "[INC] 15.11.2023 - "
        "Очень длинное название инцидента про падение checkout api после релиза "
        "новой корзины с большим количеством технических деталей"
    )

    summary = extract_postmortem_summary(report, fallback="fallback")
    title = summary.split(" - ", 1)[1]

    assert len(summary) <= 120
    assert len(title) <= 80
    assert len(title.split()) <= 10
    assert summary == (
        "[INC] 15.11.2023 - Очень длинное название инцидента про падение checkout api после релиза"
    )


def test_format_incident_closed_notice_links_task_by_title():
    notice = format_incident_closed_notice(
        jira_issue_title="[INC] 15.11.2023 - Ошибки API",
        jira_issue_url="https://jira.example.com/browse/OPS-1",
    )
    # Brackets in the title are escaped so the leading "[INC]" does not nest inside
    # the markdown link text and break rendering.
    assert notice == (
        "🟢 **Инцидент закрыт**\n"
        "ПМ: [\\[INC\\] 15.11.2023 - Ошибки API](https://jira.example.com/browse/OPS-1)"
    )


def test_format_incident_closed_notice_degrades_without_url():
    notice = format_incident_closed_notice(jira_issue_title="OPS-1", jira_issue_url=None)
    assert notice == "🟢 **Инцидент закрыт**\nПМ: OPS-1"


@pytest.mark.asyncio
async def test_postmortem_checkmark_preserves_false_validity(service):
    """A confirmed incident marked Ложный keeps that validity after the PM checkmark.

    Validity (the Jira field value) and confirmation (valid_incident) are
    independent axes: the checkmark sets end-time + generates the postmortem but
    must not re-stamp Валидный over an explicit Ложный.
    """
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    # Confirm as incident (posts to incident channel, stamps Валидный once).
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    # Turns out false: set validity to Ложный via the lightweight reaction.
    await service.handle_reaction(
        ReactionEvent(
            post_id=post.id, user_id="validator", emoji_name="man_gesturing_no", create_at=2
        )
    )
    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"

    ticket = service.repository.get_by_post_id(post.id)
    result = await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="closer",
            emoji_name="white_check_mark",
            create_at=1_700_000_200_000,
        )
    )

    assert result.status == "incident_ended"
    # Postmortem ran (end-time set, report commented) ...
    assert service.jira.end_updates == [("OPS-1", datetime_from_mattermost_ms(1_700_000_200_000))]
    assert len(service.llm.prompts) == 1
    # ... but validity was NOT re-stamped Валидный: set_valid_incident stays the
    # single confirm-time call, and the field value remains Ложный.
    assert service.jira.valid_updates == [("OPS-1", True)]
    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"


def _manual_incident_ticket(service, *, issue_key="OPS-9", validity_label=None):
    root = MattermostPost(
        id="incidentroot00000000000001",
        channel_id="incidents-channel",
        user_id="human-user",
        message="Лежим в проде, 500-ки на /pay",
        create_at=1_700_000_000_000,
        channel_name="incidents",
    )
    service.repository.create_or_get_incident_thread(
        root, message_url=service.mattermost.permalink(root.id), channel_name="incidents"
    )
    service.repository.attach_jira_issue(
        root.id, issue_key, f"https://jira.example.com/browse/{issue_key}"
    )
    if validity_label is not None:
        service.repository.set_validity_label(root.id, validity_label)
    return service.repository.get_by_post_id(root.id)


@pytest.mark.asyncio
async def test_postmortem_preserves_explicit_false_validity_on_manual_ticket(service):
    """Manual incident (valid_incident stays False) marked Ложный keeps it after end/PM."""
    ticket = _manual_incident_ticket(service, validity_label="Ложный")
    assert ticket.valid_incident is False

    await service._ensure_postmortem_jira_issue(
        ticket,
        summary="summary",
        description="description",
        ended_at=datetime_from_mattermost_ms(1_700_000_300_000),
        reacted_by_user_id="closer",
    )

    # No Валидный stamp; end-time still set.
    assert service.jira.valid_updates == []
    assert service.jira.end_updates == [("OPS-9", datetime_from_mattermost_ms(1_700_000_300_000))]


@pytest.mark.asyncio
async def test_postmortem_defaults_to_valid_when_no_validity_chosen(service):
    """Without an explicit validity, the end/PM step still defaults to Валидный."""
    ticket = _manual_incident_ticket(service, validity_label=None)

    await service._ensure_postmortem_jira_issue(
        ticket,
        summary="summary",
        description="description",
        ended_at=datetime_from_mattermost_ms(1_700_000_300_000),
        reacted_by_user_id="closer",
    )

    assert service.jira.valid_updates == [("OPS-9", True)]


# --- Postmortem comment Jira-wiki conversion --------------------------------


def test_markdown_to_jira_wiki_headings_bullets_bold_links():
    assert markdown_to_jira_wiki("## Сводка") == "h2. Сводка"
    assert markdown_to_jira_wiki("##Сводка") == "h2. Сводка"
    assert markdown_to_jira_wiki("### Уроки") == "h3. Уроки"
    assert markdown_to_jira_wiki("- пункт") == "* пункт"
    assert markdown_to_jira_wiki(" - пункт") == "* пункт"
    assert markdown_to_jira_wiki("**жирный**") == "*жирный*"
    assert markdown_to_jira_wiki("[OPS-1](https://j/OPS-1)") == "[OPS-1|https://j/OPS-1]"


def test_markdown_to_jira_wiki_converts_mentions_but_not_emails():
    assert markdown_to_jira_wiki("12:14 — @aminov.pavel3 откат") == "12:14 — [~aminov.pavel3] откат"
    assert markdown_to_jira_wiki("@ivanov и @petrov") == "[~ivanov] и [~petrov]"
    # A trailing sentence period is not swallowed into a dotted username.
    assert markdown_to_jira_wiki("откатил @aminov.pavel3.") == "откатил [~aminov.pavel3]."
    # Emails must stay intact (the char before @ is a word char).
    assert markdown_to_jira_wiki("write to user@host.ru") == "write to user@host.ru"


def test_markdown_to_jira_wiki_is_idempotent_on_wiki():
    already = "h2. Сводка\n* пункт\n*жирный*\n[OPS-1|https://j/OPS-1]\n[~ivanov]"
    assert markdown_to_jira_wiki(already) == already
    once = markdown_to_jira_wiki("## X\n- y\n**z**\n@ivanov")
    assert markdown_to_jira_wiki(once) == once


def test_postmortem_comment_is_jira_wiki_without_disturbing_summary():
    report = "[INC] 01.01.2026 - Сбой\n## Сводка\nТекст\n- пункт\n**жирный**"
    comment = build_postmortem_comment(
        report=report, incident_thread_url="https://t/1", postmortem_author="Автор"
    )
    assert "h2. Сводка" in comment
    assert "## Сводка" not in comment
    assert "* пункт" in comment
    assert "*жирный*" in comment and "**жирный**" not in comment
    # Summary extraction reads the untouched raw report.
    assert extract_postmortem_summary(report, fallback="f").startswith("[INC] 01.01.2026 - Сбой")


def test_incident_report_prompt_fills_placeholders_without_leftovers():
    prompt = build_incident_report_prompt(
        thread_url="https://t/1",
        participants=["Иван Иванов", "Пётр Петров"],
        postmortem_author="Сидор Сидоров",
        transcript="тело треда",
        max_chars=24000,
    )
    # Built from the unified default template when no override is given.
    assert prompt.startswith("Составь инцидентный отчёт по треду Mattermost")
    assert prompt.endswith("Тред:\nтело треда\n")
    assert "Тред инцидента: https://t/1" in prompt
    assert "Участники: Иван Иванов, Пётр Петров" in prompt
    assert "Автор отчёта: Сидор Сидоров" in prompt
    for token in ("{thread_url}", "{participants}", "{postmortem_author}", "{transcript}"):
        assert token not in prompt


def test_incident_report_prompt_empty_participants_placeholder():
    prompt = build_incident_report_prompt(
        thread_url="https://t/1",
        participants=[],
        postmortem_author="Автор",
        transcript="тело",
        max_chars=24000,
    )
    assert "Участники: не указано" in prompt


def test_incident_report_prompt_overridable_and_does_not_rescan_transcript():
    # A custom template wins, and brace-looking tokens inside the thread text are
    # never re-substituted (transcript is filled last).
    transcript = "юзер написал {participants} и {transcript}"
    rendered = build_incident_report_prompt(
        thread_url="https://t/1",
        participants=["Иван Иванов"],
        postmortem_author="Автор",
        transcript=transcript,
        max_chars=24000,
        template="X {thread_url} | {participants} | {postmortem_author} | {transcript}",
    )
    assert rendered == (
        "X https://t/1 | Иван Иванов | Автор | юзер написал {participants} и {transcript}"
    )


def test_incident_report_prompt_keeps_legacy_thread_url_placeholder():
    # Pre-existing LLM_POSTMORTEM_PROMPT files used {incident_thread_url}; the
    # builder still substitutes that alias so they don't emit a literal token.
    rendered = build_incident_report_prompt(
        thread_url="https://t/1",
        participants=["Иван Иванов"],
        postmortem_author="Автор",
        transcript="тело",
        max_chars=24000,
        template="ПМ {incident_thread_url} :: {transcript}",
    )
    assert rendered == "ПМ https://t/1 :: тело"


def test_summary_and_postmortem_share_one_default_template():
    assert DEFAULT_SUMMARY_PROMPT is DEFAULT_POSTMORTEM_PROMPT
    for token in ("{thread_url}", "{participants}", "{postmortem_author}", "{transcript}"):
        assert token in DEFAULT_POSTMORTEM_PROMPT


@pytest.mark.asyncio
async def test_postmortem_sets_time_to_fix_minutes(settings):
    service = _build_service(replace(settings, jira_time_to_fix_field="customfield_99999"))
    service.llm = FakeLlmClient()
    # Alert at 1_700_000_000_000; closed 300_000 ms (5 min) later.
    await _confirm_and_close_incident(service, closed_at_ms=1_700_000_300_000)
    assert service.jira.time_to_fix_updates == [("OPS-1", 5)]


@pytest.mark.asyncio
async def test_time_to_fix_skipped_when_end_before_start(settings):
    service = _build_service(replace(settings, jira_time_to_fix_field="customfield_99999"))
    service.llm = FakeLlmClient()
    # Checkmark timestamped before the alert was created → non-positive duration.
    await _confirm_and_close_incident(service, closed_at_ms=1_699_999_000_000)
    assert service.jira.time_to_fix_updates == []


@pytest.mark.asyncio
async def test_time_to_fix_set_on_alert_validity_reaction(settings):
    # Ложный/Ожидаемый на алерт (лёгкий путь) тоже пишет длительность, не только End.
    service = _build_service(replace(settings, jira_time_to_fix_field="customfield_99999"))
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    # Alert at 1_700_000_000_000; validity reaction 300_000 ms (5 min) later.
    result = await service.handle_reaction(
        ReactionEvent(
            post_id=post.id,
            user_id="validator",
            emoji_name="man_gesturing_no",
            create_at=1_700_000_300_000,
        )
    )
    assert result.status == "validity_set"
    assert service.jira.validity_updates == [("OPS-1", "Ложный")]
    assert service.jira.time_to_fix_updates == [("OPS-1", 5)]


@pytest.mark.asyncio
async def test_time_to_fix_not_set_when_field_unconfigured(service):
    service.llm = FakeLlmClient()
    await _confirm_and_close_incident(service, closed_at_ms=1_700_000_300_000)
    assert service.jira.time_to_fix_updates == []


@pytest.mark.asyncio
async def test_time_to_fix_on_manual_checkmark_new_issue(settings):
    # Manual checkmark with no prior create_task → the new-issue postmortem branch.
    service = _build_service(replace(settings, jira_time_to_fix_field="customfield_99999"))
    service.llm = FakeLlmClient()
    root = MattermostPost(
        id="manualincidentroot000000099",
        channel_id="incidents-channel",
        user_id="author",
        message="Инцидент по росту 500.",
        create_at=1_700_000_000_000,
        channel_name="incidents",
    )
    service.mattermost.posts[root.id] = root
    await service.handle_reaction(
        ReactionEvent(
            post_id=root.id,
            user_id="closer",
            emoji_name="white_check_mark",
            create_at=1_700_000_300_000,
        )
    )
    assert service.jira.time_to_fix_updates == [("OPS-1", 5)]


@pytest.mark.asyncio
async def test_time_to_fix_on_manual_incident_close(settings):
    # create_task → end_incident → the existing-issue postmortem branch.
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=True,
            jira_time_to_fix_field="customfield_99999",
        )
    )
    service.llm = FakeLlmClient()
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)
    await service.handle_incident_action(
        action="create_task", incident_post_id=post.id, user_id="opener"
    )
    await service.handle_incident_action(
        action="end_incident", incident_post_id=post.id, user_id="closer"
    )
    # Exactly one write, positive minutes (no double-write across set_end_time sites).
    assert len(service.jira.time_to_fix_updates) == 1
    assert service.jira.time_to_fix_updates[0][0] == "OPS-1"
    assert service.jira.time_to_fix_updates[0][1] > 0


@pytest.mark.asyncio
async def test_time_to_fix_failure_does_not_block_closure(settings):
    service = _build_service(replace(settings, jira_time_to_fix_field="customfield_99999"))
    service.llm = FakeLlmClient()

    async def _boom(issue_key, minutes):
        raise ApiError("nope", retryable=False)

    service.jira.set_time_to_fix = _boom
    result = await _confirm_and_close_incident(service, closed_at_ms=1_700_000_300_000)
    assert result.status == "incident_ended"
    assert ("OPS-1", datetime_from_mattermost_ms(1_700_000_300_000)) in service.jira.end_updates


@pytest.mark.asyncio
async def test_false_reaction_in_incident_closes_with_postmortem(service):
    service.llm = FakeLlmClient()
    ticket = await _confirmed_incident(service)

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="closer",
            emoji_name="man_gesturing_no",
            create_at=1_700_000_200_000,
        )
    )

    assert result.status == "incident_ended"
    # Validity stamped Ложный, postmortem generated exactly once, end-time set.
    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"
    assert len(service.llm.prompts) == 1
    assert [key for key, _ in service.jira.generic_comments] == ["OPS-1"]
    assert [key for key, _ in service.jira.end_updates] == ["OPS-1"]
    assert service.repository.get_by_post_id(ticket.mattermost_post_id).postmortem_comment_added


@pytest.mark.asyncio
async def test_false_reaction_in_incident_sets_validity_without_llm(service):
    # No LLM (postmortem disabled) must still write the chosen validity to Jira.
    service.llm = None
    ticket = await _confirmed_incident(service)

    await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="closer",
            emoji_name="man_gesturing_no",
            create_at=1_700_000_200_000,
        )
    )

    assert service.jira.validity_by_issue["OPS-1"] == "Ложный"
    # End-time set, but no postmortem comment (no LLM).
    assert [key for key, _ in service.jira.end_updates] == ["OPS-1"]
    assert service.jira.generic_comments == []


@pytest.mark.asyncio
async def test_validity_reaction_on_finalized_incident_only_flips_validity(service):
    service.llm = FakeLlmClient()
    ticket = await _confirmed_incident(service)
    # Close it once via checkmark (Валидный + postmortem).
    await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="closer",
            emoji_name="white_check_mark",
            create_at=1_700_000_200_000,
        )
    )
    assert len(service.jira.generic_comments) == 1

    # A later validity emoji just flips the field; it must not regenerate the PM.
    await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="auditor",
            emoji_name="arrows_counterclockwise",
            create_at=1_700_000_300_000,
        )
    )

    assert service.jira.validity_by_issue["OPS-1"] == "Ожидаемый"
    assert len(service.jira.generic_comments) == 1
    assert len(service.llm.prompts) == 1
    # The templated "validity changed" notice is posted in the incident thread.
    notices = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == ticket.incident_post_id and "Валидность обновлена" in _reply_text(c)
    ]
    assert len(notices) == 1
    assert "Ожидаемый" in _reply_text(notices[0])


@pytest.mark.asyncio
async def test_repeated_checkmark_does_not_duplicate_postmortem(service):
    service.llm = FakeLlmClient()
    ticket = await _confirmed_incident(service)
    reaction = ReactionEvent(
        post_id=ticket.incident_post_id,
        user_id="closer",
        emoji_name="white_check_mark",
        create_at=1_700_000_200_000,
    )

    await service.handle_reaction(reaction)
    await service.handle_reaction(reaction)

    # The PM comment is additive — a second checkmark must not duplicate it.
    assert len(service.jira.generic_comments) == 1
    assert len(service.llm.prompts) == 1
