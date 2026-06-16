from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from mm_jira_bot.config import Settings
from mm_jira_bot.domain import (
    ConfirmationResult,
    ConfirmationStatus,
    MattermostPost,
    ReactionEvent,
    backend_now,
    datetime_from_mattermost_ms,
)
from mm_jira_bot.formatting import (
    format_incident_message,
    format_thread_issue_created,
    format_thread_status_changed,
)
from mm_jira_bot.logging import log_event
from mm_jira_bot.mattermost import parse_posted_event, parse_reaction_event
from mm_jira_bot.repository import AlertTicket, AlertTicketRepository, ticket_to_post
from mm_jira_bot.retry import ApiError

logger = logging.getLogger(__name__)

POST_ID_PATTERN = re.compile(r"(?:^|/)(?:_redirect/)?pl/([a-z0-9]{20,32})(?:$|[/?#])")
BARE_POST_ID_PATTERN = re.compile(r"^[a-z0-9]{20,32}$")


@dataclass(frozen=True)
class CommandResponse:
    text: str
    response_type: str = "ephemeral"


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
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.mattermost = mattermost_client
        self.jira = jira_client

    async def handle_websocket_event(self, event: dict) -> None:
        posted = parse_posted_event(event)
        if posted:
            await self.handle_alert_post(posted)
            return

        reaction = parse_reaction_event(event)
        if reaction:
            await self.handle_reaction(reaction)

    async def handle_alert_post(self, post: MattermostPost) -> AlertTicket | None:
        if post.channel_id != self.settings.mattermost_alert_channel_id:
            log_event(
                logger,
                logging.INFO,
                "mattermost.post.skipped_non_alert_channel",
                mattermost_post_id=post.id,
                mattermost_channel_id=post.channel_id,
            )
            return None

        if post.user_id == self.settings.mattermost_bot_user_id:
            log_event(
                logger,
                logging.INFO,
                "mattermost.post.skipped_bot_message",
                mattermost_post_id=post.id,
            )
            return None

        channel_name = post.channel_name or await self.mattermost.get_channel_name(
            post.channel_id
        )
        message_url = self.mattermost.permalink(post.id)
        ticket, created = self.repository.create_or_get_alert(
            post, message_url=message_url, channel_name=channel_name
        )
        log_event(
            logger,
            logging.INFO,
            "mattermost.alert.received",
            mattermost_post_id=post.id,
            created=created,
        )

        if not created and ticket.jira_issue_key:
            log_event(
                logger,
                logging.INFO,
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

    async def handle_reaction(
        self, reaction: ReactionEvent
    ) -> ConfirmationResult:
        log_event(
            logger,
            logging.INFO,
            "mattermost.reaction.received",
            mattermost_post_id=reaction.post_id,
            emoji_name=reaction.emoji_name,
            user_id=reaction.user_id,
        )
        if reaction.emoji_name != self.settings.mattermost_incident_reaction_name:
            return ConfirmationResult(
                status=ConfirmationStatus.IGNORED,
                message="Reaction ignored: not configured incident reaction.",
            )

        post = await self.mattermost.get_post(reaction.post_id)
        if post.channel_id != self.settings.mattermost_alert_channel_id:
            log_event(
                logger,
                logging.INFO,
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

        return await self.confirm_incident(
            reaction.post_id,
            confirmed_by_user_id=reaction.user_id,
            confirmed_at=datetime_from_mattermost_ms(reaction.create_at),
            source="reaction",
        )

    async def handle_slash_command(self, *, user_id: str, text: str) -> CommandResponse:
        log_event(
            logger,
            logging.INFO,
            "mattermost.slash_command.received",
            user_id=user_id,
            text=text,
        )
        post_id = parse_post_id_from_text(text)
        if post_id is None:
            return CommandResponse(
                text=(
                    "Invalid link. Use `/incident <mattermost_message_link>` "
                    "with a Mattermost permalink to an alert message."
                )
            )

        try:
            post = await self.mattermost.get_post(post_id)
        except ApiError as exc:
            log_event(
                logger,
                logging.ERROR,
                "mattermost.slash_command.post_lookup_failed",
                mattermost_post_id=post_id,
                error=str(exc),
            )
            return CommandResponse(text=f"Could not read Mattermost post `{post_id}`.")

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
            log_event(
                logger,
                logging.WARNING,
                "incident.confirmation.no_ticket",
                mattermost_post_id=post_id,
                source=source,
            )
            return ConfirmationResult(
                status=ConfirmationStatus.NOT_FOUND,
                message=f"No Jira issue mapping found for Mattermost post `{post_id}`.",
            )

        if ticket.jira_issue_key is None:
            self.repository.mark_pending_confirmation(
                post_id, confirmed_by_user_id, confirmed_at
            )
            log_event(
                logger,
                logging.INFO,
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
            log_event(
                logger,
                logging.INFO,
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

        self.repository.mark_confirmation_started(
            post_id, confirmed_by_user_id, confirmed_at
        )
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
            await self._update_jira_for_confirmation(
                ticket, confirmed_by=confirmed_by_display
            )
            self.repository.mark_confirmed(
                post_id, user_id=confirmed_by_user_id, confirmed_at=confirmed_at
            )
        except ApiError as exc:
            self.repository.mark_confirmation_failed(post_id, str(exc))
            log_event(
                logger,
                logging.ERROR,
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
        log_event(
            logger,
            logging.INFO,
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

    async def _ensure_jira_issue(self, ticket: AlertTicket) -> None:
        if ticket.jira_issue_key:
            return
        post = ticket_to_post(ticket)
        try:
            issue = await self.jira.create_issue(
                post,
                message_url=ticket.mattermost_message_url,
                channel_name=ticket.mattermost_channel_name,
            )
            self.repository.attach_jira_issue(
                ticket.mattermost_post_id, issue.key, issue.url
            )
            log_event(
                logger,
                logging.INFO,
                "jira.issue.created",
                mattermost_post_id=ticket.mattermost_post_id,
                jira_issue_key=issue.key,
            )
            await self._post_alert_thread_reply(
                ticket.mattermost_post_id,
                channel_id=ticket.mattermost_channel_id,
                message=format_thread_issue_created(
                    jira_issue_key=issue.key, jira_issue_url=issue.url
                ),
                event="mattermost.alert_thread.issue_notice_published",
                props={"jira_issue_key": issue.key},
            )
        except ApiError as exc:
            self.repository.mark_jira_create_failed(ticket.mattermost_post_id, str(exc))
            log_event(
                logger,
                logging.ERROR,
                "jira.issue.create_failed",
                mattermost_post_id=ticket.mattermost_post_id,
                error=str(exc),
            )

    async def _resolve_user_display(self, user_id: str) -> str:
        try:
            return await self.mattermost.get_user_display_name(user_id)
        except ApiError as exc:
            log_event(
                logger,
                logging.WARNING,
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
            log_event(
                logger,
                logging.WARNING,
                "mattermost.alert_thread.reply_failed",
                mattermost_post_id=post_id,
                event_kind=event,
                error=str(exc),
            )
            return
        log_event(
            logger,
            logging.INFO,
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
        message = format_incident_message(
            ticket,
            confirmed_by=confirmed_by_display,
            confirmed_at=confirmed_at,
        )
        incident_post = await self.mattermost.create_post(
            channel_id=self.settings.mattermost_incident_channel_id,
            message=message,
            props={
                "mattermost_alert_post_id": ticket.mattermost_post_id,
                "jira_issue_key": ticket.jira_issue_key,
                "confirmed_by_user_id": confirmed_by_user_id,
            },
        )
        incident_url = self.mattermost.permalink(incident_post.id)
        self.repository.set_incident_message(
            ticket.mattermost_post_id, incident_post.id, incident_url
        )
        log_event(
            logger,
            logging.INFO,
            "mattermost.incident_message.published",
            mattermost_post_id=ticket.mattermost_post_id,
            incident_post_id=incident_post.id,
        )

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
            log_event(
                logger,
                logging.INFO,
                "jira.valid_incident.synced_true",
                mattermost_post_id=ticket.mattermost_post_id,
                jira_issue_key=ticket.jira_issue_key,
            )
        else:
            await self.jira.set_valid_incident(ticket.jira_issue_key, True)
            log_event(
                logger,
                logging.INFO,
                "jira.valid_incident.updated",
                mattermost_post_id=ticket.mattermost_post_id,
                jira_issue_key=ticket.jira_issue_key,
            )

        if not ticket.jira_confirmation_comment_added:
            await self.jira.add_confirmation_comment(
                ticket.jira_issue_key,
                incident_message_url=ticket.incident_message_url,
                confirmed_by_user_id=confirmed_by,
            )
            self.repository.mark_jira_confirmation_comment_added(
                ticket.mattermost_post_id
            )
            log_event(
                logger,
                logging.INFO,
                "jira.comment.added",
                mattermost_post_id=ticket.mattermost_post_id,
                jira_issue_key=ticket.jira_issue_key,
            )

        if self.settings.jira_confirmed_status_id:
            try:
                await self.jira.transition_issue(
                    ticket.jira_issue_key, self.settings.jira_confirmed_status_id
                )
                log_event(
                    logger,
                    logging.INFO,
                    "jira.issue.transitioned",
                    mattermost_post_id=ticket.mattermost_post_id,
                    jira_issue_key=ticket.jira_issue_key,
                    transition_id=self.settings.jira_confirmed_status_id,
                )
            except ApiError as exc:
                log_event(
                    logger,
                    logging.WARNING,
                    "jira.issue.transition_failed",
                    mattermost_post_id=ticket.mattermost_post_id,
                    jira_issue_key=ticket.jira_issue_key,
                    transition_id=self.settings.jira_confirmed_status_id,
                    error=str(exc),
                )
