from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from support import (
    POST_ID,
    FakeLlmClient,
    _build_service,
    _manual_post,
    make_alert,
)

import mm_jira_bot.jira as jira_module
import mm_jira_bot.jira_payload as jira_payload_module
from mm_jira_bot.domain import (
    ConfirmationStatus,
    MattermostPost,
    ReactionEvent,
)
from mm_jira_bot.jira_payload import build_jira_issue_payload
from mm_jira_bot.retry import ApiError


def test_builds_jira_payload(settings):
    post = replace(make_alert(), message="CPU usage is above 95%\nsecond line")
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    fields = payload["fields"]
    assert fields["project"] == {"key": "OPS"}
    assert fields["issuetype"] == {"name": "Incident"}
    assert "customfield_12345" not in fields
    assert fields["customfield_23456"] == {"value": "Crit alert"}
    assert fields["customfield_34567"] == {"value": "Да"}
    assert fields["summary"] == "[INC] 15.11.2023 - CPU usage is above 95%"
    description = fields["description"]
    assert isinstance(description, str)
    assert "h3. 🔔 Невалидный алерт из Band" in description
    assert "|Автор|" not in description
    assert "{quote}" not in description
    assert "CPU usage is above 95%" not in description
    assert "|Канал|alerts|" in description
    assert "|Время сообщения|15.11.2023 01:13|" in description
    assert (
        "|Исходное сообщение|[Открыть в Band|"
        "https://mattermost.example.com/_redirect/pl/post]|" in description
    )
    assert f"{{{{{POST_ID}}}}}" in description


def test_builds_manual_incident_payload_without_alert_only_fields(settings):
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        make_alert(channel_id="incidents-channel"),
        message_url="https://mattermost.example.com/_redirect/pl/incident",
        channel_name="incidents",
        summary="[INC] 15.11.2023 - Ручной инцидент",
        description="PM template",
        labels=["mattermost-incident", "postmortem"],
        include_alert_fields=False,
    )

    fields = payload["fields"]
    assert fields["summary"] == "[INC] 15.11.2023 - Ручной инцидент"
    assert fields["description"] == "PM template"
    assert fields["labels"] == ["mattermost-incident", "postmortem"]
    assert "customfield_23456" not in fields
    assert "customfield_34567" not in fields


def test_summary_uses_first_non_empty_line_without_leading_emoji(settings):
    message = "\n🔴 Доля рекламных кликов :: Платформа :: Ниже на 10% :: crit\n@sre-ads-duty\n"
    post = replace(make_alert(), message=message)
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == (
        "[INC] 15.11.2023 - Доля рекламных кликов :: Платформа :: Ниже на 10% :: crit"
    )


def test_summary_uses_alert_title_after_emoji_only_line(settings):
    message = "🔴\nДеньги | Минус-слова vs Общее | выше на 70% [Crit]\n@sre-ads-duty\n"
    post = replace(make_alert(), message=message)
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == (
        "[INC] 15.11.2023 - Деньги | Минус-слова vs Общее | выше на 70% [Crit]"
    )


def test_summary_removes_mattermost_emoji_shortcode(settings):
    post = replace(
        make_alert(),
        message=":rotating_light: Деньги | Минус-слова vs Общее | выше на 70% [Crit]",
    )
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == (
        "[INC] 15.11.2023 - Деньги | Минус-слова vs Общее | выше на 70% [Crit]"
    )


def test_summary_uses_grafana_alert_markdown_link_title(settings):
    message = (
        "🔴 [Деньги | Минус-слова vs Общее | выше на 70% [Crit]]"
        "(http://grafana.wb.ru/alerting/grafana/abc123/view)\n"
        "@sre-ads-duty\n"
    )
    post = replace(make_alert(), message=message)
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == (
        "[INC] 15.11.2023 - Деньги | Минус-слова vs Общее | выше на 70% [Crit]"
    )


def test_summary_uses_grafana_alert_angle_link_title(settings):
    message = (
        "🔴 <http://grafana.wb.ru/alerting/grafana/abc123/view|"
        "Доля рекламных кликов :: Платформа :: Ниже на 10% :: crit>\n"
        "@sre-ads-duty\n"
    )
    post = replace(make_alert(), message=message)
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == (
        "[INC] 15.11.2023 - Доля рекламных кликов :: Платформа :: Ниже на 10% :: crit"
    )


def test_summary_uses_grafana_attachment_title_when_message_is_empty(settings):
    post = MattermostPost.from_api(
        {
            "id": POST_ID,
            "channel_id": "alerts-channel",
            "user_id": "grafana-bot",
            "message": "",
            "create_at": 1700000000000,
            "props": {
                "attachments": [
                    {
                        "title": "ads_stat-consumer_antifraud_remove_messages 20% [Crit]",
                        "title_link": ("http://grafana.wb.ru/alerting/grafana/abc123/view"),
                        "text": (
                            "Message: Кол-во сообщений на удаление в датабас "
                            "антифрода снизилось на 20%"
                        ),
                    }
                ]
            },
        }
    )
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == (
        "[INC] 15.11.2023 - ads_stat-consumer_antifraud_remove_messages 20% [Crit]"
    )
    assert "Message: Кол-во сообщений" not in payload["fields"]["description"]


def test_builds_jira_payload_with_start_field(settings):
    post = make_alert()
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
        start_field_id="customfield_45678",
    )

    # Jira date-time picker wants ISO 8601 with a [+-]hhmm offset and
    # mandatory fractional seconds, derived from the alert arrival time.
    assert payload["fields"]["customfield_45678"] == "2023-11-15T01:13:20.000+0300"


def test_builds_jira_payload_without_start_field_by_default(settings):
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        make_alert(),
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert "customfield_45678" not in payload["fields"]


def test_builds_jira_payload_with_current_date_when_post_date_missing(settings, monkeypatch):
    monkeypatch.setattr(
        jira_payload_module,
        "backend_now",
        lambda: datetime(2026, 5, 29, 22, 30, tzinfo=UTC),
    )
    post = replace(make_alert(), create_at=0)
    payload = build_jira_issue_payload(
        settings,
        "customfield_12345",
        "customfield_23456",
        "customfield_34567",
        post,
        message_url="https://mattermost.example.com/_redirect/pl/post",
        channel_name="alerts",
    )

    assert payload["fields"]["summary"] == "[INC] 30.05.2026 - CPU usage is above 95%"
    assert "|Время сообщения|—|" in payload["fields"]["description"]


@pytest.mark.asyncio
async def test_jira_client_makes_no_calls_in_read_only_mode(settings):
    """READ_ONLY_MODE=true must not hit Jira for issue-key operations, so a
    stub key never aborts the confirm/validity/end flows."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected Jira call: {request.method} {request.url}")

    client = jira_module.JiraClient(
        replace(settings, read_only_mode=True),
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )

    assert await client.get_valid_incident("OPS-1") is None
    await client.set_valid_incident("OPS-1", True)
    await client.set_validity("OPS-1", "Ложный")
    await client.set_end_time("OPS-1", datetime(2026, 1, 1, tzinfo=UTC))
    await client.set_description("OPS-1", "desc")
    await client.add_comment("OPS-1", "body")
    issue = await client.create_postmortem_issue(
        make_alert(), message_url="u", channel_name="c", summary="s", description="d"
    )
    assert issue.key.startswith("ADS-TEST-")
    await client.aclose()


@pytest.mark.asyncio
async def test_creates_issue_with_default_valid_incident_and_option_ids(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read()) if request.method == "POST" else None
        requests.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes":
            return httpx.Response(
                200,
                json={"values": [{"id": "10001", "name": "Incident"}]},
            )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001":
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "customfield_12345": {
                            "allowedValues": [
                                {"id": "101", "value": "Валидный"},
                                {"id": "102", "value": "Ложный"},
                                {"id": "103", "value": "Ожидаемый"},
                            ]
                        },
                        "customfield_23456": {
                            "allowedValues": [{"id": "201", "value": "Crit alert"}]
                        },
                        "customfield_34567": {"allowedValues": [{"id": "301", "value": "Да"}]},
                    }
                },
            )
        if request.url.path == "/rest/api/2/issue":
            return httpx.Response(201, json={"key": "OPS-1"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        issue = await client.create_issue(
            make_alert(),
            message_url="https://mattermost.example.com/_redirect/pl/post",
            channel_name="alerts",
        )
    finally:
        await client.aclose()

    issue_body = requests[-1]["body"]["fields"]
    assert issue.key == "OPS-1"
    assert requests[0]["path"] == "/rest/api/2/issue/createmeta/OPS/issuetypes"
    assert requests[1]["path"] == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001"
    assert "customfield_12345" not in issue_body
    assert issue_body["customfield_23456"] == {"id": "201"}
    assert issue_body["customfield_34567"] == {"id": "301"}
    assert isinstance(issue_body["description"], str)


@pytest.mark.asyncio
async def test_jira_preflight_resolves_fields_and_options(settings):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes":
            return httpx.Response(
                200,
                json={"values": [{"id": "10001", "name": "Incident"}]},
            )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001":
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "customfield_12345": {
                            "allowedValues": [
                                {"id": "101", "value": "Валидный"},
                                {"id": "102", "value": "Ложный"},
                                {"id": "103", "value": "Ожидаемый"},
                            ]
                        },
                        "customfield_23456": {
                            "allowedValues": [{"id": "201", "value": "Crit alert"}]
                        },
                        "customfield_34567": {"allowedValues": [{"id": "301", "value": "Да"}]},
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        result = await client.preflight_check()
    finally:
        await client.aclose()

    assert result["jira_issue_type_id"] == "10001"
    assert result["create_field_count"] == 3
    assert result["valid_incident_field_id"] == "customfield_12345"
    assert result["source_option"] == {"id": "201"}
    assert result["is_crit_alert_option"] == {"id": "301"}
    assert requests == [
        "/rest/api/2/issue/createmeta/OPS/issuetypes",
        "/rest/api/2/issue/createmeta/OPS/issuetypes/10001",
    ]


@pytest.mark.asyncio
async def test_creates_postmortem_issue_without_alert_source_fields(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read()) if request.method == "POST" else None
        requests.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path == "/rest/api/2/issue":
            return httpx.Response(201, json={"key": "OPS-1"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        issue = await client.create_postmortem_issue(
            make_alert(channel_id="incidents-channel"),
            message_url="https://mattermost.example.com/_redirect/pl/incident",
            channel_name="incidents",
            summary="[INC] 15.11.2023 - Ручной инцидент",
            description="PM template",
        )
    finally:
        await client.aclose()

    issue_body = requests[-1]["body"]["fields"]
    assert issue.key == "OPS-1"
    assert len(requests) == 1
    assert requests[0]["method"] == "POST"
    assert requests[0]["path"] == "/rest/api/2/issue"
    assert issue_body["summary"] == "[INC] 15.11.2023 - Ручной инцидент"
    assert issue_body["description"] == "PM template"
    assert issue_body["labels"] == ["mattermost-incident", "postmortem"]
    assert "customfield_12345" not in issue_body
    assert "customfield_23456" not in issue_body
    assert "customfield_34567" not in issue_body


@pytest.mark.asyncio
async def test_creates_issue_with_paged_create_metadata_fields(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read()) if request.method == "POST" else None
        requests.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes":
            return httpx.Response(
                200,
                json={
                    "last": True,
                    "size": 1,
                    "start": 0,
                    "values": [{"id": "10001", "name": "Incident"}],
                },
            )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001":
            if request.url.params.get("startAt") == "0":
                return httpx.Response(
                    200,
                    json={
                        "last": False,
                        "size": 1,
                        "start": 0,
                        "total": 4,
                        "values": [
                            {
                                "fieldId": "summary",
                                "name": "Summary",
                            },
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "last": True,
                    "size": 3,
                    "start": 1,
                    "total": 3,
                    "values": [
                        {
                            "fieldId": "customfield_12345",
                            "allowedValues": [
                                {"id": "101", "value": "Валидный"},
                            ],
                        },
                        {
                            "fieldId": "customfield_23456",
                            "allowedValues": [{"id": "201", "value": "Crit alert"}],
                        },
                        {
                            "fieldId": "customfield_34567",
                            "allowedValues": [{"id": "301", "value": "Да"}],
                        },
                    ],
                },
            )
        if request.url.path == "/rest/api/2/issue":
            return httpx.Response(201, json={"key": "OPS-1"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        issue = await client.create_issue(
            make_alert(),
            message_url="https://mattermost.example.com/_redirect/pl/post",
            channel_name="alerts",
        )
    finally:
        await client.aclose()

    issue_body = requests[-1]["body"]["fields"]
    field_metadata_requests = [
        request
        for request in requests
        if request["path"] == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001"
    ]
    assert issue.key == "OPS-1"
    assert len(field_metadata_requests) == 2
    assert issue_body["customfield_23456"] == {"id": "201"}
    assert issue_body["customfield_34567"] == {"id": "301"}


@pytest.mark.asyncio
async def test_create_issue_sends_start_field_when_configured(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read()) if request.method == "POST" else None
        requests.append({"method": request.method, "path": request.url.path, "body": body})
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes":
            return httpx.Response(200, json={"values": [{"id": "10001", "name": "Incident"}]})
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001":
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "customfield_23456": {
                            "allowedValues": [{"id": "201", "value": "Crit alert"}]
                        },
                        "customfield_34567": {"allowedValues": [{"id": "301", "value": "Да"}]},
                    }
                },
            )
        if request.url.path == "/rest/api/2/issue":
            return httpx.Response(201, json={"key": "OPS-1"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        replace(settings, jira_start_field="customfield_45678"),
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        await client.create_issue(
            make_alert(),
            message_url="https://mattermost.example.com/_redirect/pl/post",
            channel_name="alerts",
        )
    finally:
        await client.aclose()

    issue_body = requests[-1]["body"]["fields"]
    assert issue_body["customfield_45678"] == "2023-11-15T01:13:20.000+0300"


@pytest.mark.asyncio
async def test_jira_client_uses_bearer_auth_by_default(settings):
    headers = jira_module.build_jira_auth_headers(settings)

    assert headers["Authorization"] == "Bearer jira-token"
    assert headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_jira_client_uses_rest_api_v2(settings):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/rest/api/2/issue/OPS-1":
            return httpx.Response(
                200, json={"fields": {"customfield_12345": {"value": "Валидный"}}}
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            headers=jira_module.build_jira_auth_headers(settings),
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        value = await client.get_valid_incident("OPS-1")
    finally:
        await client.aclose()

    assert value is True
    assert requests == ["/rest/api/2/issue/OPS-1"]


@pytest.mark.asyncio
async def test_resolves_valid_incident_field_name_from_jira(settings):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/rest/api/2/field":
            return httpx.Response(
                200,
                json=[
                    {"id": "summary", "name": "Summary"},
                    {"id": "customfield_12345", "name": "Valid Incident"},
                ],
            )
        if request.url.path == "/rest/api/2/issue/OPS-1":
            return httpx.Response(
                200, json={"fields": {"customfield_12345": {"value": "Валидный"}}}
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        replace(settings, jira_valid_incident_field="Valid Incident"),
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        value = await client.get_valid_incident("OPS-1")
    finally:
        await client.aclose()

    assert value is True
    assert requests == ["/rest/api/2/field", "/rest/api/2/issue/OPS-1"]


@pytest.mark.asyncio
async def test_updates_valid_incident_as_jira_option(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.read()) if request.method == "PUT" else None,
            }
        )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes":
            return httpx.Response(
                200,
                json={"values": [{"id": "10001", "name": "Incident"}]},
            )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001":
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "customfield_12345": {"allowedValues": [{"id": "201", "value": "Валидный"}]}
                    }
                },
            )
        if request.url.path == "/rest/api/2/issue/OPS-1":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        await client.set_valid_incident("OPS-1", True)
    finally:
        await client.aclose()

    assert requests == [
        {
            "method": "GET",
            "path": "/rest/api/2/issue/createmeta/OPS/issuetypes",
            "body": None,
        },
        {
            "method": "GET",
            "path": "/rest/api/2/issue/createmeta/OPS/issuetypes/10001",
            "body": None,
        },
        {
            "method": "PUT",
            "path": "/rest/api/2/issue/OPS-1",
            "body": {"fields": {"customfield_12345": {"id": "201"}}},
        },
    ]


@pytest.mark.asyncio
async def test_updates_validity_with_end_field_when_configured(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.read()) if request.method == "PUT" else None,
            }
        )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes":
            return httpx.Response(
                200,
                json={"values": [{"id": "10001", "name": "Incident"}]},
            )
        if request.url.path == "/rest/api/2/issue/createmeta/OPS/issuetypes/10001":
            return httpx.Response(
                200,
                json={
                    "fields": {
                        "customfield_12345": {"allowedValues": [{"id": "202", "value": "Ложный"}]}
                    }
                },
            )
        if request.url.path == "/rest/api/2/issue/OPS-1":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        replace(settings, jira_end_field="customfield_56789"),
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        await client.set_validity(
            "OPS-1",
            "Ложный",
            ended_at=datetime(2026, 5, 29, 22, 30, tzinfo=UTC),
        )
    finally:
        await client.aclose()

    assert requests[-1] == {
        "method": "PUT",
        "path": "/rest/api/2/issue/OPS-1",
        "body": {
            "fields": {
                "customfield_12345": {"id": "202"},
                "customfield_56789": "2026-05-30T01:30:00.000+0300",
            }
        },
    }


@pytest.mark.asyncio
async def test_updates_end_field_without_validity(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.read()) if request.method == "PUT" else None,
            }
        )
        if request.url.path == "/rest/api/2/issue/OPS-1":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        replace(settings, jira_end_field="customfield_56789"),
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        await client.set_end_time(
            "OPS-1",
            datetime(2026, 5, 29, 22, 30, tzinfo=UTC),
        )
    finally:
        await client.aclose()

    assert requests == [
        {
            "method": "PUT",
            "path": "/rest/api/2/issue/OPS-1",
            "body": {
                "fields": {
                    "customfield_56789": "2026-05-30T01:30:00.000+0300",
                }
            },
        }
    ]


# --- Ops channel: created-issue feed ----------------------------------------


def _ops_posts(service):
    return [c for c in service.mattermost.created_posts if c["channel_id"] == "ops-channel"]


@pytest.mark.asyncio
async def test_ops_channel_receives_created_issue_from_alert(settings):
    service = _build_service(replace(settings, mattermost_ops_channel_id="ops-channel"))
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    ops_posts = _ops_posts(service)
    assert len(ops_posts) == 1
    text = ops_posts[0]["props"]["attachments"][0]["text"]
    assert "OPS-1" in text
    assert service.mattermost.permalink(post.id) in text
    assert ops_posts[0]["root_id"] is None


@pytest.mark.asyncio
async def test_ops_channel_issue_is_mirrored_in_read_only(settings):
    """In read-only the ops issue announcement is no longer skipped — it is posted
    (redirected to the audit channel by the mirror in a real shadow), showing the
    clean ``ADS-TEST`` stub key."""
    service = _build_service(
        replace(settings, read_only_mode=True, mattermost_ops_channel_id="ops-channel")
    )
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    ops_posts = _ops_posts(service)
    assert len(ops_posts) == 1
    text = ops_posts[0]["props"]["attachments"][0]["text"]
    assert "ADS-TEST" in text and "ADS-TEST-" not in text  # clean stub, not the suffixed key


@pytest.mark.asyncio
async def test_ops_channel_silent_when_unconfigured(service):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    assert _ops_posts(service) == []


@pytest.mark.asyncio
async def test_ops_channel_receives_manual_incident_issue(settings):
    service = _build_service(
        replace(
            settings,
            service_public_url="https://bot.example.com",
            interactive_buttons_enabled=True,
            mattermost_ops_channel_id="ops-channel",
        )
    )
    service.llm = FakeLlmClient()
    post = _manual_post()
    service.mattermost.posts[post.id] = post
    await service.handle_manual_incident_post(post)
    await service.handle_incident_action(
        action="create_task", incident_post_id=post.id, user_id="opener"
    )
    ops_posts = _ops_posts(service)
    assert len(ops_posts) == 1
    assert "OPS-1" in ops_posts[0]["props"]["attachments"][0]["text"]


@pytest.mark.asyncio
async def test_set_time_to_fix_sends_minutes(settings):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.read()) if request.method == "PUT" else None,
            }
        )
        if request.url.path == "/rest/api/2/issue/OPS-1":
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = jira_module.JiraClient(
        replace(settings, jira_time_to_fix_field="customfield_77777"),
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        await client.set_time_to_fix("OPS-1", 42)
    finally:
        await client.aclose()

    assert requests == [
        {
            "method": "PUT",
            "path": "/rest/api/2/issue/OPS-1",
            "body": {"fields": {"customfield_77777": 42}},
        }
    ]


@pytest.mark.asyncio
async def test_set_time_to_fix_skipped_when_field_unconfigured(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected when the field is not configured")

    client = jira_module.JiraClient(
        settings,
        http_client=httpx.AsyncClient(
            base_url=settings.jira_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )
    try:
        await client.set_time_to_fix("OPS-1", 10)
    finally:
        await client.aclose()


# --- Idempotency / recovery: confirmation + backfill + pending work ----------


async def _confirm_via_reaction(service, post, *, user_id="validator", create_at=1):
    """Drive the alert -> incident confirmation through the validity reaction,
    matching the flow used elsewhere in the suite (buttons disabled fixture)."""
    return await service.handle_reaction(
        ReactionEvent(
            post_id=post.id,
            user_id=user_id,
            emoji_name="incident",
            create_at=create_at,
        )
    )


@pytest.mark.asyncio
async def test_update_jira_for_confirmation_idempotent_and_sync_back(service):
    """When Jira already reports the issue valid, confirmation syncs the local
    flag instead of issuing a duplicate set_valid_incident; the description swap
    and confirmation comment each happen exactly once across repeat confirms."""
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    issue_key = service.repository.get_by_post_id(post.id).jira_issue_key
    assert issue_key == "OPS-1"

    # Pretend Jira already flipped the field to valid before we confirm.
    service.jira.valid_by_issue[issue_key] = True

    await _confirm_via_reaction(service, post)

    ticket = service.repository.get_by_post_id(post.id)
    assert ticket.valid_incident is True
    # sync-back path: no set_valid_incident PUT issued.
    assert service.jira.valid_updates == []
    assert ticket.confirmation_status == "confirmed"
    assert service.jira.descriptions == [
        (issue_key, service.jira.descriptions[0][1]),
    ]
    assert len(service.jira.comments) == 1

    # Confirming a second time is a no-op for the one-shot Jira mutations.
    await _confirm_via_reaction(service, post, create_at=2)

    assert service.jira.valid_updates == []
    assert len(service.jira.descriptions) == 1
    assert len(service.jira.comments) == 1


@pytest.mark.asyncio
async def test_backfill_replays_idempotently(service):
    """Backfill is a no-op for posts that already have a ticket and only creates
    issues for genuinely new posts; with a non-positive limit it never fetches."""
    existing = make_alert()
    service.mattermost.posts[existing.id] = existing
    await service.handle_alert_post(existing)
    assert len(service.jira.created_payloads) == 1

    fresh = make_alert(post_id="freshalertpost000000000002")
    service.mattermost.posts[fresh.id] = fresh

    async def fake_fetch(channel_id, *, limit):
        return [existing, fresh]

    service.mattermost.fetch_recent_channel_posts = fake_fetch

    service.settings = replace(service.settings, backfill_recent_posts_limit=10)
    await service.backfill_recent_alerts()

    # The already-ticketed post produced no second issue; only `fresh` was new.
    assert len(service.jira.created_payloads) == 2
    assert service.repository.get_by_post_id(fresh.id).jira_issue_key == "OPS-2"

    # limit <= 0 must short-circuit before touching the Mattermost client.
    called = False

    async def guard_fetch(channel_id, *, limit):
        nonlocal called
        called = True
        return []

    service.mattermost.fetch_recent_channel_posts = guard_fetch
    service.settings = replace(service.settings, backfill_recent_posts_limit=0)
    await service.backfill_recent_alerts()
    assert called is False


@pytest.mark.asyncio
async def test_jira_create_failure_marks_pending_and_retries_to_success(service):
    """A one-shot create_issue failure leaves the ticket keyless and failed;
    clearing the failure and draining pending work creates exactly one issue and
    posts the "Создана задача" reply exactly once."""
    real_create = service.jira.create_issue
    calls = {"n": 0}

    async def flaky_create(post, *, message_url, channel_name):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ApiError("boom: jira create down")
        return await real_create(post, message_url=message_url, channel_name=channel_name)

    service.jira.create_issue = flaky_create

    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    ticket = service.repository.get_by_post_id(post.id)
    assert ticket is not None
    assert ticket.jira_issue_key is None
    assert ticket.creation_status == "failed_jira"
    assert ticket.last_error is not None and "boom" in ticket.last_error
    assert _issue_created_replies(service, post.id) == []

    # Recovery: subsequent create succeeds; pending work drains the ticket.
    await service.process_pending_work()

    ticket = service.repository.get_by_post_id(post.id)
    assert ticket.jira_issue_key == "OPS-1"
    assert ticket.creation_status == "jira_created"
    assert ticket.last_error is None
    # Exactly one issue created (failed attempt did not produce a payload).
    assert len(service.jira.created_payloads) == 1
    # "Создана задача" reply posted exactly once.
    assert len(_issue_created_replies(service, post.id)) == 1


@pytest.mark.asyncio
async def test_confirm_failure_is_recovered_by_process_pending_work(service):
    """A one-shot set_valid_incident failure during confirmation leaves the
    ticket in failed_confirmation; clearing it and running pending work drains
    to valid_incident=True with the confirmation comment added exactly once."""
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)

    real_set_valid = service.jira.set_valid_incident
    calls = {"n": 0}

    async def flaky_set_valid(issue_key, value):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ApiError("boom: valid_incident update down")
        return await real_set_valid(issue_key, value)

    service.jira.set_valid_incident = flaky_set_valid

    result = await _confirm_via_reaction(service, post)
    assert result.status == ConfirmationStatus.ERROR

    ticket = service.repository.get_by_post_id(post.id)
    assert ticket.confirmation_status == "failed_confirmation"
    assert ticket.valid_incident is False
    # The one-shot failure aborted before the description/comment swap.
    assert service.jira.comments == []

    # Recovery: subsequent set_valid succeeds; pending work drains confirmation.
    await service.process_pending_work()

    ticket = service.repository.get_by_post_id(post.id)
    assert ticket.valid_incident is True
    assert ticket.confirmation_status == "confirmed"
    # Confirmation comment added exactly once (no duplication on recovery).
    assert len(service.jira.comments) == 1
    assert service.jira.descriptions.count(service.jira.descriptions[0]) == 1
    assert len(service.jira.descriptions) == 1


def _issue_created_replies(service, post_id, *, issue_key="OPS-1"):
    """Bot thread replies announcing the created Jira issue, matched on the
    persisted ``jira_issue_key`` prop (the notice may be a bare message or a
    boxed attachment depending on whether interactive buttons are enabled)."""
    return [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post_id
        and (created["props"] or {}).get("jira_issue_key") == issue_key
    ]
