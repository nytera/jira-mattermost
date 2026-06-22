from __future__ import annotations

import json
import re
import secrets
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

from mm_jira_bot.actions import (
    ACTION_CREATE_TASK,
    ACTION_END_INCIDENT,
    ACTION_EXPECTED,
    ACTION_FALSE,
    ACTION_FEEDBACK,
    ACTION_INCIDENT,
    ACTION_SOURCE_INCIDENT,
    ACTION_SUMMARY,
    ACTION_VALID,
    ACTION_VALIDITY,
    alert_action_callback_url,
    build_alert_controls_attachment,
    build_alert_feedback_attachment,
    build_incident_controls_attachment,
    build_incident_create_attachment,
    feedback_dialog_callback_url,
)
from mm_jira_bot.config import Settings
from mm_jira_bot.domain import (
    ConfirmationResult,
    ConfirmationStatus,
    JiraIssue,
    MattermostPost,
    ReactionEvent,
    backend_now,
    datetime_from_mattermost_ms,
)
from mm_jira_bot.formatting import (
    extract_alert_title,
    format_incident_message,
    format_thread_issue_created,
    format_thread_status_changed,
    format_thread_validity_changed,
    is_resolved_alert,
    mark_incident_message_completed,
)
from mm_jira_bot.jira import (
    VALID_INCIDENT_CONFIRMED_VALUE,
    VALID_INCIDENT_EXPECTED_VALUE,
    VALID_INCIDENT_FALSE_VALUE,
)
from mm_jira_bot.jira_payload import build_postmortem_description
from mm_jira_bot.logging import get_logger
from mm_jira_bot.mattermost import parse_posted_event, parse_reaction_event
from mm_jira_bot.postmortem import (
    ThreadMessage,
    build_postmortem_comment,
    build_postmortem_prompt,
    extract_postmortem_summary,
    format_postmortem_thread_reply,
    format_thread_transcript,
)
from mm_jira_bot.repository import AlertTicket, AlertTicketRepository, ticket_to_post
from mm_jira_bot.retry import ApiError
from mm_jira_bot.summary import (
    build_thread_summary_prompt,
    format_thread_summary_reply,
)

log = get_logger(__name__)

POST_ID_PATTERN = re.compile(r"(?:^|/)(?:_redirect/)?pl/([a-z0-9]{20,32})(?:$|[/?#])")
BARE_POST_ID_PATTERN = re.compile(r"^[a-z0-9]{20,32}$")
INCIDENT_END_REACTION_NAMES = {
    "white_check_mark",
    "heavy_check_mark",
    "ballot_box_with_check",
}


def _copy_post_attachments(post: MattermostPost) -> list[dict]:
    props = post.props
    if not isinstance(props, dict):
        return []
    attachments = props.get("attachments")
    if not isinstance(attachments, list):
        return []
    return [deepcopy(attachment) for attachment in attachments if isinstance(attachment, dict)]


@dataclass(frozen=True)
class CommandResponse:
    text: str
    response_type: str = "ephemeral"


@dataclass(frozen=True)
class ActionResult:
    """Ephemeral feedback shown to the user who clicked an alert button.

    ``update_attachments``, when set, replaces the originating post's attachments
    via the Mattermost interactive-action ``update`` response (used to swap the
    "Создать задачу" prompt for the full controls card after task creation).
    """

    message: str
    update_attachments: list[dict] | None = None


def _validity_action_message(result: ConfirmationResult, validity_label: str) -> str:
    if result.status == ConfirmationStatus.VALIDITY_SET:
        return f"Готово: «Валидность» = {validity_label}."
    if result.status == ConfirmationStatus.PENDING_JIRA:
        return "Задача Jira ещё создаётся — обновлю «Валидность» автоматически."
    if result.status == ConfirmationStatus.ERROR:
        return "Не удалось обновить «Валидность», попробуйте ещё раз."
    return result.message


def _incident_action_message(result: ConfirmationResult) -> str:
    return {
        ConfirmationStatus.CONFIRMED: "Инцидент заведён ✅",
        ConfirmationStatus.ALREADY_CONFIRMED: "Инцидент уже подтверждён.",
        ConfirmationStatus.PENDING_JIRA: ("Подтверждение сохранено — задача Jira ещё создаётся."),
        ConfirmationStatus.ERROR: ("Произошла ошибка при подтверждении, бот повторит позже."),
        ConfirmationStatus.NOT_FOUND: ("Не нашёл связку с Jira для этого сообщения."),
    }.get(result.status, result.message)


def _incident_end_message(result: ConfirmationResult) -> str:
    if result.status == ConfirmationStatus.INCIDENT_ENDED:
        return "Инцидент завершён 🏁"
    if result.status == ConfirmationStatus.ERROR:
        return "Не удалось завершить инцидент, попробуйте ещё раз."
    return result.message


@dataclass(frozen=True)
class DebugCreateFromLinkResult:
    ok: bool
    status: str
    message: str
    mattermost_post_id: str | None = None
    jira_issue_key: str | None = None
    jira_issue_url: str | None = None


@dataclass(frozen=True)
class DebugJiraRecreateResult:
    ok: bool
    status: str
    message: str
    mattermost_post_id: str
    jira_issue_key: str | None = None
    jira_issue_url: str | None = None
    previous_jira_issue_key: str | None = None
    previous_jira_issue_url: str | None = None


def parse_post_id_from_text(text: str) -> str | None:
    text = text.strip()
    if BARE_POST_ID_PATTERN.fullmatch(text):
        return text
    match = POST_ID_PATTERN.search(text)
    if match:
        return match.group(1)
    return None


class IncidentBotService:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: AlertTicketRepository,
        mattermost_client,
        jira_client,
        llm_client=None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.mattermost = mattermost_client
        self.jira = jira_client
        self.llm = llm_client
        # Allowlist of user ids whose reactions/clicks the bot acts on. Empty +
        # disabled => act on everyone (backward compatible). Resolved from
        # MATTERMOST_AUTHORIZED_USERNAMES at startup via resolve_authorized_users.
        self._authorized_user_ids: frozenset[str] = frozenset()
        self._authorization_enforced: bool = False

    async def resolve_authorized_users(self) -> None:
        """Resolve configured usernames to ids and enable the allowlist gate.

        No usernames configured -> the gate stays disabled (act on everyone).
        Partial resolution (a typo'd login) is logged loudly so the operator
        sees who was dropped instead of silently locking them out. Total
        resolution failure is fail-open (loud warning): the action endpoint
        already relies on network isolation as the real boundary, so bricking
        incident tooling during a Mattermost hiccup is worse than briefly not
        enforcing a 5-person filter.
        """
        usernames = list(self.settings.mattermost_authorized_usernames)
        if not usernames:
            log.info("authorized_users.disabled")
            return
        try:
            resolved = await self.mattermost.get_user_ids_by_usernames(usernames)
        except ApiError as exc:
            self._authorized_user_ids = frozenset()
            self._authorization_enforced = False
            log.warning(
                "authorized_users.resolve_failed_fail_open",
                requested=usernames,
                error=str(exc),
            )
            return
        unresolved = [name for name in usernames if name not in resolved]
        if unresolved:
            log.warning(
                "authorized_users.unresolved",
                unresolved=unresolved,
                resolved=sorted(resolved),
            )
        self._authorized_user_ids = frozenset(resolved.values())
        self._authorization_enforced = True
        log.info(
            "authorized_users.enabled",
            resolved_count=len(self._authorized_user_ids),
            resolved=sorted(resolved),
        )

    def _is_authorized(self, user_id: str) -> bool:
        return not self._authorization_enforced or user_id in self._authorized_user_ids

    async def handle_websocket_event(self, event: dict) -> None:
        posted = parse_posted_event(event)
        if posted:
            if posted.channel_id == self.settings.mattermost_incident_channel_id:
                await self.handle_manual_incident_post(posted)
            else:
                await self.handle_alert_post(posted)
            return

        reaction = parse_reaction_event(event)
        if reaction:
            await self.handle_reaction(reaction)

    def _is_bot_post(self, post: MattermostPost) -> bool:
        """Posts authored by our bot or by any integration/webhook.

        Mattermost marks bot-account posts with ``props.from_bot`` and incoming
        webhook posts with ``props.from_webhook`` (both string ``"true"``); we
        also exclude our own bot user id.
        """
        if post.user_id == self.settings.mattermost_bot_user_id:
            return True
        props = post.props or {}
        return props.get("from_bot") == "true" or props.get("from_webhook") == "true"

    async def handle_manual_incident_post(self, post: MattermostPost) -> None:
        """A human's root post in the incident channel: offer a "Создать задачу" card.

        Only root posts from real users (no bots/webhooks) qualify. The Jira
        issue is not created here — it is created when someone clicks the button.
        The controls need an absolute callback URL, so without SERVICE_PUBLIC_URL
        we do nothing and leave the checkmark flow as the fallback. Idempotent:
        the controls reply is posted once, guarded by the unique ticket row.
        """
        if post.channel_id != self.settings.mattermost_incident_channel_id:
            return
        if post.root_id:  # only channel root posts, not thread replies
            return
        if self._is_bot_post(post):
            return
        if not self.settings.service_public_url:
            return
        channel_name = post.channel_name or await self.mattermost.get_channel_name(post.channel_id)
        _ticket, created = self.repository.create_or_get_incident_thread(
            post,
            message_url=self.mattermost.permalink(post.id),
            channel_name=channel_name,
        )
        if not created:
            return
        callback_url = alert_action_callback_url(self.settings.service_public_url)
        await self._post_incident_thread_reply(
            post.id,
            channel_id=post.channel_id,
            # The duty mention goes in the message text (above the card) so the
            # @group ping actually fires — attachment text does not notify.
            message=self.settings.mattermost_duty_mention or "",
            event="mattermost.incident_thread.controls_published",
            props={
                "attachments": [
                    build_incident_create_attachment(
                        incident_post_id=post.id, callback_url=callback_url
                    )
                ]
            },
        )

    async def handle_alert_post(self, post: MattermostPost) -> AlertTicket | None:
        if post.channel_id != self.settings.mattermost_alert_channel_id:
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

        if post.root_id:
            log.info(
                "mattermost.post.skipped_thread_reply",
                mattermost_post_id=post.id,
                root_post_id=post.root_id,
            )
            return None

        if is_resolved_alert(post.message):
            log.info(
                "mattermost.post.skipped_resolved_alert",
                mattermost_post_id=post.id,
            )
            return None

        channel_name = post.channel_name or await self.mattermost.get_channel_name(post.channel_id)
        message_url = self.mattermost.permalink(post.id)
        ticket, created = self.repository.create_or_get_alert(
            post, message_url=message_url, channel_name=channel_name
        )
        log.info(
            "mattermost.alert.received",
            mattermost_post_id=post.id,
            created=created,
        )

        if not created and ticket.jira_issue_key:
            log.info(
                "jira.issue.skipped_existing_mapping",
                mattermost_post_id=post.id,
                jira_issue_key=ticket.jira_issue_key,
            )
            return ticket

        await self._ensure_jira_issue(ticket)
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

    def _validity_label_for_emoji(self, emoji_name: str) -> str | None:
        """Map the two lightweight reactions to their "Валидность" option."""
        if emoji_name == self.settings.mattermost_false_incident_reaction_name:
            return VALID_INCIDENT_FALSE_VALUE
        if emoji_name == self.settings.mattermost_expected_incident_reaction_name:
            return VALID_INCIDENT_EXPECTED_VALUE
        return None

    async def handle_reaction(self, reaction: ReactionEvent) -> ConfirmationResult:
        log.info(
            "mattermost.reaction.received",
            mattermost_post_id=reaction.post_id,
            emoji_name=reaction.emoji_name,
            user_id=reaction.user_id,
        )
        is_incident = reaction.emoji_name == self.settings.mattermost_incident_reaction_name
        validity_label = (
            None if is_incident else self._validity_label_for_emoji(reaction.emoji_name)
        )
        is_incident_end = reaction.emoji_name in INCIDENT_END_REACTION_NAMES
        if not is_incident and validity_label is None and not is_incident_end:
            return ConfirmationResult(
                status=ConfirmationStatus.IGNORED,
                message="Reaction ignored: not configured incident reaction.",
            )

        if not self._is_authorized(reaction.user_id):
            log.info(
                "mattermost.reaction.skipped_unauthorized",
                mattermost_post_id=reaction.post_id,
                user_id=reaction.user_id,
                emoji_name=reaction.emoji_name,
            )
            return ConfirmationResult(
                status=ConfirmationStatus.IGNORED,
                message="Reaction ignored: user is not authorized.",
            )

        post = await self.mattermost.get_post(reaction.post_id)
        if is_incident_end and post.channel_id == self.settings.mattermost_incident_channel_id:
            return await self.handle_incident_checkmark(
                post,
                reacted_by_user_id=reaction.user_id,
                ended_at=datetime_from_mattermost_ms(reaction.create_at) or backend_now(),
                source="reaction",
            )

        if post.channel_id != self.settings.mattermost_alert_channel_id:
            log.info(
                "mattermost.reaction.skipped_non_alert_channel",
                mattermost_post_id=reaction.post_id,
                mattermost_channel_id=post.channel_id,
            )
            return ConfirmationResult(
                status=ConfirmationStatus.IGNORED,
                message="Reaction ignored: post is not in alert channel.",
            )

        ticket = self.repository.get_by_post_id(reaction.post_id)
        if ticket is None or ticket.jira_issue_key is None:
            await self.handle_alert_post(post)

        if is_incident:
            return await self.confirm_incident(
                reaction.post_id,
                confirmed_by_user_id=reaction.user_id,
                confirmed_at=datetime_from_mattermost_ms(reaction.create_at),
                source="reaction",
            )

        if validity_label is None:
            return ConfirmationResult(
                status=ConfirmationStatus.IGNORED,
                message="Reaction ignored: checkmark is only handled in incident threads.",
            )

        return await self.apply_validity_label(
            reaction.post_id,
            validity_label=validity_label,
            validity_set_at=datetime_from_mattermost_ms(reaction.create_at),
            source="reaction",
        )

    def _alert_action_attachments(
        self,
        alert_post_id: str,
        *,
        title: str | None = None,
        title_link: str | None = None,
    ) -> list[dict] | None:
        """Alert thread attachments for a single reply, or ``None`` if disabled.

        Returns two stacked blocks in one reply: a blue main block with the
        "Создана задача" notice, the validity menu, and the incident/summary
        buttons under it, then a gray feedback block below. Interactive controls
        need an absolute callback URL, so they are attached when
        ``SERVICE_PUBLIC_URL`` is configured. Emoji reactions remain the fallback.
        """
        if not self.settings.service_public_url:
            return None
        callback_url = alert_action_callback_url(self.settings.service_public_url)
        return [
            build_alert_controls_attachment(
                title=title or "Jira",
                title_link=title_link,
                alert_post_id=alert_post_id,
                callback_url=callback_url,
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
            return ActionResult(message="Недостаточно прав для этого действия.")
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

        if post.channel_id != self.settings.mattermost_alert_channel_id:
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
            return ActionResult(message=_incident_action_message(result))

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

    async def handle_incident_action(
        self,
        *,
        action: str,
        incident_post_id: str,
        user_id: str,
        selected_option: str = "",
    ) -> ActionResult:
        """Dispatch a click from the manual-incident card (incident channel).

        Keyed by the incident root post id (the manual ticket's own
        ``mattermost_post_id``), so it never touches the alert-channel paths.
        """
        if action == ACTION_CREATE_TASK:
            return await self._incident_create_task(incident_post_id, user_id=user_id)

        try:
            post = await self.mattermost.get_post(incident_post_id)
        except ApiError as exc:
            log.error(
                "mattermost.incident_action.post_lookup_failed",
                mattermost_post_id=incident_post_id,
                action=action,
                error=str(exc),
            )
            return ActionResult(message="Не удалось прочитать сообщение инцидента.")

        if action == ACTION_SUMMARY:
            return await self.generate_thread_summary(
                post, requested_by_user_id=user_id, source="incident_action"
            )

        if action == ACTION_END_INCIDENT:
            result = await self.handle_incident_checkmark(
                post,
                reacted_by_user_id=user_id,
                ended_at=backend_now(),
                source="incident_button",
            )
            return ActionResult(message=_incident_end_message(result))

        if action == ACTION_VALIDITY:
            validity_label = {
                ACTION_VALID: VALID_INCIDENT_CONFIRMED_VALUE,
                ACTION_FALSE: VALID_INCIDENT_FALSE_VALUE,
                ACTION_EXPECTED: VALID_INCIDENT_EXPECTED_VALUE,
            }.get(selected_option)
            if validity_label is None:
                return ActionResult(message="Не выбрана «Валидность».")
            # apply_validity_label is keyed by the ticket's mattermost_post_id. For
            # a manual incident that equals the incident post id; for an
            # alert-originated incident it is the alert post id, so resolve it.
            ticket = self.repository.get_by_incident_post_id(incident_post_id)
            post_key = ticket.mattermost_post_id if ticket is not None else incident_post_id
            result = await self.apply_validity_label(
                post_key, validity_label=validity_label, source="incident_action"
            )
            return ActionResult(message=_validity_action_message(result, validity_label))

        log.info(
            "mattermost.incident_action.unknown",
            action=action,
            mattermost_post_id=incident_post_id,
        )
        return ActionResult(message="Неизвестное действие.")

    async def _incident_create_task(self, incident_post_id: str, *, user_id: str) -> ActionResult:
        ticket = self.repository.get_by_incident_post_id(incident_post_id)
        if ticket is None:
            try:
                post = await self.mattermost.get_post(incident_post_id)
            except ApiError:
                return ActionResult(message="Не удалось прочитать сообщение инцидента.")
            channel_name = post.channel_name or await self.mattermost.get_channel_name(
                post.channel_id
            )
            ticket, _ = self.repository.create_or_get_incident_thread(
                post,
                message_url=self.mattermost.permalink(post.id),
                channel_name=channel_name,
            )

        if ticket.jira_issue_key is None:
            summary = (
                ticket.mattermost_alert_title
                or extract_alert_title(ticket.mattermost_message_text or "")
                or "Инцидент"
            )
            description = (
                "Инцидент заведён вручную из Mattermost.\n\n"
                f"Исходное сообщение: {ticket.mattermost_message_url}"
            )
            try:
                issue = await self.jira.create_postmortem_issue(
                    ticket_to_post(ticket),
                    message_url=ticket.mattermost_message_url,
                    channel_name=ticket.mattermost_channel_name,
                    summary=summary,
                    description=description,
                )
            except ApiError as exc:
                self.repository.set_last_error(ticket.mattermost_post_id, str(exc))
                log.error(
                    "incident.create_task.failed",
                    mattermost_post_id=incident_post_id,
                    error=str(exc),
                )
                return ActionResult(message="Не удалось создать задачу, попробуйте ещё раз.")
            self.repository.attach_jira_issue(ticket.mattermost_post_id, issue.key, issue.url)
            ticket = self.repository.get_by_post_id(ticket.mattermost_post_id) or ticket

        callback_url = alert_action_callback_url(self.settings.service_public_url)
        attachment = build_incident_controls_attachment(
            incident_post_id=incident_post_id,
            callback_url=callback_url,
        )
        return ActionResult(
            message=f"Создана задача {ticket.jira_issue_key}.",
            update_attachments=[attachment],
        )

    async def open_feedback_dialog(
        self,
        *,
        alert_post_id: str,
        trigger_id: str,
    ) -> ActionResult:
        if not trigger_id:
            return ActionResult(message="Не удалось открыть форму: нет trigger_id.")
        if not self.settings.service_public_url:
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
                url=feedback_dialog_callback_url(self.settings.service_public_url),
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

    async def generate_thread_summary(
        self,
        alert_post: MattermostPost,
        *,
        requested_by_user_id: str,
        source: str,
    ) -> ActionResult:
        if self.llm is None:
            log.info(
                "summary.skipped_llm_not_configured",
                mattermost_post_id=alert_post.id,
            )
            return ActionResult(message="Саммари недоступно: LLM не настроен.")

        root_id = alert_post.root_id or alert_post.id
        root_post = alert_post
        if root_id != alert_post.id:
            try:
                root_post = await self.mattermost.get_post(root_id)
            except ApiError:
                root_post = alert_post

        try:
            thread_messages, participants, _ = await self._postmortem_thread_context(
                root_post,
                reacted_by_user_id=requested_by_user_id,
            )
            transcript = format_thread_transcript(thread_messages)
            prompt = build_thread_summary_prompt(
                thread_url=self.mattermost.permalink(root_post.id),
                participants=participants,
                transcript=transcript,
                max_chars=self.settings.llm_thread_max_chars,
            )
            summary = await self.llm.generate_summary(prompt)
        except ApiError as exc:
            log.error(
                "summary.failed",
                mattermost_post_id=root_post.id,
                source=source,
                error=str(exc),
            )
            return ActionResult(message="Не удалось сгенерировать саммари, попробуйте позже.")

        await self._post_alert_thread_reply(
            root_post.id,
            channel_id=root_post.channel_id,
            message=format_thread_summary_reply(summary),
            event="mattermost.alert_thread.summary_published",
            props={"summary_requested_by_user_id": requested_by_user_id},
        )
        log.info(
            "summary.completed",
            mattermost_post_id=root_post.id,
            source=source,
        )
        return ActionResult(message="Саммари опубликовано в треде.")

    async def handle_incident_checkmark(
        self,
        post: MattermostPost,
        *,
        reacted_by_user_id: str,
        ended_at: datetime,
        source: str,
    ) -> ConfirmationResult:
        if post.root_id:
            log.info(
                "incident.checkmark.skipped_thread_reply",
                mattermost_post_id=post.id,
                root_post_id=post.root_id,
                reacted_by_user_id=reacted_by_user_id,
                source=source,
            )
            return ConfirmationResult(
                status=ConfirmationStatus.IGNORED,
                message="Reaction ignored: checkmarks on incident thread replies are ignored.",
            )

        ticket = self.repository.get_by_incident_post_id(post.id)
        end_result: ConfirmationResult | None = None
        if ticket is not None and ticket.jira_issue_key is not None:
            end_result = await self.apply_incident_end_time(
                post,
                ended_at=ended_at,
                source=source,
            )
            if end_result.status == ConfirmationStatus.ERROR:
                return end_result
            ticket = self.repository.get_by_incident_post_id(post.id)

        if self.llm is None:
            if end_result is not None:
                if end_result.status == ConfirmationStatus.INCIDENT_ENDED:
                    await self._mark_incident_post_completed(post.id)
                return end_result
            log.info(
                "postmortem.skipped_llm_not_configured",
                incident_post_id=post.id,
                reacted_post_id=post.id,
            )
            return ConfirmationResult(
                status=ConfirmationStatus.IGNORED,
                message="Reaction ignored: LLM postmortem generation is not configured.",
            )

        result = await self.generate_incident_postmortem(
            post,
            reacted_by_user_id=reacted_by_user_id,
            ended_at=ended_at,
            source=source,
            existing_ticket=ticket,
        )
        if result.status == ConfirmationStatus.INCIDENT_ENDED:
            await self._mark_incident_post_completed(post.id)
        return result

    async def _mark_incident_post_completed(self, incident_post_id: str) -> None:
        """Edit the incident-channel message title to the green "завершён" state.

        Only the bot-authored incident message (alert-originated path) carries
        the title; for a manual incident the "incident post" is the human's own
        message (`incident_post_id == mattermost_post_id`), so it is left alone.
        Best-effort: a failed edit never breaks the end/postmortem flow.
        """
        ticket = self.repository.get_by_incident_post_id(incident_post_id)
        if ticket is None or ticket.incident_post_id is None:
            return
        if ticket.incident_post_id == ticket.mattermost_post_id:
            return
        try:
            post = await self.mattermost.get_post(ticket.incident_post_id)
            new_message = mark_incident_message_completed(post.message)
            if new_message == post.message:
                return
            await self.mattermost.update_post(ticket.incident_post_id, message=new_message)
        except ApiError as exc:
            log.warning(
                "incident.message.complete_update_failed",
                incident_post_id=ticket.incident_post_id,
                error=str(exc),
            )

    async def generate_incident_postmortem(
        self,
        root_post: MattermostPost,
        *,
        reacted_by_user_id: str,
        ended_at: datetime,
        source: str,
        existing_ticket: AlertTicket | None = None,
    ) -> ConfirmationResult:
        incident_thread_url = self.mattermost.permalink(root_post.id)
        ticket = existing_ticket
        try:
            (
                thread_messages,
                participants,
                postmortem_author,
            ) = await self._postmortem_thread_context(
                root_post,
                reacted_by_user_id=reacted_by_user_id,
            )
            transcript = format_thread_transcript(thread_messages)
            prompt = build_postmortem_prompt(
                incident_thread_url=incident_thread_url,
                participants=participants,
                postmortem_author=postmortem_author,
                transcript=transcript,
                max_chars=self.settings.llm_thread_max_chars,
            )
            report = await self.llm.generate_postmortem(prompt)
            summary = extract_postmortem_summary(
                report,
                fallback=extract_alert_title(root_post.message),
            )
            if ticket is None:
                channel_name = root_post.channel_name or await self.mattermost.get_channel_name(
                    root_post.channel_id
                )
                ticket, _ = self.repository.create_or_get_incident_thread(
                    root_post,
                    message_url=incident_thread_url,
                    channel_name=channel_name,
                )
            alert_message_url = (
                ticket.mattermost_message_url
                if ticket.mattermost_message_url != incident_thread_url
                else None
            )
            description = build_postmortem_description(
                incident_message_url=incident_thread_url,
                alert_message_url=alert_message_url,
                postmortem_author=postmortem_author,
                participants=participants,
            )
            ticket = await self._ensure_postmortem_jira_issue(
                ticket,
                summary=summary,
                description=description,
                ended_at=ended_at,
                reacted_by_user_id=reacted_by_user_id,
            )
            assert ticket.jira_issue_key is not None
            await self.jira.set_description(ticket.jira_issue_key, description)
            await self.jira.add_comment(
                ticket.jira_issue_key,
                build_postmortem_comment(
                    report=report,
                    incident_thread_url=incident_thread_url,
                    postmortem_author=postmortem_author,
                ),
            )
        except ApiError as exc:
            if ticket is not None:
                self.repository.mark_postmortem_failed(ticket.mattermost_post_id, str(exc))
            log.error(
                "postmortem.failed",
                incident_post_id=root_post.id,
                reacted_by_user_id=reacted_by_user_id,
                source=source,
                error=str(exc),
            )
            await self._post_incident_thread_reply(
                root_post.id,
                channel_id=root_post.channel_id,
                message=(
                    "Не удалось сгенерировать или отправить постмортем в Jira. "
                    "Можно повторить реакцию позже."
                ),
                event="mattermost.incident_thread.postmortem_failed_notice",
                props={"postmortem_error": str(exc)},
            )
            return ConfirmationResult(
                status=ConfirmationStatus.ERROR,
                message="Postmortem generation failed; please retry.",
            )

        await self._post_incident_thread_reply(
            root_post.id,
            channel_id=root_post.channel_id,
            message=format_postmortem_thread_reply(
                jira_issue_key=ticket.jira_issue_key,
                jira_issue_url=ticket.jira_issue_url,
                report=report,
            ),
            event="mattermost.incident_thread.postmortem_published",
            props={
                "jira_issue_key": ticket.jira_issue_key,
                "postmortem_author_user_id": reacted_by_user_id,
            },
        )
        log.info(
            "postmortem.completed",
            incident_post_id=root_post.id,
            jira_issue_key=ticket.jira_issue_key,
            reacted_by_user_id=reacted_by_user_id,
            source=source,
        )
        return ConfirmationResult(
            status=ConfirmationStatus.INCIDENT_ENDED,
            message=(
                "Incident end time updated and postmortem generated. "
                f"Jira: {ticket.jira_issue_url or ticket.jira_issue_key}."
            ),
            jira_issue_url=ticket.jira_issue_url,
            incident_message_url=incident_thread_url,
        )

    async def _ensure_postmortem_jira_issue(
        self,
        ticket: AlertTicket,
        *,
        summary: str,
        description: str,
        ended_at: datetime,
        reacted_by_user_id: str,
    ) -> AlertTicket:
        if ticket.jira_issue_key is not None:
            if not ticket.valid_incident:
                # Validity and confirmation are independent axes: only default to
                # Валидный when nobody picked a validity. An explicit Ложный/
                # Ожидаемый (validity_label) must survive the postmortem/end step.
                if ticket.validity_label is None:
                    await self.jira.set_valid_incident(ticket.jira_issue_key, True)
                await self.jira.set_end_time(ticket.jira_issue_key, ended_at)
                self.repository.mark_confirmed(
                    ticket.mattermost_post_id,
                    user_id=reacted_by_user_id,
                    confirmed_at=ended_at,
                )
            return self.repository.get_by_post_id(ticket.mattermost_post_id) or ticket

        issue = await self.jira.create_postmortem_issue(
            ticket_to_post(ticket),
            message_url=ticket.mattermost_message_url,
            channel_name=ticket.mattermost_channel_name,
            summary=summary,
            description=description,
        )
        self.repository.attach_jira_issue(
            ticket.mattermost_post_id,
            issue.key,
            issue.url,
        )
        # Default to Валидный only when no explicit validity was chosen.
        if ticket.validity_label is None:
            await self.jira.set_valid_incident(issue.key, True)
        await self.jira.set_end_time(issue.key, ended_at)
        if self.settings.jira_confirmed_status_id:
            try:
                await self.jira.transition_issue(issue.key, self.settings.jira_confirmed_status_id)
            except ApiError as exc:
                log.warning(
                    "jira.issue.transition_failed",
                    mattermost_post_id=ticket.mattermost_post_id,
                    jira_issue_key=issue.key,
                    transition_id=self.settings.jira_confirmed_status_id,
                    error=str(exc),
                )
        self.repository.mark_confirmed(
            ticket.mattermost_post_id,
            user_id=reacted_by_user_id,
            confirmed_at=ended_at,
        )
        updated_ticket = self.repository.get_by_post_id(ticket.mattermost_post_id)
        assert updated_ticket is not None
        return updated_ticket

    async def _postmortem_thread_context(
        self,
        root_post: MattermostPost,
        *,
        reacted_by_user_id: str,
    ) -> tuple[list[ThreadMessage], list[str], str]:
        try:
            posts = await self.mattermost.get_thread_posts(root_post.id)
        except ApiError as exc:
            log.warning(
                "mattermost.incident_thread.fetch_failed",
                incident_post_id=root_post.id,
                error=str(exc),
            )
            posts = []
        if not any(post.id == root_post.id for post in posts):
            posts.insert(0, root_post)

        user_ids: list[str] = []
        for post in posts:
            if post.user_id not in user_ids:
                user_ids.append(post.user_id)
        if reacted_by_user_id not in user_ids:
            user_ids.append(reacted_by_user_id)

        display_by_user_id = {
            user_id: await self._resolve_user_display(user_id) for user_id in user_ids
        }
        thread_messages = [
            ThreadMessage(
                post=post,
                author_display=display_by_user_id.get(post.user_id, post.user_id),
            )
            for post in posts
        ]
        participant_user_ids = [
            user_id for user_id in user_ids if user_id != self.settings.mattermost_bot_user_id
        ]
        participants = [
            display_by_user_id.get(user_id, user_id) for user_id in participant_user_ids
        ]
        postmortem_author = display_by_user_id.get(reacted_by_user_id, reacted_by_user_id)
        return thread_messages, participants, postmortem_author

    async def _post_incident_thread_reply(
        self,
        post_id: str,
        *,
        channel_id: str,
        message: str,
        event: str,
        props: dict | None = None,
    ) -> None:
        thread_props = {"mattermost_incident_post_id": post_id, **(props or {})}
        try:
            reply = await self.mattermost.create_post(
                channel_id=channel_id,
                message=message,
                root_id=post_id,
                props=thread_props,
            )
        except ApiError as exc:
            log.warning(
                "mattermost.incident_thread.reply_failed",
                mattermost_post_id=post_id,
                event_kind=event,
                error=str(exc),
            )
            return
        log.info(
            event,
            mattermost_post_id=post_id,
            reply_post_id=reply.id,
        )

    async def apply_incident_end_time(
        self,
        post: MattermostPost,
        *,
        ended_at: datetime,
        source: str,
    ) -> ConfirmationResult:
        ticket = self.repository.get_by_incident_post_id(post.id)
        if ticket is None:
            log.info(
                "incident.end_time.skipped_unknown_post",
                mattermost_post_id=post.id,
                source=source,
            )
            return ConfirmationResult(
                status=ConfirmationStatus.IGNORED,
                message="Reaction ignored: no incident mapping found for this post.",
            )
        if not ticket.valid_incident or ticket.jira_issue_key is None:
            log.info(
                "incident.end_time.skipped_not_valid",
                mattermost_post_id=ticket.mattermost_post_id,
                incident_post_id=ticket.incident_post_id,
                source=source,
            )
            return ConfirmationResult(
                status=ConfirmationStatus.IGNORED,
                message="Reaction ignored: incident is not confirmed.",
            )

        try:
            await self.jira.set_end_time(ticket.jira_issue_key, ended_at)
        except ApiError as exc:
            self.repository.set_last_error(ticket.mattermost_post_id, str(exc))
            log.error(
                "incident.end_time.failed",
                mattermost_post_id=ticket.mattermost_post_id,
                incident_post_id=ticket.incident_post_id,
                jira_issue_key=ticket.jira_issue_key,
                error=str(exc),
            )
            return ConfirmationResult(
                status=ConfirmationStatus.ERROR,
                message="Incident end time update failed; please retry.",
                jira_issue_url=ticket.jira_issue_url,
                incident_message_url=ticket.incident_message_url,
            )

        log.info(
            "incident.end_time.updated",
            mattermost_post_id=ticket.mattermost_post_id,
            incident_post_id=ticket.incident_post_id,
            jira_issue_key=ticket.jira_issue_key,
            ended_at=ended_at.isoformat(),
            source=source,
        )
        return ConfirmationResult(
            status=ConfirmationStatus.INCIDENT_ENDED,
            message="Incident end time updated.",
            jira_issue_url=ticket.jira_issue_url,
            incident_message_url=ticket.incident_message_url,
        )

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
            message=format_thread_validity_changed(
                validity_label=validity_label,
                jira_issue_key=ticket.jira_issue_key,
                jira_issue_url=ticket.jira_issue_url,
            ),
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

    async def handle_slash_command(self, *, user_id: str, text: str) -> CommandResponse:
        log.info(
            "mattermost.slash_command.received",
            user_id=user_id,
            text=text,
        )
        if not self._is_authorized(user_id):
            log.info("mattermost.slash_command.skipped_unauthorized", user_id=user_id)
            return CommandResponse(text="У вас нет прав на эту команду.")
        post_id = parse_post_id_from_text(text)
        if post_id is None:
            return CommandResponse(
                text=(
                    "Invalid link. Use `/incident <band_message_link>` "
                    "with a Band permalink to an alert message."
                )
            )

        try:
            post = await self.mattermost.get_post(post_id)
        except ApiError as exc:
            log.error(
                "mattermost.slash_command.post_lookup_failed",
                mattermost_post_id=post_id,
                error=str(exc),
            )
            return CommandResponse(text=f"Could not read Band post `{post_id}`.")

        if post.channel_id != self.settings.mattermost_alert_channel_id:
            return CommandResponse(text="This message is not in the configured alerts channel.")

        ticket = self.repository.get_by_post_id(post_id)
        if ticket is None or ticket.jira_issue_key is None:
            await self.handle_alert_post(post)

        result = await self.confirm_incident(
            post_id, confirmed_by_user_id=user_id, source="slash_command"
        )
        return CommandResponse(text=result.message)

    async def confirm_incident(
        self,
        post_id: str,
        *,
        confirmed_by_user_id: str,
        source: str,
        confirmed_at: datetime | None = None,
    ) -> ConfirmationResult:
        confirmed_at = confirmed_at or backend_now()
        ticket = self.repository.get_by_post_id(post_id)
        if ticket is None:
            log.warning(
                "incident.confirmation.no_ticket",
                mattermost_post_id=post_id,
                source=source,
            )
            return ConfirmationResult(
                status=ConfirmationStatus.NOT_FOUND,
                message=f"No Jira issue mapping found for Band post `{post_id}`.",
            )

        if ticket.jira_issue_key is None:
            self.repository.mark_pending_confirmation(post_id, confirmed_by_user_id, confirmed_at)
            log.info(
                "incident.confirmation.pending_jira",
                mattermost_post_id=post_id,
                source=source,
            )
            return ConfirmationResult(
                status=ConfirmationStatus.PENDING_JIRA,
                message=(
                    "Incident confirmation is saved, but the Jira issue is not ready yet. "
                    "The bot will finish the update automatically after issue creation."
                ),
            )

        if ticket.valid_incident and ticket.incident_post_id:
            log.info(
                "incident.confirmation.skipped_already_confirmed",
                mattermost_post_id=post_id,
                jira_issue_key=ticket.jira_issue_key,
            )
            return ConfirmationResult(
                status=ConfirmationStatus.ALREADY_CONFIRMED,
                message=(
                    "Incident is already confirmed: "
                    f"{ticket.jira_issue_url or ticket.jira_issue_key}"
                ),
                jira_issue_url=ticket.jira_issue_url,
                incident_message_url=ticket.incident_message_url,
            )

        self.repository.mark_confirmation_started(post_id, confirmed_by_user_id, confirmed_at)
        ticket = self.repository.get_by_post_id(post_id)
        assert ticket is not None
        confirmed_by_display = await self._resolve_user_display(confirmed_by_user_id)

        try:
            await self._publish_incident_message_if_needed(
                ticket,
                confirmed_by_user_id=confirmed_by_user_id,
                confirmed_by_display=confirmed_by_display,
                confirmed_at=confirmed_at,
            )
            ticket = self.repository.get_by_post_id(post_id)
            assert ticket is not None
            await self._update_jira_for_confirmation(ticket, confirmed_by=confirmed_by_display)
            self.repository.mark_confirmed(
                post_id, user_id=confirmed_by_user_id, confirmed_at=confirmed_at
            )
        except ApiError as exc:
            self.repository.mark_confirmation_failed(post_id, str(exc))
            log.error(
                "incident.confirmation.failed",
                mattermost_post_id=post_id,
                jira_issue_key=ticket.jira_issue_key,
                error=str(exc),
            )
            return ConfirmationResult(
                status=ConfirmationStatus.ERROR,
                message=(
                    "Incident confirmation was recorded, but an API update failed. "
                    "The bot will retry pending work."
                ),
                jira_issue_url=ticket.jira_issue_url,
                incident_message_url=ticket.incident_message_url,
            )

        ticket = self.repository.get_by_post_id(post_id)
        assert ticket is not None
        log.info(
            "incident.confirmed",
            mattermost_post_id=post_id,
            jira_issue_key=ticket.jira_issue_key,
            incident_post_id=ticket.incident_post_id,
            confirmed_by_user_id=confirmed_by_user_id,
            source=source,
        )
        await self._post_alert_thread_reply(
            post_id,
            channel_id=ticket.mattermost_channel_id,
            message=format_thread_status_changed(
                jira_issue_key=ticket.jira_issue_key,
                jira_issue_url=ticket.jira_issue_url,
                incident_message_url=ticket.incident_message_url,
                status_transitioned=bool(self.settings.jira_confirmed_status_id),
            ),
            event="mattermost.alert_thread.status_notice_published",
            props={
                "jira_issue_key": ticket.jira_issue_key,
                "confirmed_by_user_id": confirmed_by_user_id,
            },
        )
        return ConfirmationResult(
            status=ConfirmationStatus.CONFIRMED,
            message=(
                "Incident confirmed. "
                f"Jira: {ticket.jira_issue_url}. "
                f"Incident message: {ticket.incident_message_url}."
            ),
            jira_issue_url=ticket.jira_issue_url,
            incident_message_url=ticket.incident_message_url,
        )

    async def process_pending_work(self, *, limit: int = 50) -> None:
        for ticket in self.repository.list_pending_jira(limit=limit):
            await self._ensure_jira_issue(ticket)

        for ticket in self.repository.list_pending_confirmations(limit=limit):
            if ticket.jira_issue_key is None:
                continue
            user_id = ticket.pending_confirmation_by_user_id or ticket.confirmed_by_user_id
            if user_id is None:
                continue
            await self.confirm_incident(
                ticket.mattermost_post_id,
                confirmed_by_user_id=user_id,
                confirmed_at=ticket.pending_confirmation_at or ticket.confirmed_at,
                source="pending_worker",
            )

    async def backfill_recent_alerts(self) -> None:
        if self.settings.backfill_recent_posts_limit <= 0:
            return
        posts = await self.mattermost.fetch_recent_channel_posts(
            self.settings.mattermost_alert_channel_id,
            limit=self.settings.backfill_recent_posts_limit,
        )
        for post in posts:
            await self.handle_alert_post(post)

    async def debug_create_from_link(self, link: str) -> DebugCreateFromLinkResult:
        """Create (or fetch) a Jira issue for an alert given its Band link/post id.

        Reuses the normal :meth:`handle_alert_post` flow, but resolves the post
        from a pasted permalink and returns explicit feedback for the admin UI.
        """
        post_id = parse_post_id_from_text(link)
        if post_id is None:
            return DebugCreateFromLinkResult(
                ok=False,
                status="invalid_link",
                message="Не удалось распознать ссылку или post id.",
            )

        try:
            post = await self.mattermost.get_post(post_id)
        except ApiError as exc:
            log.error(
                "debug_admin.create_from_link.post_lookup_failed",
                mattermost_post_id=post_id,
                error=str(exc),
            )
            return DebugCreateFromLinkResult(
                ok=False,
                status="post_not_found",
                message=f"Не удалось прочитать сообщение `{post_id}`: {exc}",
                mattermost_post_id=post_id,
            )

        if post.channel_id != self.settings.mattermost_alert_channel_id:
            return DebugCreateFromLinkResult(
                ok=False,
                status="skipped",
                message="Сообщение не в канале алертов.",
                mattermost_post_id=post_id,
            )
        if is_resolved_alert(post.message):
            return DebugCreateFromLinkResult(
                ok=False,
                status="skipped",
                message="Это resolved-алерт — задача не создаётся.",
                mattermost_post_id=post_id,
            )

        existing = self.repository.get_by_post_id(post_id)
        already_had_issue = bool(existing and existing.jira_issue_key)

        ticket = await self.handle_alert_post(post)
        if ticket is None:
            return DebugCreateFromLinkResult(
                ok=False,
                status="skipped",
                message="Сообщение пропущено (бот, не алерт-канал или resolved).",
                mattermost_post_id=post_id,
            )
        if ticket.jira_issue_key:
            return DebugCreateFromLinkResult(
                ok=True,
                status="exists" if already_had_issue else "created",
                message=("Задача уже существовала." if already_had_issue else "Задача создана."),
                mattermost_post_id=post_id,
                jira_issue_key=ticket.jira_issue_key,
                jira_issue_url=ticket.jira_issue_url,
            )
        return DebugCreateFromLinkResult(
            ok=False,
            status="error",
            message=ticket.last_error or "Создание задачи не удалось, см. логи.",
            mattermost_post_id=post_id,
        )

    async def debug_recreate_jira_issue(
        self, post_id: str, *, force: bool = False
    ) -> DebugJiraRecreateResult:
        ticket = self.repository.get_by_post_id(post_id)
        if ticket is None:
            return DebugJiraRecreateResult(
                ok=False,
                status="not_found",
                message=f"Alert ticket for post_id={post_id} was not found.",
                mattermost_post_id=post_id,
            )
        if ticket.jira_issue_key and not force:
            return DebugJiraRecreateResult(
                ok=False,
                status="conflict",
                message=(
                    "Jira issue already exists for this alert. "
                    "Use force=true to create a replacement issue."
                ),
                mattermost_post_id=post_id,
                jira_issue_key=ticket.jira_issue_key,
                jira_issue_url=ticket.jira_issue_url,
            )

        previous_key = ticket.jira_issue_key
        previous_url = ticket.jira_issue_url
        try:
            issue = await self._create_jira_issue(ticket)
        except ApiError as exc:
            if previous_key:
                self.repository.set_last_error(post_id, str(exc))
            else:
                self.repository.mark_jira_create_failed(post_id, str(exc))
            log.error(
                "debug_admin.jira_issue.recreate_failed",
                mattermost_post_id=post_id,
                force=force,
                error=str(exc),
            )
            return DebugJiraRecreateResult(
                ok=False,
                status="error",
                message=str(exc),
                mattermost_post_id=post_id,
                previous_jira_issue_key=previous_key,
                previous_jira_issue_url=previous_url,
            )

        self.repository.replace_jira_issue(
            post_id,
            issue.key,
            issue.url,
            reset_confirmation_comment=bool(ticket.valid_incident),
        )
        updated_ticket = self.repository.get_by_post_id(post_id)
        assert updated_ticket is not None
        if updated_ticket.valid_incident and updated_ticket.incident_post_id:
            confirmed_by = updated_ticket.confirmed_by_user_id or "debug-admin"
            confirmed_by_display = await self._resolve_user_display(confirmed_by)
            try:
                await self._update_jira_for_confirmation(
                    updated_ticket, confirmed_by=confirmed_by_display
                )
                self.repository.mark_confirmed(
                    post_id,
                    user_id=confirmed_by,
                    confirmed_at=updated_ticket.confirmed_at or backend_now(),
                )
            except ApiError as exc:
                self.repository.mark_confirmation_failed(post_id, str(exc))
                log.error(
                    "debug_admin.jira_issue.confirmation_reapply_failed",
                    mattermost_post_id=post_id,
                    jira_issue_key=issue.key,
                    error=str(exc),
                )
                return DebugJiraRecreateResult(
                    ok=False,
                    status="confirmation_error",
                    message=str(exc),
                    mattermost_post_id=post_id,
                    jira_issue_key=issue.key,
                    jira_issue_url=issue.url,
                    previous_jira_issue_key=previous_key,
                    previous_jira_issue_url=previous_url,
                )

        log.info(
            "debug_admin.jira_issue.recreated",
            mattermost_post_id=post_id,
            jira_issue_key=issue.key,
            previous_jira_issue_key=previous_key,
            force=force,
        )
        return DebugJiraRecreateResult(
            ok=True,
            status="recreated" if force and previous_key else "created",
            message="Jira issue created.",
            mattermost_post_id=post_id,
            jira_issue_key=issue.key,
            jira_issue_url=issue.url,
            previous_jira_issue_key=previous_key,
            previous_jira_issue_url=previous_url,
        )

    async def _ensure_jira_issue(self, ticket: AlertTicket) -> None:
        if ticket.jira_issue_key:
            return
        try:
            issue = await self._create_jira_issue(ticket)
            self.repository.attach_jira_issue(ticket.mattermost_post_id, issue.key, issue.url)
            log.info(
                "jira.issue.created",
                mattermost_post_id=ticket.mattermost_post_id,
                jira_issue_key=issue.key,
            )
            display_issue = self._display_jira_issue(issue)
            issue_message = format_thread_issue_created(
                jira_issue_key=display_issue.key,
                jira_issue_url=display_issue.url,
            )
            action_attachments = self._alert_action_attachments(
                ticket.mattermost_post_id,
                title=display_issue.key,
                title_link=display_issue.url,
            )
            if action_attachments is not None:
                await self._post_alert_thread_reply(
                    ticket.mattermost_post_id,
                    channel_id=ticket.mattermost_channel_id,
                    message="",
                    event="mattermost.alert_thread.issue_notice_published",
                    props={
                        "jira_issue_key": issue.key,
                        "attachments": action_attachments,
                    },
                )
            else:
                await self._post_alert_thread_reply(
                    ticket.mattermost_post_id,
                    channel_id=ticket.mattermost_channel_id,
                    message=issue_message,
                    event="mattermost.alert_thread.issue_notice_published",
                    props={"jira_issue_key": issue.key},
                )
        except ApiError as exc:
            self.repository.mark_jira_create_failed(ticket.mattermost_post_id, str(exc))
            log.error(
                "jira.issue.create_failed",
                mattermost_post_id=ticket.mattermost_post_id,
                error=str(exc),
            )

    async def _create_jira_issue(self, ticket: AlertTicket) -> JiraIssue:
        if not self.settings.jira_create_enabled:
            return self._stub_jira_issue(ticket)
        post = ticket_to_post(ticket)
        return await self.jira.create_issue(
            post,
            message_url=ticket.mattermost_message_url,
            channel_name=ticket.mattermost_channel_name,
        )

    def _stub_jira_issue(self, ticket: AlertTicket) -> JiraIssue:
        issue_key = self.settings.jira_stub_issue_key
        if not issue_key:
            issue_key = f"{self.settings.jira_project_key}-{10000 + secrets.randbelow(90000)}"
        else:
            suffix = ticket.mattermost_post_id[:12]
            prefix = issue_key[: 63 - len(suffix)]
            issue_key = f"{prefix}-{suffix}"
        issue_url = f"{self.settings.jira_base_url}/browse/{issue_key}"
        log.info(
            "jira.issue.create_stubbed",
            mattermost_post_id=ticket.mattermost_post_id,
            jira_issue_key=issue_key,
            jira_issue_url=issue_url,
        )
        return JiraIssue(key=issue_key, url=issue_url)

    def _display_jira_issue(self, issue: JiraIssue) -> JiraIssue:
        if self.settings.jira_create_enabled or not self.settings.jira_stub_issue_key:
            return issue
        issue_key = self.settings.jira_stub_issue_key
        return JiraIssue(
            key=issue_key,
            url=f"{self.settings.jira_base_url}/browse/{issue_key}",
        )

    async def _resolve_user_display(self, user_id: str) -> str:
        try:
            return await self.mattermost.get_user_display_name(user_id)
        except ApiError as exc:
            log.warning(
                "mattermost.user.lookup_failed",
                mattermost_user_id=user_id,
                error=str(exc),
            )
            return user_id

    async def _post_alert_thread_reply(
        self,
        post_id: str,
        *,
        channel_id: str,
        message: str,
        event: str,
        props: dict | None = None,
    ) -> None:
        """Reply in the alert thread; best-effort, never fails the caller."""
        thread_props = {"mattermost_alert_post_id": post_id, **(props or {})}
        try:
            reply = await self.mattermost.create_post(
                channel_id=channel_id,
                message=message,
                root_id=post_id,
                props=thread_props,
            )
        except ApiError as exc:
            log.warning(
                "mattermost.alert_thread.reply_failed",
                mattermost_post_id=post_id,
                event_kind=event,
                error=str(exc),
            )
            return
        log.info(
            event,
            mattermost_post_id=post_id,
            reply_post_id=reply.id,
        )

    async def _publish_incident_message_if_needed(
        self,
        ticket: AlertTicket,
        *,
        confirmed_by_user_id: str,
        confirmed_by_display: str,
        confirmed_at: datetime,
    ) -> None:
        if ticket.incident_post_id:
            return
        alert_attachments = await self._alert_attachments(ticket)
        message = format_incident_message(
            ticket,
            confirmed_by=confirmed_by_display,
            confirmed_at=confirmed_at,
            include_alert_text=not alert_attachments,
            include_alert_link=not alert_attachments,
        )
        props = {
            "mattermost_alert_post_id": ticket.mattermost_post_id,
            "jira_issue_key": ticket.jira_issue_key,
            "confirmed_by_user_id": confirmed_by_user_id,
        }
        if alert_attachments:
            props["attachments"] = alert_attachments
        incident_post = await self.mattermost.create_post(
            channel_id=self.settings.mattermost_incident_channel_id,
            message=message,
            props=props,
        )
        incident_url = self.mattermost.permalink(incident_post.id)
        self.repository.set_incident_message(
            ticket.mattermost_post_id, incident_post.id, incident_url
        )
        log.info(
            "mattermost.incident_message.published",
            mattermost_post_id=ticket.mattermost_post_id,
            incident_post_id=incident_post.id,
        )
        # Same management controls as a manual incident (validity menu, end,
        # summary), minus "Создать задачу" since the Jira issue already exists.
        if self.settings.service_public_url and ticket.jira_issue_key:
            callback_url = alert_action_callback_url(self.settings.service_public_url)
            await self._post_incident_thread_reply(
                incident_post.id,
                channel_id=self.settings.mattermost_incident_channel_id,
                message="",
                event="mattermost.incident_thread.controls_published",
                props={
                    "attachments": [
                        build_incident_controls_attachment(
                            incident_post_id=incident_post.id,
                            callback_url=callback_url,
                            issue_key=ticket.jira_issue_key,
                            issue_url=ticket.jira_issue_url,
                        )
                    ]
                },
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

    async def _update_jira_for_confirmation(
        self,
        ticket: AlertTicket,
        *,
        confirmed_by: str,
    ) -> None:
        assert ticket.jira_issue_key is not None
        assert ticket.incident_message_url is not None

        jira_valid = await self.jira.get_valid_incident(ticket.jira_issue_key)
        if jira_valid is True:
            self.repository.sync_valid_incident_from_jira(ticket.mattermost_post_id)
            log.info(
                "jira.valid_incident.synced_true",
                mattermost_post_id=ticket.mattermost_post_id,
                jira_issue_key=ticket.jira_issue_key,
            )
        else:
            await self.jira.set_valid_incident(ticket.jira_issue_key, True)
            log.info(
                "jira.valid_incident.updated",
                mattermost_post_id=ticket.mattermost_post_id,
                jira_issue_key=ticket.jira_issue_key,
            )

        if not ticket.jira_confirmation_comment_added:
            # Runs once per confirmation: swap the alert description for the
            # postmortem template, then add the confirmation comment. The
            # description is set first so a later comment failure does not leave
            # the issue without the template (the guard skips both on retry).
            await self.jira.set_description(
                ticket.jira_issue_key,
                build_postmortem_description(
                    incident_message_url=ticket.incident_message_url,
                    alert_message_url=ticket.mattermost_message_url,
                ),
            )
            log.info(
                "jira.description.postmortem_set",
                mattermost_post_id=ticket.mattermost_post_id,
                jira_issue_key=ticket.jira_issue_key,
            )
            await self.jira.add_confirmation_comment(
                ticket.jira_issue_key,
                incident_message_url=ticket.incident_message_url,
                confirmed_by_user_id=confirmed_by,
            )
            self.repository.mark_jira_confirmation_comment_added(ticket.mattermost_post_id)
            log.info(
                "jira.comment.added",
                mattermost_post_id=ticket.mattermost_post_id,
                jira_issue_key=ticket.jira_issue_key,
            )

        if self.settings.jira_confirmed_status_id:
            try:
                await self.jira.transition_issue(
                    ticket.jira_issue_key, self.settings.jira_confirmed_status_id
                )
                log.info(
                    "jira.issue.transitioned",
                    mattermost_post_id=ticket.mattermost_post_id,
                    jira_issue_key=ticket.jira_issue_key,
                    transition_id=self.settings.jira_confirmed_status_id,
                )
            except ApiError as exc:
                log.warning(
                    "jira.issue.transition_failed",
                    mattermost_post_id=ticket.mattermost_post_id,
                    jira_issue_key=ticket.jira_issue_key,
                    transition_id=self.settings.jira_confirmed_status_id,
                    error=str(exc),
                )
