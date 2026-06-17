from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from mm_jira_bot.config import Settings
from mm_jira_bot.domain import JiraIssue, MattermostPost
from mm_jira_bot.http import AsyncApiClient
from mm_jira_bot.jira_payload import (
    JIRA_IS_CRIT_ALERT_VALUE,
    JIRA_SOURCE_VALUE,
    build_confirmation_comment,
    build_jira_issue_payload,
    format_jira_datetime,
    jira_option,
)
from mm_jira_bot.logging import get_logger
from mm_jira_bot.retry import ApiError, is_retryable_status

log = get_logger(__name__)
CUSTOM_FIELD_ID_PATTERN = re.compile(r"^customfield_\d+$")
VALID_INCIDENT_EMPTY_VALUE = "Не заполнено"
VALID_INCIDENT_CONFIRMED_VALUE = "Валидный"
VALID_INCIDENT_FALSE_VALUE = "Ложный"
VALID_INCIDENT_EXPECTED_VALUE = "Ожидаемый"


def _payload_option_summary(payload: dict[str, str]) -> dict[str, str]:
    return {key: payload[key] for key in ("id", "value") if key in payload}


def build_jira_auth_headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.jira_api_token}",
        "Content-Type": "application/json",
    }


def _create_fields_from_values(values: list[Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for field in values:
        if not isinstance(field, dict):
            continue
        field_id = field.get("fieldId")
        if isinstance(field_id, str) and field_id:
            fields[field_id] = field
    return fields


def _next_start_at(data: dict[str, Any], values: list[Any], start_at: int) -> int | None:
    if data.get("last") is True or data.get("isLast") is True:
        return None
    if not {"last", "isLast", "size", "start"} & data.keys():
        return None

    start = data.get("start")
    if not isinstance(start, int):
        start = start_at
    size = data.get("size")
    if not isinstance(size, int):
        size = len(values)
    if size <= 0:
        return None
    return start + size


class JiraClient(AsyncApiClient):
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._field_ids: dict[str, str] = {}
        self._create_fields: dict[str, Any] | None = None
        self._issue_type_id: str | None = None
        log.info(
            "jira.client.configured",
            jira_base_url=settings.jira_base_url,
            jira_auth_type="bearer",
            jira_rest_api_version="2",
            jira_project_key=settings.jira_project_key,
            jira_issue_type=settings.jira_issue_type,
            configured_valid_incident_field=settings.jira_valid_incident_field,
            configured_source_field=settings.jira_source_field,
            configured_is_crit_alert_field=settings.jira_is_crit_alert_field,
            configured_start_field=settings.jira_start_field,
            configured_end_field=settings.jira_end_field,
        )
        client = http_client or httpx.AsyncClient(
            base_url=settings.jira_base_url,
            timeout=20,
            headers=build_jira_auth_headers(settings),
        )
        super().__init__(settings, client, own_client=http_client is None, log=log)

    def _api_path(self, path: str) -> str:
        return f"/rest/api/2/{path.lstrip('/')}"

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
        start_field_id = (
            await self._get_field_id(self._settings.jira_start_field)
            if self._settings.jira_start_field
            else None
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
            start_field_id=start_field_id,
            source_option=source_option,
            is_crit_alert_option=is_crit_alert_option,
        )
        fields = payload["fields"]
        description = fields.get("description")
        log.info(
            "jira.issue.payload_prepared",
            mattermost_post_id=post.id,
            jira_project_key=self._settings.jira_project_key,
            jira_issue_type=self._settings.jira_issue_type,
            jira_rest_api_version="2",
            summary_length=len(str(fields.get("summary", ""))),
            description_type=type(description).__name__,
            valid_incident_field_id=valid_incident_field_id,
            valid_incident_on_create=False,
            start_field_id=start_field_id,
            source_field_id=source_field_id,
            source_option=_payload_option_summary(source_option),
            is_crit_alert_field_id=is_crit_alert_field_id,
            is_crit_alert_option=_payload_option_summary(is_crit_alert_option),
        )

        def parse(response: httpx.Response) -> JiraIssue:
            key = response.json()["key"]
            log.info(
                "jira.issue.create_succeeded",
                mattermost_post_id=post.id,
                jira_issue_key=key,
            )
            return JiraIssue(key=key, url=f"{self._settings.jira_base_url}/browse/{key}")

        return await self._request(
            "POST",
            self._api_path("issue"),
            json=payload,
            error_message="Failed to create Jira issue",
            event="jira.create_issue",
            parse=parse,
            mattermost_post_id=post.id,
        )

    async def get_valid_incident(self, issue_key: str) -> bool | None:
        field_id = await self._get_field_id(self._settings.jira_valid_incident_field)

        def parse(response: httpx.Response) -> bool | None:
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

        return await self._request(
            "GET",
            self._api_path(f"issue/{issue_key}"),
            params={"fields": field_id},
            error_message="Failed to read Jira issue",
            event="jira.get_issue",
            parse=parse,
            jira_issue_key=issue_key,
        )

    async def set_valid_incident(self, issue_key: str, value: bool) -> None:
        await self.set_validity(
            issue_key,
            VALID_INCIDENT_CONFIRMED_VALUE if value else VALID_INCIDENT_EMPTY_VALUE,
        )

    async def set_end_time(self, issue_key: str, ended_at: datetime) -> None:
        if not self._settings.jira_end_field:
            log.info(
                "jira.end_time.skipped_not_configured",
                jira_issue_key=issue_key,
            )
            return

        field_id = await self._get_field_id(self._settings.jira_end_field)
        payload = {"fields": {field_id: format_jira_datetime(ended_at)}}
        log.info(
            "jira.end_time.payload_prepared",
            jira_issue_key=issue_key,
            field_id=field_id,
        )
        await self._request(
            "PUT",
            self._api_path(f"issue/{issue_key}"),
            json=payload,
            error_message="Failed to update Jira issue end time",
            event="jira.update_end_time",
            jira_issue_key=issue_key,
        )

    async def set_validity(
        self,
        issue_key: str,
        option_value: str,
        *,
        ended_at: datetime | None = None,
    ) -> None:
        """Set the "Валидность" field to an arbitrary option value."""
        field_id = await self._get_field_id(self._settings.jira_valid_incident_field)
        option_payload = await self._get_option_payload(field_id, option_value)
        end_field_id = (
            await self._get_field_id(self._settings.jira_end_field)
            if ended_at is not None and self._settings.jira_end_field
            else None
        )
        fields = {field_id: option_payload}
        if end_field_id is not None:
            fields[end_field_id] = format_jira_datetime(ended_at)
        payload = {"fields": fields}
        log.info(
            "jira.validity.payload_prepared",
            jira_issue_key=issue_key,
            field_id=field_id,
            requested_value=option_value,
            option_payload=_payload_option_summary(option_payload),
            end_field_id=end_field_id,
        )
        await self._request(
            "PUT",
            self._api_path(f"issue/{issue_key}"),
            json=payload,
            error_message="Failed to update Jira issue",
            event="jira.update_validity",
            jira_issue_key=issue_key,
        )

    async def set_description(self, issue_key: str, description: str) -> None:
        payload = {"fields": {"description": description}}
        log.info(
            "jira.description.payload_prepared",
            jira_issue_key=issue_key,
            description_length=len(description),
        )
        await self._request(
            "PUT",
            self._api_path(f"issue/{issue_key}"),
            json=payload,
            error_message="Failed to update Jira description",
            event="jira.update_description",
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
        await self._request(
            "POST",
            self._api_path(f"issue/{issue_key}/comment"),
            json=payload,
            error_message="Failed to add Jira comment",
            event="jira.add_comment",
            jira_issue_key=issue_key,
        )

    async def transition_issue(self, issue_key: str, transition_id: str) -> None:
        await self._request(
            "POST",
            self._api_path(f"issue/{issue_key}/transitions"),
            json={"transition": {"id": transition_id}},
            error_message="Failed to transition Jira issue",
            event="jira.transition",
            jira_issue_key=issue_key,
            transition_id=transition_id,
        )

    async def _get_field_id(self, configured_field: str) -> str:
        field_id = self._field_ids.get(configured_field)
        if field_id is not None:
            log.debug(
                "jira.field.cache_hit",
                jira_field_configured=configured_field,
                jira_field_id=field_id,
            )
            return field_id

        if CUSTOM_FIELD_ID_PATTERN.fullmatch(configured_field):
            self._field_ids[configured_field] = configured_field
            log.info(
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
                        log.info(
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

        resolved_field_id = await self._retry(
            operation,
            event="jira.get_field_id",
            jira_field_name=configured_field,
        )
        self._field_ids[configured_field] = resolved_field_id
        return resolved_field_id

    async def _get_create_fields(self) -> dict[str, Any]:
        if self._create_fields is not None:
            log.debug(
                "jira.create_metadata.cache_hit",
                jira_project_key=self._settings.jira_project_key,
                jira_issue_type=self._settings.jira_issue_type,
            )
            return self._create_fields

        self._create_fields = await self._get_create_fields_for_issue_type()
        return self._create_fields

    async def _get_issue_type_id(self) -> str:
        if self._settings.jira_issue_type.isdigit():
            return self._settings.jira_issue_type
        if self._issue_type_id is not None:
            return self._issue_type_id

        async def operation() -> str:
            start_at = 0
            available_names: list[str] = []
            while True:
                response = await self._client.get(
                    self._api_path(
                        f"issue/createmeta/{self._settings.jira_project_key}/issuetypes"
                    ),
                    params={"startAt": start_at, "maxResults": 50},
                )
                self._raise_for_status(response, "Failed to fetch Jira issue types")
                data = response.json()
                issue_types = data.get("values")
                if not isinstance(issue_types, list):
                    raise ApiError(
                        "Jira issue types response did not include values",
                        retryable=False,
                    )

                configured_name = self._settings.jira_issue_type.casefold()
                for issue_type in issue_types:
                    if not isinstance(issue_type, dict):
                        continue
                    name = issue_type.get("name")
                    issue_type_id = issue_type.get("id")
                    if isinstance(name, str):
                        available_names.append(name)
                    if (
                        isinstance(name, str)
                        and name.casefold() == configured_name
                        and isinstance(issue_type_id, str)
                        and issue_type_id
                    ):
                        log.info(
                            "jira.issue_type.resolved",
                            jira_project_key=self._settings.jira_project_key,
                            jira_issue_type=self._settings.jira_issue_type,
                            jira_issue_type_id=issue_type_id,
                        )
                        return issue_type_id

                next_start_at = _next_start_at(data, issue_types, start_at)
                if next_start_at is None:
                    break
                start_at = next_start_at

            raise ApiError(
                f"Jira issue type '{self._settings.jira_issue_type}' was not found "
                f"for project {self._settings.jira_project_key}. "
                f"Available issue types: {', '.join(available_names) or 'none'}",
                retryable=False,
            )

        self._issue_type_id = await self._retry(
            operation,
            event="jira.get_issue_types",
            jira_project_key=self._settings.jira_project_key,
            jira_issue_type=self._settings.jira_issue_type,
        )
        return self._issue_type_id

    async def _get_create_fields_for_issue_type(self) -> dict[str, Any]:
        issue_type_id = await self._get_issue_type_id()

        async def operation() -> dict[str, Any]:
            start_at = 0
            paged_fields: dict[str, Any] = {}
            while True:
                response = await self._client.get(
                    self._api_path(
                        "issue/createmeta/"
                        f"{self._settings.jira_project_key}/issuetypes/{issue_type_id}"
                    ),
                    params={"startAt": start_at, "maxResults": 50},
                )
                self._raise_for_status(
                    response, "Failed to fetch Jira issue type create metadata"
                )
                data = response.json()
                fields = data.get("fields")
                if isinstance(fields, dict):
                    log.info(
                        "jira.create_metadata.loaded",
                        jira_project_key=self._settings.jira_project_key,
                        jira_issue_type=self._settings.jira_issue_type,
                        jira_issue_type_id=issue_type_id,
                        field_count=len(fields),
                        endpoint="issue_type",
                    )
                    return fields

                values = data.get("values")
                if not isinstance(values, list):
                    break
                paged_fields.update(_create_fields_from_values(values))
                next_start_at = _next_start_at(data, values, start_at)
                if next_start_at is None:
                    break
                start_at = next_start_at

            if paged_fields:
                log.info(
                    "jira.create_metadata.loaded",
                    jira_project_key=self._settings.jira_project_key,
                    jira_issue_type=self._settings.jira_issue_type,
                    jira_issue_type_id=issue_type_id,
                    field_count=len(paged_fields),
                    endpoint="issue_type_fields",
                )
                return paged_fields

            raise ApiError(
                "Jira issue type create metadata did not include fields for "
                f"project={self._settings.jira_project_key} "
                f"issue_type={self._settings.jira_issue_type}",
                retryable=False,
            )

        return await self._retry(
            operation,
            event="jira.get_issue_type_create_metadata",
            jira_project_key=self._settings.jira_project_key,
            jira_issue_type=self._settings.jira_issue_type,
            jira_issue_type_id=issue_type_id,
        )

    async def _get_option_payload(self, field_id: str, value: str) -> dict[str, str]:
        fields = await self._get_create_fields()
        field = fields.get(field_id)
        if not isinstance(field, dict):
            log.warning(
                "jira.option.field_missing_in_create_metadata",
                jira_field_id=field_id,
                requested_value=value,
            )
            return jira_option(value)

        allowed_values = field.get("allowedValues")
        if not isinstance(allowed_values, list) or not allowed_values:
            log.warning(
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
                log.info(
                    "jira.option.resolved",
                    jira_field_id=field_id,
                    jira_field_name=field.get("name"),
                    requested_value=value,
                    option_value=option_value,
                    option_payload=_payload_option_summary(payload),
                    allowed_values_count=len(allowed_values),
                )
                return payload

        log.error(
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
        log.error(
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
