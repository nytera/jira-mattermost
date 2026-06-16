from __future__ import annotations

import base64
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from mm_jira_bot.config import Settings
from mm_jira_bot.domain import JiraIssue, MattermostPost, utc_now
from mm_jira_bot.formatting import truncate_for_summary
from mm_jira_bot.logging import log_event
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


def jira_option(value: str, option_id: str | None = None) -> dict[str, str]:
    if option_id:
        return {"id": option_id}
    return {"value": value}


def _payload_option_summary(payload: dict[str, str]) -> dict[str, str]:
    return {key: payload[key] for key in ("id", "value") if key in payload}


def build_jira_auth_headers(settings: Settings) -> dict[str, str]:
    auth_type = settings.jira_auth_type.casefold()
    if auth_type == "bearer":
        authorization = f"Bearer {settings.jira_api_token}"
    elif auth_type == "basic":
        auth_token = base64.b64encode(
            f"{settings.jira_email}:{settings.jira_api_token}".encode("utf-8")
        ).decode("ascii")
        authorization = f"Basic {auth_token}"
    else:
        raise ValueError("JIRA_AUTH_TYPE must be 'bearer' or 'basic'")
    return {
        "Authorization": authorization,
        "Content-Type": "application/json",
    }


def build_jira_description(
    post: MattermostPost,
    *,
    message_url: str,
    channel_name: str | None,
    use_adf: bool,
) -> dict[str, Any] | str:
    created_at = post.created_at_datetime.isoformat() if post.created_at_datetime else ""
    if not use_adf:
        lines = [
            "Mattermost alert",
            "",
            post.message,
            "",
            f"Author: {post.user_id}",
            f"Message time: {created_at}",
            f"Original Mattermost message: {message_url}",
            f"Mattermost post_id: {post.id}",
            f"Channel: {channel_name or post.channel_id}",
            f"Valid Incident: {VALID_INCIDENT_EMPTY_VALUE}",
        ]
        return "\n".join(lines)
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
    valid_incident_option: dict[str, str] | None = None,
    source_option: dict[str, str] | None = None,
    is_crit_alert_option: dict[str, str] | None = None,
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
            post,
            message_url=message_url,
            channel_name=channel_name,
            use_adf=settings.jira_rest_api_version == "3",
        ),
        valid_incident_field_id: valid_incident_option
        or jira_option(VALID_INCIDENT_EMPTY_VALUE),
        source_field_id: source_option or jira_option(JIRA_SOURCE_VALUE),
        is_crit_alert_field_id: is_crit_alert_option
        or jira_option(JIRA_IS_CRIT_ALERT_VALUE),
        "labels": ["mattermost-alert"],
    }
    return {"fields": fields}


def build_confirmation_comment(
    *,
    incident_message_url: str,
    confirmed_by_user_id: str,
    use_adf: bool,
) -> dict[str, Any] | str:
    if use_adf:
        return adf_document(
            [
                _paragraph("Alert confirmed as a valid incident from Mattermost."),
                _link_paragraph("Incident channel message", incident_message_url),
                _paragraph(f"Confirmed by: {confirmed_by_user_id}"),
            ]
        )
    return (
        "Alert confirmed as a valid incident from Mattermost.\n\n"
        f"Incident channel message: {incident_message_url}\n"
        f"Confirmed by: {confirmed_by_user_id}"
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
        self._create_fields: dict[str, Any] | None = None
        self._own_client = http_client is None
        log_event(
            logger,
            logging.INFO,
            "jira.client.configured",
            jira_base_url=settings.jira_base_url,
            jira_auth_type=settings.jira_auth_type,
            jira_rest_api_version=settings.jira_rest_api_version,
            jira_project_key=settings.jira_project_key,
            jira_issue_type=settings.jira_issue_type,
            configured_valid_incident_field=settings.jira_valid_incident_field,
            configured_source_field=settings.jira_source_field,
            configured_is_crit_alert_field=settings.jira_is_crit_alert_field,
        )
        self._client = http_client or httpx.AsyncClient(
            base_url=settings.jira_base_url,
            timeout=20,
            headers=build_jira_auth_headers(settings),
        )

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    def _api_path(self, path: str) -> str:
        return f"/rest/api/{self._settings.jira_rest_api_version}/{path.lstrip('/')}"

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
        valid_incident_option = await self._get_option_payload(
            valid_incident_field_id, VALID_INCIDENT_EMPTY_VALUE
        )
        source_option = await self._get_option_payload(
            source_field_id, JIRA_SOURCE_VALUE
        )
        is_crit_alert_option = await self._get_option_payload(
            is_crit_alert_field_id, JIRA_IS_CRIT_ALERT_VALUE
        )
        payload = build_jira_issue_payload(
            self._settings,
            valid_incident_field_id,
            source_field_id,
            is_crit_alert_field_id,
            post,
            message_url=message_url,
            channel_name=channel_name,
            valid_incident_option=valid_incident_option,
            source_option=source_option,
            is_crit_alert_option=is_crit_alert_option,
        )
        fields = payload["fields"]
        description = fields.get("description")
        log_event(
            logger,
            logging.INFO,
            "jira.issue.payload_prepared",
            mattermost_post_id=post.id,
            jira_project_key=self._settings.jira_project_key,
            jira_issue_type=self._settings.jira_issue_type,
            jira_rest_api_version=self._settings.jira_rest_api_version,
            summary_length=len(str(fields.get("summary", ""))),
            description_type=type(description).__name__,
            valid_incident_field_id=valid_incident_field_id,
            valid_incident_option=_payload_option_summary(valid_incident_option),
            source_field_id=source_field_id,
            source_option=_payload_option_summary(source_option),
            is_crit_alert_field_id=is_crit_alert_field_id,
            is_crit_alert_option=_payload_option_summary(is_crit_alert_option),
        )

        async def operation() -> JiraIssue:
            response = await self._client.post(self._api_path("issue"), json=payload)
            self._raise_for_status(response, "Failed to create Jira issue")
            data = response.json()
            key = data["key"]
            log_event(
                logger,
                logging.INFO,
                "jira.issue.create_succeeded",
                mattermost_post_id=post.id,
                jira_issue_key=key,
            )
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
                self._api_path(f"issue/{issue_key}"), params={"fields": field_id}
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
        option_payload = await self._get_option_payload(field_id, option_value)
        payload = {"fields": {field_id: option_payload}}
        log_event(
            logger,
            logging.INFO,
            "jira.valid_incident.payload_prepared",
            jira_issue_key=issue_key,
            field_id=field_id,
            requested_value=option_value,
            option_payload=_payload_option_summary(option_payload),
        )

        async def operation() -> None:
            response = await self._client.put(
                self._api_path(f"issue/{issue_key}"), json=payload
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
                use_adf=self._settings.jira_rest_api_version == "3",
            )
        }

        async def operation() -> None:
            response = await self._client.post(
                self._api_path(f"issue/{issue_key}/comment"), json=payload
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
                self._api_path(f"issue/{issue_key}/transitions"), json=payload
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
            log_event(
                logger,
                logging.DEBUG,
                "jira.field.cache_hit",
                jira_field_configured=configured_field,
                jira_field_id=field_id,
            )
            return field_id

        if CUSTOM_FIELD_ID_PATTERN.fullmatch(configured_field):
            self._field_ids[configured_field] = configured_field
            log_event(
                logger,
                logging.INFO,
                "jira.field.using_configured_id",
                jira_field_configured=configured_field,
                jira_field_id=configured_field,
            )
            return configured_field

        async def operation() -> str:
            response = await self._client.get(self._api_path("field"))
            self._raise_for_status(response, "Failed to fetch Jira fields")
            configured_field_normalized = configured_field.casefold()
            for field in response.json():
                if field.get("name", "").casefold() == configured_field_normalized:
                    field_id = field.get("id")
                    if isinstance(field_id, str) and field_id:
                        schema = field.get("schema")
                        log_event(
                            logger,
                            logging.INFO,
                            "jira.field.resolved",
                            jira_field_configured=configured_field,
                            jira_field_id=field_id,
                            jira_field_name=field.get("name"),
                            jira_field_schema=schema if isinstance(schema, dict) else None,
                        )
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

    async def _get_create_fields(self) -> dict[str, Any]:
        if self._create_fields is not None:
            log_event(
                logger,
                logging.DEBUG,
                "jira.create_metadata.cache_hit",
                jira_project_key=self._settings.jira_project_key,
                jira_issue_type=self._settings.jira_issue_type,
            )
            return self._create_fields

        params = {
            "projectKeys": self._settings.jira_project_key,
            "expand": "projects.issuetypes.fields",
        }
        if self._settings.jira_issue_type.isdigit():
            params["issuetypeIds"] = self._settings.jira_issue_type
        else:
            params["issuetypeNames"] = self._settings.jira_issue_type

        async def operation() -> dict[str, Any]:
            response = await self._client.get(
                self._api_path("issue/createmeta"), params=params
            )
            self._raise_for_status(response, "Failed to fetch Jira create metadata")
            data = response.json()
            for project in data.get("projects", []):
                for issue_type in project.get("issuetypes", []):
                    fields = issue_type.get("fields")
                    if isinstance(fields, dict):
                        log_event(
                            logger,
                            logging.INFO,
                            "jira.create_metadata.loaded",
                            jira_project_key=self._settings.jira_project_key,
                            jira_issue_type=self._settings.jira_issue_type,
                            field_count=len(fields),
                            tracked_fields=[
                                field_id
                                for field_id in (
                                    self._field_ids.get(
                                        self._settings.jira_valid_incident_field
                                    ),
                                    self._field_ids.get(
                                        self._settings.jira_source_field
                                    ),
                                    self._field_ids.get(
                                        self._settings.jira_is_crit_alert_field
                                    ),
                                )
                                if field_id
                            ],
                        )
                        return fields
            raise ApiError(
                "Jira create metadata did not include fields for "
                f"project={self._settings.jira_project_key} "
                f"issue_type={self._settings.jira_issue_type}",
                retryable=False,
            )

        self._create_fields = await retry_async(
            operation,
            attempts=self._settings.api_retry_attempts,
            base_delay_seconds=self._settings.api_retry_base_delay_seconds,
            logger=logger,
            event="jira.get_create_metadata",
            jira_project_key=self._settings.jira_project_key,
            jira_issue_type=self._settings.jira_issue_type,
        )
        return self._create_fields

    async def _get_option_payload(self, field_id: str, value: str) -> dict[str, str]:
        fields = await self._get_create_fields()
        field = fields.get(field_id)
        if not isinstance(field, dict):
            log_event(
                logger,
                logging.WARNING,
                "jira.option.field_missing_in_create_metadata",
                jira_field_id=field_id,
                requested_value=value,
            )
            return jira_option(value)

        allowed_values = field.get("allowedValues")
        if not isinstance(allowed_values, list) or not allowed_values:
            log_event(
                logger,
                logging.WARNING,
                "jira.option.no_allowed_values",
                jira_field_id=field_id,
                requested_value=value,
                jira_field_name=field.get("name"),
            )
            return jira_option(value)

        normalized_value = value.casefold()
        allowed_labels: list[str] = []
        for option in allowed_values:
            if not isinstance(option, dict):
                continue
            option_value = option.get("value") or option.get("name")
            if not isinstance(option_value, str):
                continue
            allowed_labels.append(option_value)
            if option_value.casefold() == normalized_value:
                option_id = option.get("id")
                payload = jira_option(
                    option_value, option_id if isinstance(option_id, str) else None
                )
                log_event(
                    logger,
                    logging.INFO,
                    "jira.option.resolved",
                    jira_field_id=field_id,
                    jira_field_name=field.get("name"),
                    requested_value=value,
                    option_value=option_value,
                    option_payload=_payload_option_summary(payload),
                    allowed_values_count=len(allowed_values),
                )
                return payload

        log_event(
            logger,
            logging.ERROR,
            "jira.option.not_found",
            jira_field_id=field_id,
            jira_field_name=field.get("name"),
            requested_value=value,
            allowed_values=allowed_labels,
        )
        raise ApiError(
            f"Jira option '{value}' was not found for field {field_id}. "
            f"Allowed values: {', '.join(allowed_labels) or 'none'}",
            retryable=False,
        )

    def _raise_for_status(self, response: httpx.Response, message: str) -> None:
        if response.is_success:
            return
        log_event(
            logger,
            logging.ERROR,
            "jira.http.error",
            status_code=response.status_code,
            reason_phrase=response.reason_phrase,
            request_method=response.request.method,
            request_url=str(response.request.url),
            response_text=response.text,
        )
        raise ApiError(
            f"{message}: HTTP {response.status_code} {response.text}",
            status_code=response.status_code,
            retryable=is_retryable_status(response.status_code),
        )
