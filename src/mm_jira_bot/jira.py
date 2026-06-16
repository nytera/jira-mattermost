from __future__ import annotations

import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from mm_jira_bot.config import Settings
from mm_jira_bot.domain import JiraIssue, MattermostPost, utc_now
from mm_jira_bot.formatting import truncate_for_summary
from mm_jira_bot.retry import ApiError, is_retryable_status, retry_async

logger = logging.getLogger(__name__)
CUSTOM_FIELD_ID_PATTERN = re.compile(r"^customfield_\d+$")
VALID_INCIDENT_EMPTY_VALUE = "Не заполнено"
VALID_INCIDENT_CONFIRMED_VALUE = "Валидный"
JIRA_SOURCE_VALUE = "Crit alert"
JIRA_IS_CRIT_ALERT_VALUE = "Да"


def _text_node(text: str, **attrs: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": text}
    if attrs:
        node.update(attrs)
    return node


def _paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [_text_node(text)] if text else []}


def _link_paragraph(label: str, url: str) -> dict[str, Any]:
    return {
        "type": "paragraph",
        "content": [
            _text_node(f"{label}: "),
            {
                "type": "text",
                "text": url,
                "marks": [{"type": "link", "attrs": {"href": url}}],
            },
        ],
    }


def adf_document(paragraphs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": paragraphs}


def jira_option(value: str) -> dict[str, str]:
    return {"value": value}


def build_jira_description(
    post: MattermostPost,
    *,
    message_url: str,
    channel_name: str | None,
) -> dict[str, Any]:
    created_at = post.created_at_datetime.isoformat() if post.created_at_datetime else ""
    return adf_document(
        [
            _paragraph("Mattermost alert"),
            _paragraph(post.message),
            _paragraph(f"Author: {post.user_id}"),
            _paragraph(f"Message time: {created_at}"),
            _link_paragraph("Original Mattermost message", message_url),
            _paragraph(f"Mattermost post_id: {post.id}"),
            _paragraph(f"Channel: {channel_name or post.channel_id}"),
            _paragraph(f"Valid Incident: {VALID_INCIDENT_EMPTY_VALUE}"),
        ]
    )


def build_jira_issue_payload(
    settings: Settings,
    valid_incident_field_id: str,
    source_field_id: str,
    is_crit_alert_field_id: str,
    post: MattermostPost,
    *,
    message_url: str,
    channel_name: str | None,
) -> dict[str, Any]:
    issue_type: dict[str, str]
    if settings.jira_issue_type.isdigit():
        issue_type = {"id": settings.jira_issue_type}
    else:
        issue_type = {"name": settings.jira_issue_type}

    created_at = post.created_at_datetime if post.create_at > 0 else utc_now()
    alert_date = created_at.astimezone(ZoneInfo(settings.incident_timezone)).strftime(
        "%d.%m.%y"
    )
    message_lines = post.message.splitlines()
    first_message_line = message_lines[0] if message_lines else ""

    fields: dict[str, Any] = {
        "project": {"key": settings.jira_project_key},
        "issuetype": issue_type,
        "summary": f"[INC] {alert_date} - {truncate_for_summary(first_message_line)}",
        "description": build_jira_description(
            post, message_url=message_url, channel_name=channel_name
        ),
        valid_incident_field_id: jira_option(VALID_INCIDENT_EMPTY_VALUE),
        source_field_id: jira_option(JIRA_SOURCE_VALUE),
        is_crit_alert_field_id: jira_option(JIRA_IS_CRIT_ALERT_VALUE),
        "labels": ["mattermost-alert"],
    }
    return {"fields": fields}


def build_confirmation_comment(
    *,
    incident_message_url: str,
    confirmed_by_user_id: str,
) -> dict[str, Any]:
    return adf_document(
        [
            _paragraph("Alert confirmed as a valid incident from Mattermost."),
            _link_paragraph("Incident channel message", incident_message_url),
            _paragraph(f"Confirmed by: {confirmed_by_user_id}"),
        ]
    )


class JiraClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._field_ids: dict[str, str] = {}
        self._own_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=settings.jira_base_url,
            timeout=20,
            headers={
                "Authorization": f"Bearer {settings.jira_api_token}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def create_issue(
        self,
        post: MattermostPost,
        *,
        message_url: str,
        channel_name: str | None,
    ) -> JiraIssue:
        valid_incident_field_id = await self._get_field_id(
            self._settings.jira_valid_incident_field
        )
        source_field_id = await self._get_field_id(self._settings.jira_source_field)
        is_crit_alert_field_id = await self._get_field_id(
            self._settings.jira_is_crit_alert_field
        )
        payload = build_jira_issue_payload(
            self._settings,
            valid_incident_field_id,
            source_field_id,
            is_crit_alert_field_id,
            post,
            message_url=message_url,
            channel_name=channel_name,
        )

        async def operation() -> JiraIssue:
            response = await self._client.post("/rest/api/2/issue", json=payload)
            self._raise_for_status(response, "Failed to create Jira issue")
            data = response.json()
            key = data["key"]
            return JiraIssue(key=key, url=f"{self._settings.jira_base_url}/browse/{key}")

        return await retry_async(
            operation,
            attempts=self._settings.api_retry_attempts,
            base_delay_seconds=self._settings.api_retry_base_delay_seconds,
            logger=logger,
            event="jira.create_issue",
            mattermost_post_id=post.id,
        )

    async def get_valid_incident(self, issue_key: str) -> bool | None:
        field_id = await self._get_field_id(self._settings.jira_valid_incident_field)

        async def operation() -> bool | None:
            response = await self._client.get(
                f"/rest/api/3/issue/{issue_key}", params={"fields": field_id}
            )
            self._raise_for_status(response, "Failed to read Jira issue")
            fields = response.json().get("fields", {})
            value = fields.get(field_id)
            if value is None:
                return None
            if isinstance(value, dict):
                option_value = value.get("value")
                if option_value == VALID_INCIDENT_CONFIRMED_VALUE:
                    return True
                if option_value == VALID_INCIDENT_EMPTY_VALUE:
                    return False
                return None
            return bool(value)

        return await retry_async(
            operation,
            attempts=self._settings.api_retry_attempts,
            base_delay_seconds=self._settings.api_retry_base_delay_seconds,
            logger=logger,
            event="jira.get_issue",
            jira_issue_key=issue_key,
        )

    async def set_valid_incident(self, issue_key: str, value: bool) -> None:
        field_id = await self._get_field_id(self._settings.jira_valid_incident_field)
        option_value = (
            VALID_INCIDENT_CONFIRMED_VALUE if value else VALID_INCIDENT_EMPTY_VALUE
        )
        payload = {"fields": {field_id: jira_option(option_value)}}

        async def operation() -> None:
            response = await self._client.put(
                f"/rest/api/3/issue/{issue_key}", json=payload
            )
            self._raise_for_status(response, "Failed to update Jira issue")

        await retry_async(
            operation,
            attempts=self._settings.api_retry_attempts,
            base_delay_seconds=self._settings.api_retry_base_delay_seconds,
            logger=logger,
            event="jira.update_valid_incident",
            jira_issue_key=issue_key,
        )

    async def add_confirmation_comment(
        self,
        issue_key: str,
        *,
        incident_message_url: str,
        confirmed_by_user_id: str,
    ) -> None:
        payload = {
            "body": build_confirmation_comment(
                incident_message_url=incident_message_url,
                confirmed_by_user_id=confirmed_by_user_id,
            )
        }

        async def operation() -> None:
            response = await self._client.post(
                f"/rest/api/3/issue/{issue_key}/comment", json=payload
            )
            self._raise_for_status(response, "Failed to add Jira comment")

        await retry_async(
            operation,
            attempts=self._settings.api_retry_attempts,
            base_delay_seconds=self._settings.api_retry_base_delay_seconds,
            logger=logger,
            event="jira.add_comment",
            jira_issue_key=issue_key,
        )

    async def transition_issue(self, issue_key: str, transition_id: str) -> None:
        payload = {"transition": {"id": transition_id}}

        async def operation() -> None:
            response = await self._client.post(
                f"/rest/api/3/issue/{issue_key}/transitions", json=payload
            )
            self._raise_for_status(response, "Failed to transition Jira issue")

        await retry_async(
            operation,
            attempts=self._settings.api_retry_attempts,
            base_delay_seconds=self._settings.api_retry_base_delay_seconds,
            logger=logger,
            event="jira.transition",
            jira_issue_key=issue_key,
            transition_id=transition_id,
        )

    async def _get_field_id(self, configured_field: str) -> str:
        field_id = self._field_ids.get(configured_field)
        if field_id is not None:
            return field_id

        if CUSTOM_FIELD_ID_PATTERN.fullmatch(configured_field):
            self._field_ids[configured_field] = configured_field
            return configured_field

        async def operation() -> str:
            response = await self._client.get("/rest/api/3/field")
            self._raise_for_status(response, "Failed to fetch Jira fields")
            configured_field_normalized = configured_field.casefold()
            for field in response.json():
                if field.get("name", "").casefold() == configured_field_normalized:
                    field_id = field.get("id")
                    if isinstance(field_id, str) and field_id:
                        return field_id
            raise ApiError(
                f"Jira field named '{configured_field}' was not found",
                retryable=False,
            )

        resolved_field_id = await retry_async(
            operation,
            attempts=self._settings.api_retry_attempts,
            base_delay_seconds=self._settings.api_retry_base_delay_seconds,
            logger=logger,
            event="jira.get_field_id",
            jira_field_name=configured_field,
        )
        self._field_ids[configured_field] = resolved_field_id
        return resolved_field_id

    def _raise_for_status(self, response: httpx.Response, message: str) -> None:
        if response.is_success:
            return
        raise ApiError(
            f"{message}: HTTP {response.status_code} {response.text}",
            status_code=response.status_code,
            retryable=is_retryable_status(response.status_code),
        )
