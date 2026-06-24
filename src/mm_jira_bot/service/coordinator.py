from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any, cast

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
    DUTY_HELP_ATTACHMENT_COLOR,
    INCIDENT_DONE_COLOR,
    INCIDENT_OPEN_COLOR,
    NOTICE_ATTACHMENT_COLOR,
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
    runtime_timezone,
)
from mm_jira_bot.formatting import (
    alert_signature,
    extract_alert_title,
    format_alert_duty_help,
    format_incident_duty_help,
    format_incident_message,
    format_thread_issue_created,
    format_thread_linked_to_root,
    format_thread_status_changed,
    format_thread_validity_changed,
    is_resolved_alert,
    mark_incident_message_completed,
    mention_from_display,
)
from mm_jira_bot.jira import (
    VALID_INCIDENT_CONFIRMED_VALUE,
    VALID_INCIDENT_EXPECTED_VALUE,
    VALID_INCIDENT_FALSE_VALUE,
    stub_jira_issue,
)
from mm_jira_bot.jira_payload import (
    build_expected_alert_block,
    build_jira_description,
    build_postmortem_description,
)
from mm_jira_bot.llm import StreamProgress
from mm_jira_bot.logging import get_logger
from mm_jira_bot.mattermost import parse_posted_event, parse_reaction_event
from mm_jira_bot.postmortem import (
    ThreadMessage,
    build_incident_report_prompt,
    build_postmortem_comment,
    extract_postmortem_summary,
    format_incident_closed_notice,
    format_thread_transcript,
)
from mm_jira_bot.repository import AlertTicket, AlertTicketRepository, ticket_to_post
from mm_jira_bot.retry import ApiError
from mm_jira_bot.summary import (
    format_thread_summary_reply,
    format_thread_summary_streaming,
)

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
SUMMARY_PENDING_TEXT = "⏳ Генерация саммари…"
SUMMARY_FAILED_TEXT = "Не удалось сгенерировать саммари, попробуйте позже."

# DB-override keys for the runtime-editable LLM prompt templates (debug panel).
_PROMPT_KEY_SUMMARY = "llm_summary_prompt"
_PROMPT_KEY_POSTMORTEM = "llm_postmortem_prompt"


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

    async def handle_manual_incident_post(self, post: MattermostPost) -> None:
        """A human's root post in the incident channel: ping on-call and (when
        interactive controls are on) offer a "Создать задачу" card.

        Only root posts from real users (no bots/webhooks) qualify. The Jira
        issue is not created here — it is created on the button click or the
        checkmark. With interactive controls (SERVICE_PUBLIC_URL +
        INTERACTIVE_BUTTONS_ENABLED≠false) we post the card with the duty mention
        above it; in emoji-only mode we still post the duty mention alone so the
        manual incident gets noticed, leaving the checkmark flow as the action
        path. When no duty mention is configured, emoji-only mode posts nothing.
        Idempotent: the reply is posted once, guarded by the unique ticket row.
        """
        if post.channel_id != self.settings.mattermost_incident_channel_id:
            return
        if post.root_id:  # only channel root posts, not thread replies
            return
        if self._is_bot_post(post):
            return
        interactive = self._interactive_controls_enabled()
        duty_mention = self.settings.mattermost_duty_mention
        # Nothing to post (no card, no ping, no help) → leave the checkmark flow
        # as the sole fallback, exactly as before.
        if not interactive and not duty_mention and not self.settings.duty_help_enabled:
            return
        channel_name = post.channel_name or await self.mattermost.get_channel_name(post.channel_id)
        _ticket, created = self.repository.create_or_get_incident_thread(
            post,
            message_url=self.mattermost.permalink(post.id),
            channel_name=channel_name,
        )
        if not created:
            return
        if interactive:
            callback_url = self._action_callback_url()
            await self._post_incident_thread_reply(
                post.id,
                channel_id=post.channel_id,
                # The duty mention goes in the message text (above the card) so the
                # @group ping actually fires — attachment text does not notify.
                message=duty_mention or "",
                event="mattermost.incident_thread.controls_published",
                props={
                    "attachments": [
                        build_incident_create_attachment(
                            incident_post_id=post.id, callback_url=callback_url
                        )
                    ]
                },
            )
        elif duty_mention:
            # No controls: just ping on-call so the manual incident is noticed.
            # Kept as a bare message (not a boxed notice) so the @mention notifies.
            await self._post_incident_thread_mention(
                post.id,
                channel_id=post.channel_id,
                message=duty_mention,
                event="mattermost.incident_thread.duty_pinged",
            )
        # One duty cheat-sheet after the create guard, common to every branch.
        if self.settings.duty_help_enabled:
            await self._post_incident_thread_reply(
                post.id,
                channel_id=post.channel_id,
                message=self._incident_duty_help(),
                event="mattermost.incident_thread.duty_help_published",
                color=DUTY_HELP_ATTACHMENT_COLOR,
            )

    def _incident_duty_help(self) -> str:
        return format_incident_duty_help(
            false_emoji=self.settings.mattermost_false_incident_reaction_name,
            expected_emoji=self.settings.mattermost_expected_incident_reaction_name,
            summary_emoji=self.settings.mattermost_summary_reaction_name,
        )

    def _prompt_env_default(self, key: str) -> str | None:
        """Env-configured override for a prompt key (``None`` ⇒ built-in default)."""
        if key == _PROMPT_KEY_SUMMARY:
            return self.settings.llm_summary_prompt
        if key == _PROMPT_KEY_POSTMORTEM:
            return self.settings.llm_postmortem_prompt
        return None

    def _resolve_prompt_template(self, key: str) -> str | None:
        """Effective prompt override: DB (debug-panel edit) → env → ``None``.

        ``None`` lets ``build_incident_report_prompt`` fall back to the built-in
        default. The DB read runs only on summary/postmortem generation, so edits
        from the debug panel apply on the next run with no restart.
        """
        return self.repository.get_setting(key) or self._prompt_env_default(key)

    async def _post_incident_thread_mention(
        self,
        post_id: str,
        *,
        channel_id: str,
        message: str,
        event: str,
    ) -> None:
        """Post a bare @mention reply in an incident thread.

        Unlike ``_post_incident_thread_reply`` this keeps the text in the post
        ``message`` (no boxed attachment) so the mention actually fires a ping.
        Best-effort: a failed post never breaks the caller.
        """
        try:
            reply = await self.mattermost.create_post(
                channel_id=channel_id,
                message=message,
                root_id=post_id,
                props={"mattermost_incident_post_id": post_id},
            )
        except ApiError as exc:
            log.warning(
                "mattermost.incident_thread.reply_failed",
                mattermost_post_id=post_id,
                event_kind=event,
                error=str(exc),
            )
            return
        log.info(event, mattermost_post_id=post_id, reply_post_id=reply.id)

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

    def _incident_controls_attachment(
        self, incident_post_id: str, *, completed: bool = False
    ) -> dict:
        """Build the incident controls card, picking the task header automatically:
        shown for alert-originated incidents, omitted for manual ones."""
        callback_url = self._action_callback_url()
        ticket = self.repository.get_by_incident_post_id(incident_post_id)
        issue_key = issue_url = None
        if ticket is not None and ticket.incident_post_id != ticket.mattermost_post_id:
            issue_key, issue_url = ticket.jira_issue_key, ticket.jira_issue_url
        return build_incident_controls_attachment(
            incident_post_id=incident_post_id,
            callback_url=callback_url,
            issue_key=issue_key,
            issue_url=issue_url,
            completed=completed,
        )

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
            update_attachments = None
            if (
                result.status == ConfirmationStatus.INCIDENT_ENDED
                and self._interactive_controls_enabled()
            ):
                update_attachments = [
                    self._incident_controls_attachment(incident_post_id, completed=True)
                ]
            return ActionResult(
                message=_incident_end_message(result),
                update_attachments=update_attachments,
            )

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

        update_attachments = None
        if self._interactive_controls_enabled():
            callback_url = self._action_callback_url()
            update_attachments = [
                build_incident_controls_attachment(
                    incident_post_id=incident_post_id,
                    callback_url=callback_url,
                )
            ]
        return ActionResult(
            message=f"Создана задача {ticket.jira_issue_key}.",
            update_attachments=update_attachments,
        )

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
            thread_messages, participants, summary_author = await self._postmortem_thread_context(
                root_post,
                reacted_by_user_id=requested_by_user_id,
            )
        except ApiError as exc:
            log.error(
                "summary.failed",
                mattermost_post_id=root_post.id,
                source=source,
                error=str(exc),
            )
            return ActionResult(message=SUMMARY_FAILED_TEXT)

        ok = await self._publish_thread_summary(
            root_post_id=root_post.id,
            channel_id=root_post.channel_id,
            thread_url=self.mattermost.permalink(root_post.id),
            participants=participants,
            postmortem_author=summary_author,
            transcript=format_thread_transcript(thread_messages),
            requested_by_user_id=requested_by_user_id,
            thread_id_key="mattermost_alert_post_id",
            event="mattermost.alert_thread.summary_published",
        )
        if not ok:
            return ActionResult(message=SUMMARY_FAILED_TEXT)
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
        validity_label: str | None = None,
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
        if ticket is not None and ticket.postmortem_comment_added:
            # Already finalized: never regenerate the postmortem (the comment is
            # additive — a second trigger would duplicate it). A validity emoji on
            # a closed incident simply flips the Jira "Валидность" field.
            if validity_label is not None and ticket.validity_label != validity_label:
                await self._set_incident_validity(ticket, validity_label)
            await self._mark_incident_post_completed(post.id)
            return ConfirmationResult(
                status=ConfirmationStatus.INCIDENT_ENDED,
                message="Incident already finalized; postmortem left unchanged.",
                jira_issue_url=ticket.jira_issue_url,
                incident_message_url=ticket.incident_message_url,
            )
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
            # No LLM → no postmortem, but a validity emoji must still write Jira
            # (the alert-channel path does, so the incident path must not silently
            # drop it). _ensure_postmortem_jira_issue, which normally applies it,
            # is never reached on this early-return branch.
            if validity_label is not None and ticket is not None and ticket.jira_issue_key:
                await self._set_incident_validity(ticket, validity_label)
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
            validity_label=validity_label,
        )
        # Turn the title green once the incident has ended, even if the postmortem
        # itself failed — the end time is already set in Jira, so leaving it red
        # would misrepresent a closed incident.
        ended = result.status == ConfirmationStatus.INCIDENT_ENDED or (
            end_result is not None and end_result.status == ConfirmationStatus.INCIDENT_ENDED
        )
        if ended:
            await self._mark_incident_post_completed(post.id)
        return result

    async def _set_incident_validity(self, ticket: AlertTicket, validity_label: str) -> None:
        """Push an explicit validity onto a closed incident's Jira issue (best-effort).

        Used when a validity emoji lands on an already-finalized incident: the
        postmortem is left untouched, but the Jira field is updated and the same
        templated "validity changed" notice is posted in the incident thread.
        """
        if ticket.jira_issue_key is None:
            return
        try:
            await self.jira.set_validity(ticket.jira_issue_key, validity_label)
        except ApiError as exc:
            log.warning(
                "incident.validity.update_failed",
                mattermost_post_id=ticket.mattermost_post_id,
                jira_issue_key=ticket.jira_issue_key,
                validity_label=validity_label,
                error=str(exc),
            )
            return
        self.repository.set_validity_label(ticket.mattermost_post_id, validity_label)
        log.info(
            "incident.validity.updated",
            mattermost_post_id=ticket.mattermost_post_id,
            jira_issue_key=ticket.jira_issue_key,
            validity_label=validity_label,
            source="incident_finalized",
        )
        if ticket.incident_post_id:
            await self._post_incident_thread_reply(
                ticket.incident_post_id,
                channel_id=self.settings.mattermost_incident_channel_id,
                message=format_thread_validity_changed(validity_label=validity_label),
                event="mattermost.incident_thread.validity_notice_published",
                props={
                    "jira_issue_key": ticket.jira_issue_key,
                    "validity_label": validity_label,
                },
            )

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
            props = dict(post.props or {})
            attachments = props.get("attachments")
            if not isinstance(attachments, list) or not attachments:
                return
            info_block = attachments[0]
            if not isinstance(info_block, dict):
                return
            new_text = mark_incident_message_completed(info_block.get("text", ""))
            if new_text == info_block.get("text", ""):
                return
            props["attachments"] = [
                {**info_block, "text": new_text, "color": INCIDENT_DONE_COLOR},
                *[{**a, "color": INCIDENT_DONE_COLOR} for a in attachments[1:]],
            ]
            await self.mattermost.update_post(ticket.incident_post_id, props=props)
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
        validity_label: str | None = None,
    ) -> ConfirmationResult:
        llm = self.llm
        assert llm is not None
        incident_thread_url = self.mattermost.permalink(root_post.id)
        ticket = existing_ticket
        summary_base_props = self._summary_base_props(
            "mattermost_incident_post_id", root_post.id, reacted_by_user_id
        )
        summary_event = "mattermost.incident_thread.postmortem_published"
        placeholder_id: str | None = None
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
            # Placeholder up front so the whole wait (postmortem LLM + Jira calls +
            # summary LLM) shows "Генерация саммари…"; it is edited into the final
            # reply below, or into the error notice if the Jira step fails.
            placeholder_id = await self._post_summary_placeholder(
                root_post_id=root_post.id,
                channel_id=root_post.channel_id,
                base_props=summary_base_props,
                event=summary_event,
            )
            prompt = build_incident_report_prompt(
                thread_url=incident_thread_url,
                participants=participants,
                postmortem_author=postmortem_author,
                transcript=transcript,
                max_chars=self.settings.llm_thread_max_chars,
                template=self._resolve_prompt_template(_PROMPT_KEY_POSTMORTEM),
            )
            await self._set_summary_status(
                placeholder_id,
                root_post.id,
                message="⏳ Шаг 1/3: генерирую постмортем…",
                base_props=summary_base_props,
                event=f"{summary_event}.status",
            )
            report = await llm.generate_postmortem(prompt)
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
            await self._set_summary_status(
                placeholder_id,
                root_post.id,
                message="⏳ Шаг 2/3: отправляю постмортем в Jira…",
                base_props=summary_base_props,
                event=f"{summary_event}.status",
            )
            ticket = await self._ensure_postmortem_jira_issue(
                ticket,
                summary=summary,
                description=description,
                ended_at=ended_at,
                reacted_by_user_id=reacted_by_user_id,
                validity_label=validity_label,
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
            self.repository.mark_postmortem_comment_added(ticket.mattermost_post_id)
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
            # Edit the placeholder into the failure notice so it never stays stuck
            # on "Генерация саммари…".
            await self._finalize_thread_summary_reply(
                placeholder_id,
                root_post.id,
                channel_id=root_post.channel_id,
                message=(
                    "Не удалось сгенерировать или отправить постмортем в Jira. "
                    "Можно повторить реакцию позже."
                ),
                base_props=summary_base_props,
                event="mattermost.incident_thread.postmortem_failed_notice",
            )
            return ConfirmationResult(
                status=ConfirmationStatus.ERROR,
                message="Postmortem generation failed; please retry.",
            )

        # The thread gets the fact-based incident summary (own LLM prompt); the
        # Jira postmortem above is generated separately and stays untouched.
        await self._set_summary_status(
            placeholder_id,
            root_post.id,
            message="⏳ Шаг 3/3: генерирую саммари…",
            base_props=summary_base_props,
            event=f"{summary_event}.status",
        )
        await self._generate_and_finalize_summary(
            placeholder_id=placeholder_id,
            root_post_id=root_post.id,
            channel_id=root_post.channel_id,
            base_props=summary_base_props,
            thread_url=incident_thread_url,
            participants=participants,
            postmortem_author=postmortem_author,
            transcript=transcript,
            event=summary_event,
        )
        # Reached only after the Jira postmortem succeeded, so the link is always
        # present: announce closure in a standalone green box (replaces the old
        # in-summary footer). Only the final-status completion flow gets here.
        await self._create_thread_summary_reply(
            root_post.id,
            channel_id=root_post.channel_id,
            message=format_incident_closed_notice(
                jira_issue_title=summary,
                jira_issue_url=ticket.jira_issue_url,
            ),
            base_props=summary_base_props,
            event="mattermost.incident_thread.incident_closed_notice",
            color=INCIDENT_DONE_COLOR,
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

    async def _set_time_to_fix(
        self, issue_key: str, ticket: AlertTicket, ended_at: datetime
    ) -> None:
        """Best-effort: write the incident duration (minutes) to the Jira field.

        Time to fix is a secondary derived field, so it must never break incident
        closure: a misconfigured field id raises a non-retryable ``ApiError`` that
        is logged, not propagated (unlike ``set_end_time``). The persisted start
        may come back naive (SQLite drops the tz); since it was written as a
        runtime-tz instant, a naive value is localized to the runtime timezone —
        not assumed UTC — so the duration matches wall-clock reality.
        """
        if not self.settings.jira_time_to_fix_field:
            return
        start = ticket.mattermost_message_created_at
        if start is None:
            log.warning(
                "jira.time_to_fix.skipped_no_start",
                mattermost_post_id=ticket.mattermost_post_id,
                jira_issue_key=issue_key,
            )
            return
        tz = runtime_timezone()
        start = start if start.tzinfo else start.replace(tzinfo=tz)
        ended = ended_at if ended_at.tzinfo else ended_at.replace(tzinfo=tz)
        if ended <= start:
            log.warning(
                "jira.time_to_fix.skipped_non_positive",
                mattermost_post_id=ticket.mattermost_post_id,
                jira_issue_key=issue_key,
            )
            return
        minutes = round((ended - start).total_seconds() / 60)
        try:
            await self.jira.set_time_to_fix(issue_key, minutes)
        except ApiError as exc:
            log.warning(
                "jira.time_to_fix.failed",
                mattermost_post_id=ticket.mattermost_post_id,
                jira_issue_key=issue_key,
                error=str(exc),
            )

    async def _apply_postmortem_validity(
        self, post_id: str, issue_key: str, *, validity_label: str | None
    ) -> None:
        """Write the incident's validity onto its Jira issue at finalize time.

        ``validity_label`` is the choice made *now* (a Ложный/Ожидаемый emoji in
        the incident thread) and wins over any earlier value. With no choice and
        none recorded, default to Валидный; an earlier explicit Ложный/Ожидаемый
        is left untouched (already pushed when it was picked).
        """
        if validity_label is not None:
            await self.jira.set_validity(issue_key, validity_label)
            self.repository.set_validity_label(post_id, validity_label)
            return
        ticket = self.repository.get_by_post_id(post_id)
        if ticket is None or ticket.validity_label is None:
            await self.jira.set_valid_incident(issue_key, True)

    async def _ensure_postmortem_jira_issue(
        self,
        ticket: AlertTicket,
        *,
        summary: str,
        description: str,
        ended_at: datetime,
        reacted_by_user_id: str,
        validity_label: str | None = None,
    ) -> AlertTicket:
        if ticket.jira_issue_key is not None:
            if not ticket.valid_incident or validity_label is not None:
                # Validity and confirmation are independent axes: only default to
                # Валидный when nobody picked a validity. An explicit Ложный/
                # Ожидаемый (validity_label) must survive — and a validity emoji
                # on an already-confirmed incident must still update the field.
                await self._apply_postmortem_validity(
                    ticket.mattermost_post_id,
                    ticket.jira_issue_key,
                    validity_label=validity_label,
                )
            if not ticket.valid_incident:
                await self.jira.set_end_time(ticket.jira_issue_key, ended_at)
                await self._set_time_to_fix(ticket.jira_issue_key, ticket, ended_at)
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
        await self._apply_postmortem_validity(
            ticket.mattermost_post_id, issue.key, validity_label=validity_label
        )
        await self.jira.set_end_time(issue.key, ended_at)
        await self._set_time_to_fix(issue.key, ticket, ended_at)
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
                author_display=display_by_user_id[post.user_id],
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

    @staticmethod
    def _box_thread_reply(message: str, props: dict | None, color: str) -> tuple[str, dict | None]:
        """Render a plain bot notice as a boxed attachment instead of a bare message.

        Skipped when the caller already supplies ``attachments`` (interactive
        cards keep their own layout, and any ``@mention`` in ``message`` must
        stay in the message text to actually notify). ``fallback`` carries the
        text into push notifications / channel previews.
        """
        if not message or (props or {}).get("attachments"):
            return message, props
        boxed = {
            **(props or {}),
            "attachments": [{"fallback": message, "color": color, "text": message}],
        }
        return "", boxed

    async def _create_thread_summary_reply(
        self,
        post_id: str,
        *,
        channel_id: str,
        message: str,
        base_props: dict,
        event: str,
        color: str = NOTICE_ATTACHMENT_COLOR,
    ) -> str | None:
        """Create a boxed thread reply and return its id (None if the post fails)."""
        boxed_message, props = self._box_thread_reply(message, dict(base_props), color)
        try:
            reply = await self.mattermost.create_post(
                channel_id=channel_id,
                message=boxed_message,
                root_id=post_id,
                props=props,
            )
        except ApiError as exc:
            log.warning(
                "mattermost.thread.summary_reply_failed",
                mattermost_post_id=post_id,
                event_kind=event,
                error=str(exc),
            )
            return None
        log.info(event, mattermost_post_id=post_id, reply_post_id=reply.id)
        return reply.id

    async def _edit_summary_reply(
        self,
        reply_id: str,
        post_id: str,
        *,
        message: str,
        base_props: dict,
        event: str,
    ) -> None:
        """Re-render an existing summary reply in place (status, stream, finale).

        ``update_post`` replaces props wholesale, so re-box from ``base_props`` to
        keep the thread-routing keys. Best-effort: a failed edit is logged, never
        raised — callers in the LLM stream loop must not let an edit blip restart
        generation.
        """
        boxed_message, props = self._box_thread_reply(
            message, dict(base_props), NOTICE_ATTACHMENT_COLOR
        )
        try:
            await self.mattermost.update_post(reply_id, message=boxed_message, props=props)
        except ApiError as exc:
            log.warning(
                "mattermost.thread.summary_reply_update_failed",
                reply_post_id=reply_id,
                event_kind=event,
                error=str(exc),
            )
            return
        log.info(event, mattermost_post_id=post_id, reply_post_id=reply_id)

    async def _set_summary_status(
        self,
        reply_id: str | None,
        post_id: str,
        *,
        message: str,
        base_props: dict,
        event: str,
    ) -> None:
        """Edit the placeholder to a transient progress status (no-op if missing)."""
        if reply_id is None:
            return
        await self._edit_summary_reply(
            reply_id, post_id, message=message, base_props=base_props, event=event
        )

    async def _finalize_thread_summary_reply(
        self,
        reply_id: str | None,
        post_id: str,
        *,
        channel_id: str,
        message: str,
        base_props: dict,
        event: str,
    ) -> None:
        """Swap the "Генерация саммари…" placeholder for the final reply.

        If the placeholder was never created (post failed), fall back to a fresh
        reply so the summary still lands in the thread.
        """
        if reply_id is None:
            await self._create_thread_summary_reply(
                post_id,
                channel_id=channel_id,
                message=message,
                base_props=base_props,
                event=event,
            )
            return
        await self._edit_summary_reply(
            reply_id, post_id, message=message, base_props=base_props, event=event
        )

    @staticmethod
    def _summary_base_props(
        thread_id_key: str, root_post_id: str, requested_by_user_id: str
    ) -> dict:
        return {
            thread_id_key: root_post_id,
            "summary_requested_by_user_id": requested_by_user_id,
        }

    async def _post_summary_placeholder(
        self,
        *,
        root_post_id: str,
        channel_id: str,
        base_props: dict,
        event: str,
    ) -> str | None:
        """Post the "Генерация саммари…" placeholder up front so the wait has
        feedback; the id is later handed to ``_finalize_summary_reply``."""
        return await self._create_thread_summary_reply(
            root_post_id,
            channel_id=channel_id,
            message=SUMMARY_PENDING_TEXT,
            base_props=base_props,
            event=f"{event}.pending",
        )

    def _make_summary_stream_callback(
        self,
        *,
        reply_id: str,
        post_id: str,
        base_props: dict,
        event: str,
    ) -> StreamProgress:
        """Throttled progress callback: live-edit the placeholder as the LLM streams.

        Receives the cumulative text per delta and edits at most every
        ``llm_stream_edit_interval_seconds`` OR every ``llm_stream_edit_min_chars``
        new characters. ``last_edit_time`` is seeded to "now" so the first stream
        edit respects the interval after any preceding status edit. Edits go through
        ``_edit_summary_reply``, which swallows ``ApiError`` — the callback must
        never raise, or an edit blip would restart the whole generation via retry.
        """
        interval = self.settings.llm_stream_edit_interval_seconds
        min_chars = self.settings.llm_stream_edit_min_chars
        state = {"last_edit_time": perf_counter(), "last_len": 0}

        async def on_progress(text: str) -> None:
            now = perf_counter()
            # Shrink ⇒ a retry restarted the stream: force a re-render so the stale
            # longer text is overwritten and the char baseline resets.
            shrank = len(text) < state["last_len"]
            due = (now - state["last_edit_time"]) >= interval or (
                len(text) - state["last_len"]
            ) >= min_chars
            if not shrank and not due:
                return
            await self._edit_summary_reply(
                reply_id,
                post_id,
                message=format_thread_summary_streaming(text),
                base_props=base_props,
                event=f"{event}.streaming",
            )
            state["last_edit_time"] = now
            state["last_len"] = len(text)

        return on_progress

    async def _generate_and_finalize_summary(
        self,
        *,
        placeholder_id: str | None,
        root_post_id: str,
        channel_id: str,
        base_props: dict,
        thread_url: str,
        participants: list[str],
        postmortem_author: str,
        transcript: str,
        event: str,
    ) -> bool:
        """Generate the incident-report summary and edit the placeholder into the
        final reply (or an error notice). Returns True on success.

        When a placeholder exists, the summary is streamed into it live (throttled);
        the final edit always overwrites the streaming state with the clean format.
        Callers must ensure ``self.llm`` is configured.
        """
        llm = self.llm
        assert llm is not None
        prompt = build_incident_report_prompt(
            thread_url=thread_url,
            participants=participants,
            postmortem_author=postmortem_author,
            transcript=transcript,
            max_chars=self.settings.llm_thread_max_chars,
            template=self._resolve_prompt_template(_PROMPT_KEY_SUMMARY),
        )
        on_progress = (
            self._make_summary_stream_callback(
                reply_id=placeholder_id,
                post_id=root_post_id,
                base_props=base_props,
                event=event,
            )
            if placeholder_id is not None
            else None
        )
        try:
            summary = await llm.generate_summary(prompt, on_progress=on_progress)
        except ApiError as exc:
            log.error("summary.failed", mattermost_post_id=root_post_id, error=str(exc))
            await self._finalize_thread_summary_reply(
                placeholder_id,
                root_post_id,
                channel_id=channel_id,
                message=SUMMARY_FAILED_TEXT,
                base_props=base_props,
                event=f"{event}.failed",
            )
            return False
        await self._finalize_thread_summary_reply(
            placeholder_id,
            root_post_id,
            channel_id=channel_id,
            message=format_thread_summary_reply(summary),
            base_props=base_props,
            event=event,
        )
        return True

    async def _publish_thread_summary(
        self,
        *,
        root_post_id: str,
        channel_id: str,
        thread_url: str,
        participants: list[str],
        postmortem_author: str,
        transcript: str,
        requested_by_user_id: str,
        thread_id_key: str,
        event: str,
    ) -> bool:
        """Post a pending placeholder, generate the incident-report summary, then
        edit the placeholder into the final reply. Returns True on success.

        Used by the on-demand Summary button, where there is no preceding work to
        cover; the completion flow posts the placeholder earlier itself.
        """
        base_props = self._summary_base_props(thread_id_key, root_post_id, requested_by_user_id)
        placeholder_id = await self._post_summary_placeholder(
            root_post_id=root_post_id,
            channel_id=channel_id,
            base_props=base_props,
            event=event,
        )
        return await self._generate_and_finalize_summary(
            placeholder_id=placeholder_id,
            root_post_id=root_post_id,
            channel_id=channel_id,
            base_props=base_props,
            thread_url=thread_url,
            participants=participants,
            postmortem_author=postmortem_author,
            transcript=transcript,
            event=event,
        )

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
        await self._set_time_to_fix(ticket.jira_issue_key, ticket, ended_at)

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
            assert ticket is not None
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
                incident_message_url=ticket.incident_message_url,
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

    async def _ensure_jira_issue(self, ticket: AlertTicket, is_repeat: bool = False) -> None:
        """Create the Jira issue for a firing alert and post the "Создана задача"
        reply once (guarded by the existing key). Resolved alerts never reach
        here — they are skipped in ``handle_alert_post`` before a ticket exists —
        so the on-call ``MATTERMOST_DUTY_MENTION`` ping fires only for firing
        alerts, above the boxed notice.

        For ``is_repeat=True`` (a repeat firing of an open episode) the duty ping
        and the duty cheat-sheet are suppressed: ``_handle_expected_repeat`` runs
        right after and auto-marks the repeat as expected, so no on-call action is
        required and the reminders would only be noise.
        """
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
            duty_mention = None if is_repeat else self.settings.mattermost_duty_mention
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
                    mention=duty_mention,
                )
            else:
                await self._post_alert_thread_reply(
                    ticket.mattermost_post_id,
                    channel_id=ticket.mattermost_channel_id,
                    message=issue_message,
                    event="mattermost.alert_thread.issue_notice_published",
                    props={"jira_issue_key": issue.key},
                    mention=duty_mention,
                )
            if self.settings.duty_help_enabled and not is_repeat:
                await self._post_alert_thread_reply(
                    ticket.mattermost_post_id,
                    channel_id=ticket.mattermost_channel_id,
                    message=format_alert_duty_help(
                        incident_emoji=self.settings.mattermost_incident_reaction_name,
                        false_emoji=self.settings.mattermost_false_incident_reaction_name,
                        expected_emoji=self.settings.mattermost_expected_incident_reaction_name,
                        summary_emoji=self.settings.mattermost_summary_reaction_name,
                    ),
                    event="mattermost.alert_thread.duty_help_published",
                    color=DUTY_HELP_ATTACHMENT_COLOR,
                )
        except ApiError as exc:
            self.repository.mark_jira_create_failed(ticket.mattermost_post_id, str(exc))
            log.error(
                "jira.issue.create_failed",
                mattermost_post_id=ticket.mattermost_post_id,
                error=str(exc),
            )

    async def _handle_expected_repeat(self, ticket: AlertTicket, root: AlertTicket) -> None:
        """Annotate a repeat firing as an expected duplicate of an open episode.

        Idempotent steps (reaction, validity, description) run on every delivery;
        the non-idempotent Jira "is child of" link and the "Прилинковано к" notice
        are guarded by the persisted ``expected_repeat_linked`` flag. The flag is
        set only after the link call returns, so a link failure is retried on the
        next delivery rather than silently lost.
        """
        if not ticket.jira_issue_key:
            return  # Jira issue creation failed upstream; nothing to annotate yet.

        try:
            await self.mattermost.add_reaction(
                ticket.mattermost_post_id,
                self.settings.mattermost_expected_incident_reaction_name,
            )
        except ApiError as exc:
            log.warning(
                "mattermost.expected_reaction.failed",
                mattermost_post_id=ticket.mattermost_post_id,
                error=str(exc),
            )

        try:
            await self.jira.set_validity(ticket.jira_issue_key, VALID_INCIDENT_EXPECTED_VALUE)
            self.repository.set_validity_label(
                ticket.mattermost_post_id, VALID_INCIDENT_EXPECTED_VALUE
            )
        except ApiError as exc:
            log.warning(
                "jira.expected_validity.failed",
                jira_issue_key=ticket.jira_issue_key,
                error=str(exc),
            )

        try:
            description = (
                build_jira_description(
                    ticket_to_post(ticket),
                    message_url=ticket.mattermost_message_url,
                    channel_name=ticket.mattermost_channel_name,
                )
                + "\n"
                + build_expected_alert_block(
                    root_message_url=root.mattermost_message_url,
                    root_issue_key=root.jira_issue_key,
                    root_issue_url=root.jira_issue_url,
                )
            )
            await self.jira.set_description(ticket.jira_issue_key, description)
        except ApiError as exc:
            log.warning(
                "jira.expected_description.failed",
                jira_issue_key=ticket.jira_issue_key,
                error=str(exc),
            )

        if ticket.expected_repeat_linked:
            return
        if not root.jira_issue_key:
            log.warning(
                "jira.expected_link.skipped_root_without_issue",
                mattermost_post_id=ticket.mattermost_post_id,
            )
            return
        try:
            await self.jira.link_child_of(ticket.jira_issue_key, root.jira_issue_key)
        except ApiError as exc:
            # Linking is an explicit requirement; leave the flag false so the next
            # delivery retries instead of permanently losing the link.
            log.error(
                "jira.expected_link.failed",
                jira_issue_key=ticket.jira_issue_key,
                root_issue_key=root.jira_issue_key,
                error=str(exc),
            )
            return
        self.repository.mark_expected_repeat_linked(ticket.mattermost_post_id)
        await self._post_alert_thread_reply(
            ticket.mattermost_post_id,
            channel_id=ticket.mattermost_channel_id,
            message=format_thread_linked_to_root(
                root_issue_key=root.jira_issue_key,
                root_issue_url=root.jira_issue_url,
                root_message_url=root.mattermost_message_url,
            ),
            event="mattermost.alert_thread.linked_to_root",
            props={"jira_issue_key": ticket.jira_issue_key},
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
        issue = stub_jira_issue(self.settings, ticket.mattermost_post_id)
        log.info(
            "jira.issue.create_stubbed",
            mattermost_post_id=ticket.mattermost_post_id,
            jira_issue_key=issue.key,
            jira_issue_url=issue.url,
        )
        return issue

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
        color: str = NOTICE_ATTACHMENT_COLOR,
        mention: str | None = None,
    ) -> None:
        """Reply in the alert thread; best-effort, never fails the caller.

        ``mention`` (e.g. an on-call ``@group``) is placed as bare text above
        the boxed notice so the ping actually fires — attachment text does not
        notify.
        """
        message, props = self._box_thread_reply(message, props, color)
        if mention:
            message = f"{mention}\n{message}" if message else mention
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
        # Incident details go into a gray block above the forwarded alert block.
        info_text = format_incident_message(
            cast(Any, ticket),
            confirmed_by=mention_from_display(confirmed_by_display),
            confirmed_at=confirmed_at,
            include_alert_text=not alert_attachments,
        )
        info_block = {
            "fallback": "Инцидент открыт",
            "color": INCIDENT_OPEN_COLOR,
            "text": info_text,
        }
        props = {
            "mattermost_alert_post_id": ticket.mattermost_post_id,
            "jira_issue_key": ticket.jira_issue_key,
            "confirmed_by_user_id": confirmed_by_user_id,
            "attachments": [info_block, *alert_attachments],
        }
        incident_post = await self.mattermost.create_post(
            channel_id=self.settings.mattermost_incident_channel_id,
            message="",
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
        if self._interactive_controls_enabled() and ticket.jira_issue_key:
            callback_url = self._action_callback_url()
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
        # The alert thread's cheat-sheet covers firing reactions; the incident
        # thread needs its own (validity = close + postmortem, summary emoji).
        if self.settings.duty_help_enabled:
            await self._post_incident_thread_reply(
                incident_post.id,
                channel_id=self.settings.mattermost_incident_channel_id,
                message=self._incident_duty_help(),
                event="mattermost.incident_thread.duty_help_published",
                color=DUTY_HELP_ATTACHMENT_COLOR,
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
