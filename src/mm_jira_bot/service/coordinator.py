from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mm_jira_bot.actions import (
    INCIDENT_OPEN_COLOR,
    NOTICE_ATTACHMENT_COLOR,
    OPS_ISSUE_CREATED_COLOR,
    alert_action_callback_url,
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
    format_ops_issue_created,
    is_resolved_alert,
    mention_from_display,
)
from mm_jira_bot.jira import (
    VALID_INCIDENT_EXPECTED_VALUE,
    VALID_INCIDENT_FALSE_VALUE,
)
from mm_jira_bot.logging import get_logger
from mm_jira_bot.mattermost import parse_posted_event, parse_reaction_event
from mm_jira_bot.repository import AlertTicket, AlertTicketRepository
from mm_jira_bot.retry import ApiError
from mm_jira_bot.service._alerts import AlertMixin
from mm_jira_bot.service._incidents import IncidentMixin
from mm_jira_bot.service._jira_sync import JiraSyncMixin
from mm_jira_bot.service._postmortem import PostmortemMixin
from mm_jira_bot.service._shared import (
    ActionResult,
    SharedMixin,
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


@dataclass(frozen=True)
class CommandResponse:
    text: str
    response_type: str = "ephemeral"


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
    SharedMixin,
    AlertMixin,
    IncidentMixin,
    JiraSyncMixin,
    PostmortemMixin,
    ThreadSummaryMixin,
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
