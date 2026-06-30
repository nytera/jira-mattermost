from __future__ import annotations

from dataclasses import dataclass, replace
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
    mention_from_display,
)
from mm_jira_bot.jira import (
    STUB_ISSUE_KEY,
    VALID_INCIDENT_EXPECTED_VALUE,
    VALID_INCIDENT_FALSE_VALUE,
)
from mm_jira_bot.logging import get_logger
from mm_jira_bot.mattermost import parse_posted_event, parse_reaction_event
from mm_jira_bot.repository import AlertTicket, AlertTicketRepository
from mm_jira_bot.retry import ApiError
from mm_jira_bot.service._admin import AdminMixin
from mm_jira_bot.service._alerts import AlertMixin
from mm_jira_bot.service._incidents import IncidentMixin
from mm_jira_bot.service._jira_sync import JiraSyncMixin
from mm_jira_bot.service._postmortem import PostmortemMixin
from mm_jira_bot.service._shared import (
    ActionResult,
    SharedMixin,
    parse_post_id_from_text,
)
from mm_jira_bot.service._thread_summary import ThreadSummaryMixin

# Имя логгера держим стабильным (`mm_jira_bot.service`), несмотря на перенос модуля
# в пакет `service/` — на него завязаны тесты и настроенные логгеры.
log = get_logger("mm_jira_bot.service")

INCIDENT_END_REACTION_NAMES = {
    "white_check_mark",
    "heavy_check_mark",
    "ballot_box_with_check",
}


@dataclass(frozen=True)
class CommandResponse:
    text: str
    response_type: str = "ephemeral"


class IncidentBotService(
    SharedMixin,
    AlertMixin,
    AdminMixin,
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

    async def resolve_bot_user_id(self) -> None:
        """Auto-populate ``mattermost_bot_user_id`` from the bot token at startup.

        The token already determines identity, so ``MATTERMOST_BOT_USER_ID`` is
        optional: when unset, resolve it from ``/users/me`` and push it into both
        the service settings (hot-path checks: own-reaction ignore, ``_is_bot_post``)
        and the Mattermost client (``add_reaction`` sends ``user_id``). When set, it
        is kept as-is and preflight cross-checks it. Runs before the websocket loop
        so handlers see the right id. A failure here is fatal — the bot can't tell
        its own activity apart without an identity.
        """
        if self.settings.mattermost_bot_user_id:
            return
        bot_user_id = await self.mattermost.fetch_bot_user_id()
        if not bot_user_id:
            raise RuntimeError(
                "Could not resolve bot user id from Mattermost /users/me; "
                "set MATTERMOST_BOT_USER_ID explicitly."
            )
        self.settings = replace(self.settings, mattermost_bot_user_id=bot_user_id)
        self.mattermost.adopt_resolved_bot_user_id(bot_user_id)
        log.info("bot_user_id.resolved", bot_user_id=bot_user_id)

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
            if await self._observe_prod_artifact(posted):
                return
            if self._is_incident_channel(posted.channel_id):
                await self.handle_manual_incident_post(posted)
            else:
                await self.handle_alert_post(posted)
            return

        reaction = parse_reaction_event(event)
        if reaction:
            await self.handle_reaction(reaction)

    async def _observe_prod_artifact(self, post: MattermostPost) -> bool:
        """Read-only (shadow) mode: adopt real prod artifacts from a prod-bot post.

        A post in the **real** alert/incident channel that carries
        ``props.mattermost_alert_post_id`` is a prod-bot artifact: only the prod
        bot sets that prop (``_post_alert_thread_reply`` /
        ``_publish_incident_message_if_needed``), humans can't set props, and the
        shadow strips it from its own audit copies. We correlate it to the shadow's
        ticket and adopt:

        - the real Jira key (replacing the read-only ``ADS-TEST-…`` stub), so the
          audit mirror shows the real link instead of the stub;
        - for the incident **root** post, the real prod incident post id, so a later
          ✅ on that real post resolves to this ticket and the shadow runs its own
          postmortem into the audit thread.

        Returns ``True`` when the post is a prod artifact and must not fall through
        to the normal alert/incident handlers (which would only drop it).
        """
        if not self.settings.read_only_mode:
            return False
        alert_post_id = (post.props or {}).get("mattermost_alert_post_id")
        if not isinstance(alert_post_id, str) or not alert_post_id:
            return False
        # Positive channel gate: ONLY the real alert/incident channels. This is the
        # load-bearing guard for the zero-prod-impact invariant — it excludes test,
        # audit, and every other channel, so the shadow can never adopt its own
        # audit post (the prop strip is then only defense-in-depth).
        if post.channel_id not in {
            self.settings.mattermost_alert_channel_id,
            self.settings.mattermost_incident_channel_id,
        }:
            return False
        ticket = self.repository.get_by_post_id(alert_post_id)
        if ticket is None:
            # A prod artifact the shadow can't correlate (it never saw the source
            # alert). Consume it so it doesn't fall through; nothing to adopt.
            log.info(
                "readonly.adopt.no_ticket",
                mattermost_post_id=post.id,
                source_alert_post_id=alert_post_id,
            )
            return True

        await self._adopt_prod_jira_issue(ticket, (post.props or {}).get("jira_issue_key"))
        if post.channel_id == self.settings.mattermost_incident_channel_id and not post.root_id:
            await self._adopt_prod_incident_post(ticket, post.id)
        return True

    async def _adopt_prod_jira_issue(self, ticket: AlertTicket, issue_key: object) -> None:
        """Replace the read-only stub Jira key with the adopted real prod key.

        Idempotent and self-healing: adopts only while the current key is still a
        ``ADS-TEST-…`` stub, so the first prod reply carrying ``jira_issue_key``
        wins and any later one is a no-op. A missing stub (shadow hasn't created
        its own yet) is skipped — a later prod notice (every confirmed incident
        gets several) re-attempts once the stub exists.
        """
        if not isinstance(issue_key, str) or not issue_key:
            return
        if not (ticket.jira_issue_key or "").startswith(STUB_ISSUE_KEY):
            return
        issue_url = f"{self.settings.jira_base_url}/browse/{issue_key}"
        self.repository.replace_jira_issue(ticket.mattermost_post_id, issue_key, issue_url)
        log.info(
            "readonly.adopt.jira_issue",
            mattermost_post_id=ticket.mattermost_post_id,
            jira_issue_key=issue_key,
        )
        # The adoption note is the ONE place the real link surfaces in the audit
        # channel (the original "Создана задача" reply keeps showing the stub).
        await self._post_alert_thread_reply(
            ticket.mattermost_post_id,
            channel_id=ticket.mattermost_channel_id,
            message=f"Усыновлён реальный Jira с прода: [{issue_key}]({issue_url})",
            event="readonly.adopt.jira_issue_published",
            props={"jira_issue_key": issue_key},
        )

    async def _adopt_prod_incident_post(
        self, ticket: AlertTicket, prod_incident_post_id: str
    ) -> None:
        """Record the real prod incident post id and alias it to the shadow's own
        incident audit thread (idempotent)."""
        if ticket.prod_incident_post_id == prod_incident_post_id:
            return
        self.repository.set_prod_incident_post_id(ticket.mattermost_post_id, prod_incident_post_id)
        # If the shadow already published its own incident message, alias the prod
        # post id to that audit thread so the prod ✅'s postmortem lands there
        # rather than under a fresh anchor. Done before the note below so the note
        # itself threads correctly. Re-read fresh: the shadow's own confirm path may
        # have recorded incident_post_id concurrently (same ✅-on-alert cause)
        # since this observer snapshotted the ticket, so the snapshot can be stale.
        current = self.repository.get_by_post_id(ticket.mattermost_post_id) or ticket
        audit = getattr(self.mattermost, "audit", None)
        if current.incident_post_id and audit is not None:
            audit.adopt_alias(current.incident_post_id, prod_incident_post_id)
        log.info(
            "readonly.adopt.incident_post",
            mattermost_post_id=ticket.mattermost_post_id,
            prod_incident_post_id=prod_incident_post_id,
        )
        await self._post_incident_thread_reply(
            prod_incident_post_id,
            channel_id=self.settings.mattermost_incident_channel_id,
            message="Инцидент усыновлён с прода — отслеживаю закрытие.",
            event="readonly.adopt.incident_post_published",
            color=INCIDENT_OPEN_COLOR,
        )

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
        in_incident_channel = self._is_incident_channel(post.channel_id)
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

        if not self._is_alert_channel(post.channel_id):
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

        if not self._is_alert_channel(post.channel_id):
            return CommandResponse(text="This message is not in the configured alerts channel.")

        ticket = self.repository.get_by_post_id(post_id)
        if ticket is None or ticket.jira_issue_key is None:
            await self.handle_alert_post(post)

        result = await self.confirm_incident(
            post_id, confirmed_by_user_id=user_id, source="slash_command"
        )
        return CommandResponse(text=result.message)

    async def _announce_issue_to_ops(
        self, ticket: AlertTicket, issue: JiraIssue, *, source: str
    ) -> None:
        """Best-effort: post every newly created Jira issue to the ops channel with
        a link back to its source thread/message. Shares ``MATTERMOST_OPS_CHANNEL_ID``
        with the error-alert feed; skips stub issues (read-only mode, where every
        issue is a stub) and never breaks issue creation (a failed post is logged,
        not propagated).
        """
        channel_id = self.settings.mattermost_ops_channel_id
        if not channel_id or self.settings.read_only_mode:
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
