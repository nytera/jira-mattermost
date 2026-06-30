"""Алерты: AlertMixin.

Полный жизненный цикл алерта в alert-канале: первичная обработка поста (создание
Jira-задачи через JiraSync / обработка повторов), выставление валидности
(`apply_validity_label`) и сбор вложений исходного поста. Методы вызываются собранным
`IncidentBotService` (см. `coordinator.py`); state
(`settings`/`repository`/`mattermost`/`jira`) ставит конструктор координатора, а
инцидент-механика, Jira-проводка, постмортем и summary живут в sibling-классах.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING, Any

from mm_jira_bot.domain import (
    ConfirmationResult,
    ConfirmationStatus,
    MattermostPost,
    backend_now,
    datetime_from_mattermost_ms,
    incident_ttf_minutes,
)
from mm_jira_bot.formatting import (
    alert_signature,
    format_thread_validity_changed,
    is_resolved_alert,
)
from mm_jira_bot.jira_payload import format_readonly_jira_params
from mm_jira_bot.logging import get_logger
from mm_jira_bot.repository import AlertTicket, AlertTicketRepository
from mm_jira_bot.retry import ApiError

if TYPE_CHECKING:
    from mm_jira_bot.config import Settings

# Имя логгера держим стабильным (`mm_jira_bot.service`) во всех файлах пакета —
# тесты и настроенные логгеры завязаны на него, а не на `__name__` модуля.
log = get_logger("mm_jira_bot.service")


def _copy_post_attachments(post: MattermostPost) -> list[dict]:
    props = post.props
    if not isinstance(props, dict):
        return []
    attachments = props.get("attachments")
    if not isinstance(attachments, list):
        return []
    return [deepcopy(attachment) for attachment in attachments if isinstance(attachment, dict)]


class AlertMixin:
    # State устанавливает coordinator.__init__; объявляем только то, что трогает
    # этот миксин, теми же типами, что декларирует конструктор: `settings`/
    # `repository` типизированы, клиенты `mattermost`/`jira` идут без аннотаций → `Any`.
    settings: Settings
    repository: AlertTicketRepository
    mattermost: Any
    jira: Any

    if TYPE_CHECKING:
        # Стабы sibling-методов из других классов собранного IncidentBotService —
        # pyright их иначе не видит на самом миксине. Сигнатуры повторяют реальные
        # (kw-only `*` и имена параметров важны для override-совместимости).
        # --- остаются в coordinator ---
        def _is_bot_post(self, post: MattermostPost) -> bool: ...

        # --- JiraSyncMixin ---
        async def _ensure_jira_issue(self, ticket: AlertTicket, is_repeat: bool = ...) -> None: ...

        async def _handle_expected_repeat(self, ticket: AlertTicket, root: AlertTicket) -> None: ...

        # --- IncidentMixin ---
        async def confirm_incident(
            self,
            post_id: str,
            *,
            confirmed_by_user_id: str,
            source: str,
            confirmed_at: datetime | None = ...,
        ) -> ConfirmationResult: ...

        # --- PostmortemMixin ---
        async def _set_time_to_fix(
            self, issue_key: str, ticket: AlertTicket, ended_at: datetime
        ) -> None: ...

        # --- SharedMixin ---
        def _is_alert_channel(self, channel_id: str) -> bool: ...

        async def _post_alert_thread_reply(
            self,
            post_id: str,
            *,
            channel_id: str,
            message: str,
            event: str,
            props: dict | None = ...,
            color: str = ...,
            mention: str | None = ...,
        ) -> None: ...

    async def handle_alert_post(self, post: MattermostPost) -> AlertTicket | None:
        if not self._is_alert_channel(post.channel_id):
            log.info(
                "mattermost.post.skipped_non_alert_channel",
                mattermost_post_id=post.id,
                mattermost_channel_id=post.channel_id,
            )
            return None

        if post.user_id == self.settings.mattermost_bot_user_id:
            log.info(
                "mattermost.post.skipped_bot_message",
                mattermost_post_id=post.id,
            )
            return None

        if post.is_system_message:
            log.info(
                "mattermost.post.skipped_system_message",
                mattermost_post_id=post.id,
                post_type=post.post_type,
            )
            return None

        if not self._is_bot_post(post):
            log.info(
                "mattermost.post.skipped_non_bot_alert_message",
                mattermost_post_id=post.id,
                mattermost_user_id=post.user_id,
            )
            return None

        if post.root_id:
            log.info(
                "mattermost.post.skipped_thread_reply",
                mattermost_post_id=post.id,
                root_post_id=post.root_id,
            )
            return None

        signature = alert_signature(post.message)

        if is_resolved_alert(post.message):
            # A resolved (✅) repost never creates a ticket or Jira issue — it only
            # closes the open episode so the next firing becomes a fresh root.
            resolved_at = datetime_from_mattermost_ms(post.create_at) or backend_now()
            root = self.repository.mark_episode_resolved(signature, post.channel_id, resolved_at)
            log.info(
                "mattermost.episode.resolved",
                mattermost_post_id=post.id,
                alert_signature=signature,
                found=root is not None,
            )
            return None

        channel_name = post.channel_name or await self.mattermost.get_channel_name(post.channel_id)
        message_url = self.mattermost.permalink(post.id)
        ticket, created, root = self.repository.create_or_classify_alert(
            post, message_url=message_url, channel_name=channel_name, signature=signature
        )
        log.info(
            "mattermost.alert.received",
            mattermost_post_id=post.id,
            created=created,
            is_repeat=root is not None,
        )

        if root is None and not created and ticket.jira_issue_key:
            log.info(
                "jira.issue.skipped_existing_mapping",
                mattermost_post_id=post.id,
                jira_issue_key=ticket.jira_issue_key,
            )
            return ticket

        await self._ensure_jira_issue(ticket, is_repeat=root is not None)
        if root is not None:
            ticket = self.repository.get_by_post_id(post.id) or ticket
            await self._handle_expected_repeat(ticket, root)
        ticket = self.repository.get_by_post_id(post.id)
        if ticket and ticket.confirmation_status in {
            "pending_confirmation",
            "failed_confirmation",
            "confirming",
        }:
            user_id = ticket.pending_confirmation_by_user_id or ticket.confirmed_by_user_id
            if user_id:
                await self.confirm_incident(
                    post.id,
                    confirmed_by_user_id=user_id,
                    confirmed_at=ticket.pending_confirmation_at,
                    source="pending_confirmation",
                )
        return self.repository.get_by_post_id(post.id)

    async def apply_validity_label(
        self,
        post_id: str,
        *,
        validity_label: str,
        validity_set_at: datetime | None = None,
        source: str,
    ) -> ConfirmationResult:
        """Lightweight path: set Jira "Валидность" and reply in the alert thread.

        Unlike :meth:`confirm_incident`, this does not post to the incidents
        channel, add a comment, or transition the issue. The last reaction wins:
        each distinct label overwrites the Jira field. ``validity_label`` on the
        ticket guards against re-applying the same label (no duplicate replies).
        """
        ticket = self.repository.get_by_post_id(post_id)
        if ticket is None or ticket.jira_issue_key is None:
            log.warning(
                "incident.validity.jira_not_ready",
                mattermost_post_id=post_id,
                validity_label=validity_label,
                source=source,
            )
            return ConfirmationResult(
                status=ConfirmationStatus.PENDING_JIRA,
                message="Validity update is skipped: the Jira issue is not ready yet.",
            )

        if ticket.validity_label == validity_label:
            log.info(
                "incident.validity.skipped_unchanged",
                mattermost_post_id=post_id,
                jira_issue_key=ticket.jira_issue_key,
                validity_label=validity_label,
            )
            return ConfirmationResult(
                status=ConfirmationStatus.VALIDITY_SET,
                message=f"Validity is already set to {validity_label}.",
                jira_issue_url=ticket.jira_issue_url,
            )

        try:
            await self.jira.set_validity(
                ticket.jira_issue_key,
                validity_label,
                ended_at=validity_set_at,
            )
        except ApiError as exc:
            self.repository.set_last_error(post_id, str(exc))
            log.error(
                "incident.validity.failed",
                mattermost_post_id=post_id,
                jira_issue_key=ticket.jira_issue_key,
                validity_label=validity_label,
                error=str(exc),
            )
            return ConfirmationResult(
                status=ConfirmationStatus.ERROR,
                message="Validity update failed; please retry.",
                jira_issue_url=ticket.jira_issue_url,
            )

        self.repository.set_validity_label(post_id, validity_label)
        await self._set_time_to_fix(ticket.jira_issue_key, ticket, validity_set_at or backend_now())
        log.info(
            "incident.validity.updated",
            mattermost_post_id=post_id,
            jira_issue_key=ticket.jira_issue_key,
            validity_label=validity_label,
            source=source,
        )
        await self._post_alert_thread_reply(
            post_id,
            channel_id=ticket.mattermost_channel_id,
            message=format_thread_validity_changed(validity_label=validity_label),
            event="mattermost.alert_thread.validity_notice_published",
            props={
                "jira_issue_key": ticket.jira_issue_key,
                "validity_label": validity_label,
            },
        )
        if self.settings.read_only_mode:
            # Surface the Jira fields the shadow computed but did not write (validity
            # + Time-to-Fix) as a code block, so the audit thread shows the would-be
            # parameters instead of dropping them into the no-op.
            await self._post_alert_thread_reply(
                post_id,
                channel_id=ticket.mattermost_channel_id,
                message=format_readonly_jira_params(
                    jira_issue_key=ticket.jira_issue_key,
                    start=ticket.mattermost_message_created_at,
                    ended_at=None,
                    ttf_minutes=incident_ttf_minutes(
                        ticket.mattermost_message_created_at, validity_set_at or backend_now()
                    ),
                    validity_label=validity_label,
                ),
                event="readonly.alert_params_published",
            )
        return ConfirmationResult(
            status=ConfirmationStatus.VALIDITY_SET,
            message=f"Validity set to {validity_label}.",
            jira_issue_url=ticket.jira_issue_url,
        )

    async def _alert_attachments(self, ticket: AlertTicket) -> list[dict]:
        try:
            post = await self.mattermost.get_post(ticket.mattermost_post_id)
        except ApiError as exc:
            log.warning(
                "mattermost.incident_message.alert_lookup_failed",
                mattermost_post_id=ticket.mattermost_post_id,
                error=str(exc),
            )
            return []
        return _copy_post_attachments(post)
