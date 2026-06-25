"""Инциденты (ручные и из алертов): IncidentMixin.

Полный жизненный цикл инцидента: ручной incident-post (пинг дежурного + карточка
«Создать задачу»), интерактивные кнопки/чекмарк, выставление валидности и END-
времени, подтверждение инцидента и публикация сообщения в incident-канал. Методы
вызываются собранным `IncidentBotService` (см. `coordinator.py`); state
(`settings`/`repository`/`mattermost`/`jira`/`llm`) ставит конструктор
координатора, Jira-проводка, постмортем и summary-механика живут в sibling-классах.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from mm_jira_bot.actions import (
    ACTION_CREATE_TASK,
    ACTION_END_INCIDENT,
    ACTION_EXPECTED,
    ACTION_FALSE,
    ACTION_SUMMARY,
    ACTION_VALID,
    ACTION_VALIDITY,
    DUTY_HELP_ATTACHMENT_COLOR,
    INCIDENT_DONE_COLOR,
    INCIDENT_OPEN_COLOR,
    build_incident_controls_attachment,
    build_incident_create_attachment,
)
from mm_jira_bot.domain import (
    ConfirmationResult,
    ConfirmationStatus,
    MattermostPost,
    backend_now,
)
from mm_jira_bot.formatting import (
    extract_alert_title,
    format_incident_duty_help,
    format_incident_message,
    format_thread_status_changed,
    format_thread_validity_changed,
    mark_incident_message_completed,
    mention_from_display,
)
from mm_jira_bot.jira import (
    VALID_INCIDENT_CONFIRMED_VALUE,
    VALID_INCIDENT_EXPECTED_VALUE,
    VALID_INCIDENT_FALSE_VALUE,
)
from mm_jira_bot.logging import get_logger
from mm_jira_bot.repository import AlertTicket, AlertTicketRepository, ticket_to_post
from mm_jira_bot.retry import ApiError
from mm_jira_bot.service._shared import ActionResult, _validity_action_message

if TYPE_CHECKING:
    from mm_jira_bot.config import Settings
    from mm_jira_bot.domain import JiraIssue

# Имя логгера держим стабильным (`mm_jira_bot.service`) во всех файлах пакета —
# тесты и настроенные логгеры завязаны на него, а не на `__name__` модуля.
log = get_logger("mm_jira_bot.service")


def _incident_end_message(result: ConfirmationResult) -> str:
    if result.status == ConfirmationStatus.INCIDENT_ENDED:
        return "Инцидент завершён 🏁"
    if result.status == ConfirmationStatus.ERROR:
        return "Не удалось завершить инцидент, попробуйте ещё раз."
    return result.message


class IncidentMixin:
    # State устанавливает coordinator.__init__; объявляем только то, что трогает
    # этот миксин, теми же типами, что декларирует конструктор: `settings`/
    # `repository` типизированы, остальные клиенты идут без аннотаций → `Any`.
    settings: Settings
    repository: AlertTicketRepository
    mattermost: Any
    jira: Any
    llm: Any

    if TYPE_CHECKING:
        # Стабы sibling-методов из других классов собранного IncidentBotService —
        # pyright их иначе не видит на самом миксине. Сигнатуры повторяют реальные
        # (kw-only `*` и имена параметров важны для override-совместимости).
        # --- остаются в coordinator ---
        def _interactive_controls_enabled(self) -> bool: ...

        def _action_callback_url(self) -> str: ...

        def _is_bot_post(self, post: MattermostPost) -> bool: ...

        async def _announce_issue_to_ops(
            self, ticket: AlertTicket, issue: JiraIssue, *, source: str
        ) -> None: ...

        async def _resolve_user_display(self, user_id: str) -> str: ...

        async def _post_incident_thread_reply(
            self,
            post_id: str,
            *,
            channel_id: str,
            message: str,
            event: str,
            props: dict | None = ...,
            color: str = ...,
        ) -> None: ...

        async def _alert_attachments(self, ticket: AlertTicket) -> list[dict]: ...

        async def apply_validity_label(
            self,
            post_id: str,
            *,
            validity_label: str,
            validity_set_at: datetime | None = ...,
            source: str,
        ) -> ConfirmationResult: ...

        # --- JiraSyncMixin ---
        async def _update_jira_for_confirmation(
            self, ticket: AlertTicket, *, confirmed_by: str
        ) -> None: ...

        # --- PostmortemMixin ---
        async def generate_incident_postmortem(
            self,
            root_post: MattermostPost,
            *,
            reacted_by_user_id: str,
            ended_at: datetime,
            source: str,
            existing_ticket: AlertTicket | None = ...,
            validity_label: str | None = ...,
        ) -> ConfirmationResult: ...

        async def _set_time_to_fix(
            self, issue_key: str, ticket: AlertTicket, ended_at: datetime
        ) -> None: ...

        async def _resolve_incident_end_time(
            self,
            root_post: MattermostPost,
            *,
            reacted_by_user_id: str,
            reaction_ended_at: datetime,
            ticket: AlertTicket | None,
        ) -> datetime: ...

        # --- ThreadSummaryMixin ---
        async def generate_thread_summary(
            self, alert_post: MattermostPost, *, requested_by_user_id: str, source: str
        ) -> ActionResult: ...

        # --- SharedMixin ---
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
        if post.is_system_message:
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
            await self._announce_issue_to_ops(ticket, issue, source="manual_incident")

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

    async def handle_incident_checkmark(
        self,
        post: MattermostPost,
        *,
        reacted_by_user_id: str,
        ended_at: datetime,
        source: str,
        validity_label: str | None = None,
        override_end_time: bool = False,
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

        # Prefer the real recovery time inferred by the LLM from the thread
        # chronology over the raw reaction timestamp, and feed that single value
        # to every downstream END / Time-to-Fix write (both apply_incident_end_time
        # and the postmortem). Falls back to the reaction time when undeterminable.
        # ``override_end_time`` skips this: an admin who typed an explicit END time
        # in the UI gets that exact value, not the model's guess.
        if not override_end_time:
            ended_at = await self._resolve_incident_end_time(
                post,
                reacted_by_user_id=reacted_by_user_id,
                reaction_ended_at=ended_at,
                ticket=ticket,
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
