from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mm_jira_bot.actions import (
    ACTION_EXPECTED,
    ACTION_FALSE,
    ACTION_FEEDBACK,
    ACTION_INCIDENT,
    ACTION_SOURCE_INCIDENT,
    ACTION_SUMMARY,
    ACTION_VALID,
    ACTION_VALIDITY,
    INCIDENT_OPEN_COLOR,
    NOTICE_ATTACHMENT_COLOR,
    OPS_ISSUE_CREATED_COLOR,
    alert_action_callback_url,
    build_alert_controls_attachment,
    build_alert_feedback_attachment,
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
    alert_signature,
    format_ops_issue_created,
    format_thread_validity_changed,
    is_resolved_alert,
    mention_from_display,
)
from mm_jira_bot.jira import (
    VALID_INCIDENT_CONFIRMED_VALUE,
    VALID_INCIDENT_EXPECTED_VALUE,
    VALID_INCIDENT_FALSE_VALUE,
)
from mm_jira_bot.logging import get_logger
from mm_jira_bot.mattermost import parse_posted_event, parse_reaction_event
from mm_jira_bot.repository import AlertTicket, AlertTicketRepository
from mm_jira_bot.retry import ApiError
from mm_jira_bot.service._incidents import IncidentMixin
from mm_jira_bot.service._jira_sync import JiraSyncMixin
from mm_jira_bot.service._postmortem import PostmortemMixin
from mm_jira_bot.service._shared import (
    ActionResult,
    SharedMixin,
    _validity_action_message,
)
from mm_jira_bot.service._thread_summary import ThreadSummaryMixin

# Имя логгера держим стабильным (`mm_jira_bot.service`), несмотря на перенос модуля
# в пакет `service/` — на него завязаны тесты и настроенные логгеры.
log = get_logger("mm_jira_bot.service")

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


def _incident_action_message(result: ConfirmationResult) -> str:
    return {
        ConfirmationStatus.CONFIRMED: "Инцидент заведён ✅",
        ConfirmationStatus.ALREADY_CONFIRMED: "Инцидент уже подтверждён.",
        ConfirmationStatus.PENDING_JIRA: ("Подтверждение сохранено — задача Jira ещё создаётся."),
        ConfirmationStatus.ERROR: ("Произошла ошибка при подтверждении, бот повторит позже."),
        ConfirmationStatus.NOT_FOUND: ("Не нашёл связку с Jira для этого сообщения."),
    }.get(result.status, result.message)


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


class IncidentBotService(
    SharedMixin, IncidentMixin, JiraSyncMixin, PostmortemMixin, ThreadSummaryMixin
):
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
        """Resolve configured logins/groups to ids and enable the allowlist gate.

        ``MATTERMOST_AUTHORIZED_USERNAMES`` is a mixed list: each entry is first
        tried as a login (batch resolve), and anything left over is tried as a
        Mattermost group whose members are expanded into the allowlist. Re-run
        periodically (see ``authorized_users_refresh_loop``) so group membership
        changes propagate.

        No entries configured -> the gate stays disabled (act on everyone). A
        typo'd entry (neither login nor group) is logged loudly. Failure
        semantics differ by state: the *first* resolve fails open (a Mattermost
        hiccup must not brick incident tooling), but a later refresh keeps the
        last known-good set instead of clobbering a working allowlist.
        """
        names = list(self.settings.mattermost_authorized_usernames)
        if not names:
            log.info("authorized_users.disabled")
            return
        try:
            users = await self.mattermost.get_user_ids_by_usernames(names)
        except ApiError as exc:
            self._degrade_authorization("authorized_users.resolve_failed", names, error=str(exc))
            return

        member_ids: set[str] = set(users.values())
        group_names = [name for name in names if name not in users]
        resolved_groups: dict[str, str] = {}
        if group_names:
            try:
                resolved_groups = await self.mattermost.get_group_ids_by_names(group_names)
                for group_id in resolved_groups.values():
                    member_ids |= await self.mattermost.get_group_member_ids(group_id)
            except ApiError as exc:
                # Groups may need a license/permission the token lacks: a group
                # lookup failure must not brick the login-based allowlist.
                log.warning(
                    "authorized_users.groups_resolve_failed",
                    requested_groups=group_names,
                    error=str(exc),
                )

        unresolved = [name for name in names if name not in users and name not in resolved_groups]
        if unresolved:
            log.warning("authorized_users.unresolved", unresolved=unresolved)
        if not member_ids:
            self._degrade_authorization("authorized_users.none_resolved", names)
            return
        self._authorized_user_ids = frozenset(member_ids)
        self._authorization_enforced = True
        log.info(
            "authorized_users.enabled",
            resolved_count=len(member_ids),
            resolved_logins=sorted(users),
            resolved_groups=sorted(resolved_groups),
        )

    def _degrade_authorization(self, event: str, names: list[str], **fields: Any) -> None:
        """Handle a failed/empty resolution: keep last-good if already enforced.

        Fail-open (disable the gate) only on the very first resolution; once a
        working set exists, a transient failure or empty response keeps it so a
        Mattermost glitch can't silently lock everyone out or open the gate.
        """
        if self._authorization_enforced:
            log.warning(f"{event}_keep_last", requested=names, **fields)
            return
        self._authorized_user_ids = frozenset()
        self._authorization_enforced = False
        log.warning(f"{event}_fail_open", requested=names, **fields)

    def _is_authorized(self, user_id: str) -> bool:
        return not self._authorization_enforced or user_id in self._authorized_user_ids

    def _interactive_controls_enabled(self) -> bool:
        """Whether to attach interactive button/menu cards (vs. emoji-only mode).

        Controls need an absolute callback URL (``SERVICE_PUBLIC_URL``); even with
        one configured, ``INTERACTIVE_BUTTONS_ENABLED=false`` forces emoji-only
        mode, dropping every card and leaving the emoji-reaction flow as the only
        entry point.
        """
        return bool(self.settings.service_public_url) and self.settings.interactive_buttons_enabled

    def _action_callback_url(self) -> str:
        service_public_url = self.settings.service_public_url
        if service_public_url is None:
            raise RuntimeError("SERVICE_PUBLIC_URL is required for interactive controls")
        return alert_action_callback_url(service_public_url)

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

    def _validity_label_for_emoji(self, emoji_name: str) -> str | None:
        """Map the two lightweight reactions to their "Валидность" option."""
        if emoji_name == self.settings.mattermost_false_incident_reaction_name:
            return VALID_INCIDENT_FALSE_VALUE
        if emoji_name == self.settings.mattermost_expected_incident_reaction_name:
            return VALID_INCIDENT_EXPECTED_VALUE
        return None

    async def handle_reaction(self, reaction: ReactionEvent) -> ConfirmationResult | ActionResult:
        log.info(
            "mattermost.reaction.received",
            mattermost_post_id=reaction.post_id,
            emoji_name=reaction.emoji_name,
            user_id=reaction.user_id,
        )
        if reaction.user_id == self.settings.mattermost_bot_user_id:
            # The bot adds its own "Ожидаемый" reaction on repeat alerts; that
            # event echoes back over the websocket. Ignore it so it never
            # re-enters the validity path (or posts an unauthorized notice when
            # an allowlist is configured).
            return ConfirmationResult(
                status=ConfirmationStatus.IGNORED,
                message="Reaction ignored: bot's own reaction.",
            )
        is_incident = reaction.emoji_name == self.settings.mattermost_incident_reaction_name
        is_summary = reaction.emoji_name == self.settings.mattermost_summary_reaction_name
        validity_label = (
            None if is_incident else self._validity_label_for_emoji(reaction.emoji_name)
        )
        is_incident_end = reaction.emoji_name in INCIDENT_END_REACTION_NAMES
        if not is_incident and not is_summary and validity_label is None and not is_incident_end:
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
            try:
                unauth_post = await self.mattermost.get_post(reaction.post_id)
                display = mention_from_display(
                    await self.mattermost.get_user_display_name(reaction.user_id)
                )
                await self._post_unauthorized_notice(
                    root_post_id=reaction.post_id,
                    channel_id=unauth_post.channel_id,
                    user_mention=display,
                )
            except ApiError:
                pass
            return ConfirmationResult(
                status=ConfirmationStatus.IGNORED,
                message="Reaction ignored: user is not authorized.",
            )

        post = await self.mattermost.get_post(reaction.post_id)
        if is_summary:
            # Summary works in any thread (alert/incident/manual); it resolves the
            # thread root itself and never touches Jira.
            return await self.generate_thread_summary(
                post, requested_by_user_id=reaction.user_id, source="reaction"
            )
        in_incident_channel = post.channel_id == self.settings.mattermost_incident_channel_id
        if is_incident_end and in_incident_channel:
            return await self.handle_incident_checkmark(
                post,
                reacted_by_user_id=reaction.user_id,
                ended_at=datetime_from_mattermost_ms(reaction.create_at) or backend_now(),
                source="reaction",
            )
        if validity_label is not None and in_incident_channel:
            # Validity emoji in an incident thread doubles as "finish + postmortem"
            # with that validity (✅ stays the Валидный shortcut). In the alert
            # channel the same emoji is the light label-only path below.
            return await self.handle_incident_checkmark(
                post,
                reacted_by_user_id=reaction.user_id,
                ended_at=datetime_from_mattermost_ms(reaction.create_at) or backend_now(),
                source="reaction",
                validity_label=validity_label,
            )
        if is_incident_end:
            # A checkmark anywhere but an incident-thread root is not actionable; bail
            # before the alert-channel path below would create a Jira issue for it.
            return ConfirmationResult(
                status=ConfirmationStatus.IGNORED,
                message="Reaction ignored: checkmark only handled on incident thread roots.",
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

    async def _post_incident_thread_reply(
        self,
        post_id: str,
        *,
        channel_id: str,
        message: str,
        event: str,
        props: dict | None = None,
        color: str = NOTICE_ATTACHMENT_COLOR,
    ) -> None:
        message, props = self._box_thread_reply(message, props, color)
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
        await self._announce_issue_to_ops(updated_ticket, issue, source="recreate")
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

    async def _announce_issue_to_ops(
        self, ticket: AlertTicket, issue: JiraIssue, *, source: str
    ) -> None:
        """Best-effort: post every newly created Jira issue to the ops channel with
        a link back to its source thread/message. Shares ``MATTERMOST_OPS_CHANNEL_ID``
        with the error-alert feed; skips stub issues (``jira_create_enabled=false``)
        and never breaks issue creation (a failed post is logged, not propagated).
        """
        channel_id = self.settings.mattermost_ops_channel_id
        if not channel_id or not self.settings.jira_create_enabled:
            return
        message = format_ops_issue_created(
            jira_issue_key=issue.key,
            jira_issue_url=issue.url,
            source_title=ticket.mattermost_alert_title,
            source_message_url=ticket.mattermost_message_url,
            channel_name=ticket.mattermost_channel_name,
            incident_message_url=ticket.incident_message_url,
        )
        try:
            await self.mattermost.create_post(
                channel_id=channel_id,
                message="",
                props={
                    "jira_issue_key": issue.key,
                    "attachments": [
                        {"fallback": message, "color": OPS_ISSUE_CREATED_COLOR, "text": message}
                    ],
                },
            )
            log.info(
                "ops.issue_created.published",
                jira_issue_key=issue.key,
                mattermost_post_id=ticket.mattermost_post_id,
                source=source,
            )
        except ApiError as exc:
            log.warning(
                "ops.issue_created.failed",
                jira_issue_key=issue.key,
                mattermost_post_id=ticket.mattermost_post_id,
                error=str(exc),
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

    async def _post_unauthorized_notice(
        self, *, root_post_id: str, channel_id: str, user_mention: str
    ) -> None:
        allowed = " ".join(f"@{u}" for u in self.settings.mattermost_authorized_usernames)
        text = "Это действие доступно только авторизованным пользователям."
        if allowed:
            text += f"\nРазрешённые пользователи: `{allowed}`"
        await self._post_alert_thread_reply(
            root_post_id,
            channel_id=channel_id,
            message=text,
            event="mattermost.unauthorized.notice_posted",
            color=INCIDENT_OPEN_COLOR,
            mention=user_mention,
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
