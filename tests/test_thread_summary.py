from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from support import (
    FakeLlmClient,
    _confirmed_incident,
    _FakeStreamResponse,
    _reply_text,
    make_alert,
)

from mm_jira_bot.colors import INCIDENT_DONE_COLOR, NOTICE_ATTACHMENT_COLOR, OPS_ALERT_COLOR
from mm_jira_bot.domain import (
    ReactionEvent,
)
from mm_jira_bot.llm import PostmortemLlmClient
from mm_jira_bot.retry import ApiError
from mm_jira_bot.summary import (
    format_thread_summary_reply,
    format_thread_summary_streaming,
    neutralize_mentions,
)


def test_format_thread_summary_streaming_marks_in_progress():
    rendered = format_thread_summary_streaming("  частичный текст  ")
    assert rendered.startswith("📝 **Саммари треда** _(генерируется…)_")
    assert "частичный текст" in rendered
    assert rendered.endswith("частичный текст")  # surrounding whitespace trimmed


@pytest.mark.asyncio
async def test_summary_stream_callback_throttles_edits(service, monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr("mm_jira_bot.service._thread_summary.perf_counter", lambda: clock["t"])
    edits: list[str] = []

    async def _record(reply_id, post_id, *, message, base_props, event):
        edits.append(message)

    monkeypatch.setattr(service, "_edit_summary_reply", _record)
    on_progress = service._make_summary_stream_callback(
        reply_id="reply1", post_id="root1", base_props={}, event="evt"
    )

    # Below both thresholds (≈10 chars, 0.5s after the seed) → no edit yet.
    clock["t"] = 100.5
    await on_progress("a" * 10)
    assert edits == []
    # Crossing the char threshold (≥80 new chars) forces an edit.
    clock["t"] = 100.6
    await on_progress("a" * 100)
    assert len(edits) == 1
    assert "генерируется" in edits[0]
    # Crossing the time threshold (≥1.5s) forces the next edit.
    clock["t"] = 102.5
    await on_progress("a" * 120)
    assert len(edits) == 2
    # A shrinking buffer (retry restart) forces a re-render regardless of thresholds.
    clock["t"] = 102.6
    await on_progress("x" * 5)
    assert len(edits) == 3


@pytest.mark.asyncio
async def test_collect_stream_emits_cumulative_progress(settings):
    # End-to-end wiring of the SSE collector: each delta fires on_progress with the
    # CUMULATIVE text, and the joined result is returned.
    client = PostmortemLlmClient(settings, http_client=httpx.AsyncClient())
    lines = [
        'data: {"choices":[{"delta":{"content":"Привет"}}]}',
        "",  # non-data line is skipped
        'data: {"choices":[{"delta":{"content":", "}}]}',
        'data: {"choices":[{"delta":{"content":"мир"}}]}',
        "data: [DONE]",
    ]
    seen: list[str] = []

    async def record(text: str) -> None:
        seen.append(text)

    try:
        result = await client._collect_stream(_FakeStreamResponse(lines), on_progress=record)
    finally:
        await client.aclose()

    assert result == "Привет, мир"
    assert seen == ["Привет", "Привет, ", "Привет, мир"]  # cumulative, per delta


@pytest.mark.asyncio
async def test_llm_client_uses_openai_compatible_chat_completions(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.read()),
            }
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "[INC] 15.11.2023 - Отчет"}}]},
        )

    client = PostmortemLlmClient(
        replace(
            settings,
            llm_api_token="llm-token",
            llm_base_url="https://corellm.wb.ru/deepseek/v1",
        ),
        http_client=httpx.AsyncClient(
            base_url="https://corellm.wb.ru/deepseek/v1/",
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        report = await client.resolve_incident_closeout("thread transcript")
    finally:
        await client.aclose()

    assert report == "[INC] 15.11.2023 - Отчет"
    assert requests[0]["method"] == "POST"
    assert requests[0]["path"] == "/deepseek/v1/chat/completions"
    assert requests[0]["body"]["model"] == "deepseek-chat"
    assert requests[0]["body"]["messages"][1]["content"] == "thread transcript"
    assert "temperature" not in requests[0]["body"]


@pytest.mark.asyncio
async def test_llm_client_assembles_streamed_sse_deltas(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append({"body": json.loads(request.read())})
        sse = (
            'data: {"choices":[{"delta":{"content":"[INC] "}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"15.11"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":" - Отчет"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse.encode("utf-8"),
        )

    client = PostmortemLlmClient(
        replace(settings, llm_api_token="llm-token"),
        http_client=httpx.AsyncClient(
            base_url="https://corellm.wb.ru/deepseek/v1/",
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        report = await client.resolve_incident_closeout("thread transcript")
    finally:
        await client.aclose()

    assert report == "[INC] 15.11 - Отчет"
    assert requests[0]["body"]["stream"] is True


@pytest.mark.asyncio
async def test_llm_client_wraps_transport_errors_as_api_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = PostmortemLlmClient(
        replace(settings, llm_api_token="llm-token", api_retry_attempts=1),
        http_client=httpx.AsyncClient(
            base_url="https://corellm.wb.ru/deepseek/v1/",
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        with pytest.raises(ApiError) as excinfo:
            await client.resolve_incident_closeout("thread transcript")
    finally:
        await client.aclose()

    assert excinfo.value.retryable is True
    assert "ConnectError" in str(excinfo.value)


@pytest.mark.asyncio
async def test_llm_preflight_uses_small_openai_compatible_request(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.read()),
            }
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    client = PostmortemLlmClient(
        replace(
            settings,
            llm_api_token="llm-token",
            llm_base_url="https://corellm.wb.ru/deepseek/v1",
            llm_model="DeepSeek-V3.1 Terminus",
            llm_max_tokens=8000,
        ),
        http_client=httpx.AsyncClient(
            base_url="https://corellm.wb.ru/deepseek/v1/",
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        result = await client.preflight_check()
    finally:
        await client.aclose()

    assert result["llm_model"] == "DeepSeek-V3.1 Terminus"
    assert result["llm_response_length"] == 2
    assert requests[0]["method"] == "POST"
    assert requests[0]["path"] == "/deepseek/v1/chat/completions"
    assert requests[0]["body"]["model"] == "DeepSeek-V3.1 Terminus"
    assert requests[0]["body"]["messages"][0]["role"] == "user"
    assert requests[0]["body"]["max_tokens"] == 16
    assert "temperature" not in requests[0]["body"]


@pytest.mark.asyncio
async def test_summary_reaction_without_llm_is_noop(service):
    # The conftest ``service`` fixture has ``llm=None``: a memo reaction must noop
    # with the "LLM не настроен" notice and post no summary reply.
    post = make_alert()
    service.mattermost.posts[post.id] = post

    result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="reader", emoji_name="memo", create_at=2)
    )

    summary_replies = [
        created
        for created in service.mattermost.created_posts
        if "Саммари треда" in _reply_text(created)
    ]
    assert summary_replies == []
    assert "LLM не настроен" in result.message


def test_thread_summary_strips_mentions_so_it_never_pings():
    assert neutralize_mentions("12:14 — @aminov.pavel3 откат") == "12:14 — aminov.pavel3 откат"
    assert "@" not in format_thread_summary_reply("сделал @ivanov")
    assert "@" not in format_thread_summary_streaming("сделал @ivanov")
    # Emails are left untouched.
    assert neutralize_mentions("user@host.ru") == "user@host.ru"


# --- Summary emoji ----------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_reaction_posts_thread_summary_and_jira_comment(service):
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="reader", emoji_name="memo", create_at=2)
    )

    assert "опубликовано в треде и добавлено комментарием в Jira" in result.message
    assert len(service.llm.summary_prompts) == 1
    # The thread has a Jira issue, so the summary is also posted as a comment.
    assert len(service.jira.generic_comments) == 1
    issue_key, comment = service.jira.generic_comments[0]
    assert issue_key == "OPS-1"
    assert "Саммари треда сгенерировано" in comment
    assert "Суть: всё сломалось." in comment


@pytest.mark.asyncio
async def test_summary_reaction_works_in_incident_thread(service):
    service.llm = FakeLlmClient()
    ticket = await _confirmed_incident(service)

    result = await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id, user_id="reader", emoji_name="memo", create_at=3
        )
    )

    assert "опубликовано в треде и добавлено комментарием в Jira" in result.message
    assert len(service.llm.summary_prompts) == 1
    # The incident thread maps to a Jira issue, so the summary lands as a comment.
    assert len(service.jira.generic_comments) == 1
    assert service.jira.generic_comments[0][0] == "OPS-1"


@pytest.mark.asyncio
async def test_summary_mention_reaches_jira_as_clickable(service):
    # The Jira comment gets the RAW summary (mentions preserved → [~user]); the
    # thread reply path neutralizes separately. Proves raw text flows to Jira.
    service.llm = FakeLlmClient()
    service.llm.summary = "Починил @ivanov.ivan"
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="reader", emoji_name="memo", create_at=2)
    )

    _issue_key, comment = service.jira.generic_comments[-1]
    assert "[~ivanov.ivan]" in comment
    assert "@ivanov.ivan" not in comment


@pytest.mark.asyncio
async def test_summary_reaction_without_jira_issue_warns(service):
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post  # no handle_alert_post → no ticket/issue

    result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="reader", emoji_name="memo", create_at=2)
    )

    assert "В Jira не отправлено: задача не найдена" in result.message
    assert service.jira.generic_comments == []


@pytest.mark.asyncio
async def test_summary_reaction_jira_failure_keeps_thread(service):
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    async def _boom(issue_key, body):
        raise ApiError("jira down", retryable=True)

    service.jira.add_comment = _boom

    result = await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="reader", emoji_name="memo", create_at=2)
    )

    # The thread reply still succeeded; only the Jira comment failed.
    assert "отправка в Jira не удалась" in result.message
    block = _summary_jira_block(service, post.id)
    assert block["color"] == OPS_ALERT_COLOR
    assert "Не удалось отправить" in block["text"]


def _summary_jira_block(service, root_id: str) -> dict:
    """The Jira-outcome attachment appended as the 2nd block under the summary reply."""
    replies = [
        c
        for c in service.mattermost.created_posts
        if c["root_id"] == root_id and (c["props"] or {}).get("summary_requested_by_user_id")
    ]
    assert replies, "no summary reply found"
    reply = service.mattermost.posts[replies[0]["post"].id]
    attachments = (reply.props or {}).get("attachments")
    assert isinstance(attachments, list) and len(attachments) == 2, attachments
    return attachments[1]


@pytest.mark.asyncio
async def test_summary_reply_appends_jira_success_block(service):
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="reader", emoji_name="memo", create_at=2)
    )

    block = _summary_jira_block(service, post.id)
    assert block["color"] == INCIDENT_DONE_COLOR
    assert "добавлено комментарием в Jira" in block["text"]
    assert "OPS-1" in block["text"]


@pytest.mark.asyncio
async def test_summary_reply_appends_not_found_block(service):
    service.llm = FakeLlmClient()
    post = make_alert()
    service.mattermost.posts[post.id] = post  # no handle_alert_post → no ticket/issue

    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="reader", emoji_name="memo", create_at=2)
    )

    block = _summary_jira_block(service, post.id)
    assert block["color"] == NOTICE_ATTACHMENT_COLOR
    assert "задача не найдена" in block["text"]
