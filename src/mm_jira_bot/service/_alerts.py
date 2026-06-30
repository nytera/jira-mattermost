"""Алерты: AlertMixin.

Полный жизненный цикл алерта в alert-канале: первичная обработка поста (создание
Jira-задачи через JiraSync / обработка повторов), интерактивные кнопки и меню
(`handle_alert_action`), feedback-диалог, выставление валидности (`apply_validity_label`)
и сбор вложений исходного поста. Методы вызываются собранным `IncidentBotService`
(см. `coordinator.py`); state (`settings`/`repository`/`mattermost`/`jira`) ставит
конструктор координатора, а инцидент-механика, Jira-проводка, постмортем и summary
живут в sibling-классах.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING, Any

from mm_jira_bot.actions import (
    ACTION_EXPECTED,
    ACTION_FALSE,
    ACTION_FEEDBACK,
    ACTION_INCIDENT,
    ACTION_SOURCE_INCIDENT,
    ACTION_SUMMARY,
    ACTION_VALID,
    ACTION_VALIDITY,
    build_alert_controls_attachment,
    build_alert_feedback_attachment,
    feedback_dialog_callback_url,
)
from mm_jira_bot.domain import (
    ConfirmationResult,
    ConfirmationStatus,
    JiraIssue,
    MattermostPost,
    backend_now,
    datetime_from_mattermost_ms,
)
from mm_jira_bot.formatting import (
    alert_signature,
    format_thread_validity_changed,
    is_resolved_alert,
)
from mm_jira_bot.jira import (
    VALID_INCIDENT_CONFIRMED_VALUE,
    VALID_INCIDENT_EXPECTED_VALUE,
    VALID_INCIDENT_FALSE_VALUE,
)
from mm_jira_bot.logging import get_logger
from mm_jira_bot.repository import AlertTicket, AlertTicketRepository
from mm_jira_bot.retry import ApiError
from mm_jira_bot.service._shared import ActionResult, _validity_action_message

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


def _incident_action_message(result: ConfirmationResult) -> str:
    return {
        ConfirmationStatus.CONFIRMED: "Инцидент заведён ✅",
        ConfirmationStatus.ALREADY_CONFIRMED: "Инцидент уже подтверждён.",
        ConfirmationStatus.PENDING_JIRA: ("Подтверждение сохранено — задача Jira ещё создаётся."),
        ConfirmationStatus.ERROR: ("Произошла ошибка при подтверждении, бот повторит позже."),
        ConfirmationStatus.NOT_FOUND: ("Не нашёл связку с Jira для этого сообщения."),
    }.get(result.status, result.message)


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

        def _is_authorized(self, user_id: str) -> bool: ...

        def _interactive_controls_enabled(self) -> bool: ...

        def _action_callback_url(self) -> str: ...

        async def _resolve_user_display(self, user_id: str) -> str: ...

        async def _post_unauthorized_notice(
            self, *, root_post_id: str, channel_id: str, user_mention: str
        ) -> None: ...

        # --- JiraSyncMixin ---
        async def _ensure_jira_issue(self, ticket: AlertTicket, is_repeat: bool = ...) -> None: ...

        async def _handle_expected_repeat(self, ticket: AlertTicket, root: AlertTicket) -> None: ...

        def _display_jira_issue(self, issue: JiraIssue) -> JiraIssue: ...

        # --- IncidentMixin ---
        async def handle_incident_action(
            self,
            *,
            action: str,
            incident_post_id: str,
            user_id: str,
            selected_option: str = ...,
        ) -> ActionResult: ...

        async def confirm_incident(
            self,
            post_id: str,
            *,
            confirmed_by_user_id: str,
            source: str,
            confirmed_at: datetime | None = ...,
        ) -> ConfirmationResult: ...

        # --- ThreadSummaryMixin ---
        async def generate_thread_summary(
            self, alert_post: MattermostPost, *, requested_by_user_id: str, source: str
        ) -> ActionResult: ...

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

    def _alert_action_attachments(
        self,
        alert_post_id: str,
        *,
        title: str | None = None,
        title_link: str | None = None,
        confirmed: bool = False,
    ) -> list[dict] | None:
        """Alert thread attachments for a single reply, or ``None`` if disabled.

        Returns two stacked blocks in one reply: a blue main block with the
        "Создана задача" notice, the validity menu, and the incident/summary
        buttons under it, then a gray feedback block below. Interactive controls
        need an absolute callback URL and may be turned off entirely, so they are
        attached only when ``SERVICE_PUBLIC_URL`` is configured and
        ``INTERACTIVE_BUTTONS_ENABLED`` is not ``false``. Emoji reactions remain
        the fallback.
        """
        if not self._interactive_controls_enabled():
            return None
        callback_url = self._action_callback_url()
        return [
            build_alert_controls_attachment(
                title=title or "Jira",
                title_link=title_link,
                alert_post_id=alert_post_id,
                callback_url=callback_url,
                confirmed=confirmed,
            ),
            build_alert_feedback_attachment(
                alert_post_id=alert_post_id,
                callback_url=callback_url,
            ),
        ]

    async def handle_alert_action(
        self,
        *,
        action: str,
        alert_post_id: str,
        user_id: str,
        user_name: str = "",
        channel_id: str = "",
        selected_option: str = "",
        trigger_id: str = "",
        source: str = "alert",
        incident_post_id: str = "",
    ) -> ActionResult:
        acted_post_id = incident_post_id if source == ACTION_SOURCE_INCIDENT else alert_post_id
        log.info(
            "mattermost.action.received",
            action=action,
            selected_option=selected_option,
            trigger_id=trigger_id,
            mattermost_post_id=acted_post_id,
            source=source,
            user_id=user_id,
        )
        # Feedback is open to everyone; all other actions require authorization.
        if action != ACTION_FEEDBACK and not self._is_authorized(user_id):
            log.info(
                "mattermost.action.skipped_unauthorized",
                action=action,
                source=source,
                user_id=user_id,
            )
            await self._post_unauthorized_notice(
                root_post_id=acted_post_id,
                channel_id=channel_id,
                user_mention=f"@{user_name}" if user_name else user_id,
            )
            return ActionResult(message="")
        if source == ACTION_SOURCE_INCIDENT:
            if not incident_post_id:
                return ActionResult(message="Не указан инцидент для действия.")
            return await self.handle_incident_action(
                action=action,
                incident_post_id=incident_post_id,
                user_id=user_id,
                selected_option=selected_option,
            )
        if not alert_post_id:
            return ActionResult(message="Не указан алерт для действия.")
        try:
            post = await self.mattermost.get_post(alert_post_id)
        except ApiError as exc:
            log.error(
                "mattermost.action.post_lookup_failed",
                mattermost_post_id=alert_post_id,
                action=action,
                error=str(exc),
            )
            return ActionResult(message="Не удалось прочитать сообщение алерта.")

        if action == ACTION_SUMMARY:
            return await self.generate_thread_summary(
                post, requested_by_user_id=user_id, source="action"
            )

        if not self._is_alert_channel(post.channel_id):
            return ActionResult(message="Сообщение не в канале алертов.")

        if action == ACTION_FEEDBACK:
            return await self.open_feedback_dialog(
                alert_post_id=alert_post_id,
                trigger_id=trigger_id,
            )

        ticket = self.repository.get_by_post_id(alert_post_id)
        if ticket is None or ticket.jira_issue_key is None:
            await self.handle_alert_post(post)

        if action == ACTION_INCIDENT:
            result = await self.confirm_incident(
                alert_post_id, confirmed_by_user_id=user_id, source="action"
            )
            update_attachments = None
            if result.status in (
                ConfirmationStatus.CONFIRMED,
                ConfirmationStatus.ALREADY_CONFIRMED,
            ):
                ticket = self.repository.get_by_post_id(alert_post_id)
                if ticket is not None and ticket.jira_issue_key is not None:
                    display = self._display_jira_issue(
                        JiraIssue(key=ticket.jira_issue_key, url=ticket.jira_issue_url or "")
                    )
                    update_attachments = self._alert_action_attachments(
                        alert_post_id,
                        title=display.key,
                        title_link=display.url or None,
                        confirmed=True,
                    )
            return ActionResult(
                message=_incident_action_message(result),
                update_attachments=update_attachments,
            )

        if action == ACTION_VALIDITY:
            if not selected_option:
                return ActionResult(message="Не выбрана «Валидность».")
            action = selected_option

        validity_label = {
            ACTION_VALID: VALID_INCIDENT_CONFIRMED_VALUE,
            ACTION_FALSE: VALID_INCIDENT_FALSE_VALUE,
            ACTION_EXPECTED: VALID_INCIDENT_EXPECTED_VALUE,
        }.get(action)
        if validity_label is None:
            log.info(
                "mattermost.action.unknown",
                action=action,
                mattermost_post_id=alert_post_id,
            )
            return ActionResult(message="Неизвестное действие.")

        result = await self.apply_validity_label(
            alert_post_id, validity_label=validity_label, source="action"
        )
        return ActionResult(message=_validity_action_message(result, validity_label))

    async def open_feedback_dialog(
        self,
        *,
        alert_post_id: str,
        trigger_id: str,
    ) -> ActionResult:
        if not trigger_id:
            return ActionResult(message="Не удалось открыть форму: нет trigger_id.")
        service_public_url = self.settings.service_public_url
        if not service_public_url:
            return ActionResult(message="Не удалось открыть форму: не настроен SERVICE_PUBLIC_URL.")
        state = json.dumps({"alert_post_id": alert_post_id}, ensure_ascii=False)
        dialog = {
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
            "state": state,
        }
        try:
            await self.mattermost.open_dialog(
                trigger_id=trigger_id,
                url=feedback_dialog_callback_url(service_public_url),
                dialog=dialog,
            )
        except ApiError as exc:
            log.error(
                "mattermost.feedback_dialog.open_failed",
                mattermost_post_id=alert_post_id,
                error=str(exc),
            )
            return ActionResult(message="Не удалось открыть форму обратной связи.")
        return ActionResult(message="Открыта форма обратной связи.")

    async def handle_feedback_dialog_submission(
        self,
        *,
        user_id: str,
        state: str,
        submission: dict,
        cancelled: bool = False,
    ) -> ActionResult:
        if cancelled:
            return ActionResult(message="")
        try:
            data = json.loads(state or "{}")
        except json.JSONDecodeError:
            data = {}
        alert_post_id = str(data.get("alert_post_id") or "")
        if not alert_post_id:
            return ActionResult(message="Не указан алерт для обратной связи.")
        feedback = str(submission.get("feedback") or "").strip()
        if not feedback:
            return ActionResult(message="Обратная связь пустая.")
        user_display = await self._resolve_user_display(user_id)
        try:
            ticket = self.repository.get_by_post_id(alert_post_id)
            if ticket is None:
                return ActionResult(message="Не нашёл связку алерта.")
            self.repository.add_feedback(
                alert_post_id,
                user_id=user_id,
                user_display_name=user_display,
                message=feedback,
            )
            log.info(
                "feedback.received",
                mattermost_post_id=alert_post_id,
                user_id=user_id,
            )
            await self._post_alert_thread_reply(
                alert_post_id,
                channel_id=ticket.mattermost_channel_id,
                message=f"Получили обратную связь от {user_display}",
                event="mattermost.alert_thread.feedback_received_published",
                props={"feedback_user_id": user_id},
            )
        except ApiError as exc:
            log.error(
                "mattermost.feedback_dialog.submit_failed",
                mattermost_post_id=alert_post_id,
                error=str(exc),
            )
            return ActionResult(message="Не удалось обработать обратную связь.")
        return ActionResult(message="")

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
