"""Синхронизация алертов с Jira: JiraSyncMixin.

Создание Jira-задачи для сработавшего алерта и реплай «Создана задача» в тред,
аннотация ожидаемых повторов (validity + «is child of» линк), фоновая обработка
отложенной работы/бэкафилл и проводка полей задачи при подтверждении инцидента.
Методы вызываются собранным `IncidentBotService` (см. `coordinator.py`); state
(`settings`/`repository`/`mattermost`/`jira`) ставит конструктор координатора,
ops-лента, alert-аттачменты и `confirm_incident` живут в sibling-классах.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mm_jira_bot.actions import DUTY_HELP_ATTACHMENT_COLOR
from mm_jira_bot.domain import JiraIssue
from mm_jira_bot.formatting import (
    format_alert_duty_help,
    format_thread_issue_created,
    format_thread_linked_to_root,
)
from mm_jira_bot.jira import VALID_INCIDENT_EXPECTED_VALUE, stub_jira_issue
from mm_jira_bot.jira_payload import (
    build_expected_alert_block,
    build_jira_description,
    build_postmortem_description,
)
from mm_jira_bot.logging import get_logger
from mm_jira_bot.repository import AlertTicket, AlertTicketRepository, ticket_to_post
from mm_jira_bot.retry import ApiError

if TYPE_CHECKING:
    from datetime import datetime

    from mm_jira_bot.config import Settings
    from mm_jira_bot.domain import ConfirmationResult, MattermostPost

# Имя логгера держим стабильным (`mm_jira_bot.service`) во всех файлах пакета —
# тесты и настроенные логгеры завязаны на него, а не на `__name__` модуля.
log = get_logger("mm_jira_bot.service")


class JiraSyncMixin:
    # State устанавливает coordinator.__init__; объявляем только то, что трогает
    # этот миксин, теми же типами, что декларирует конструктор: `settings`/
    # `repository` типизированы, клиенты `mattermost`/`jira` идут без аннотаций →
    # `Any`, чтобы не ужесточать тип собранного класса (иначе фейки в тестах не
    # проходят pyright).
    settings: Settings
    repository: AlertTicketRepository
    mattermost: Any
    jira: Any

    if TYPE_CHECKING:
        # Стабы sibling-методов из других классов собранного IncidentBotService —
        # pyright их иначе не видит на самом миксине. Сигнатуры повторяют реальные
        # дословно (важны kw-only `*` и имена параметров для override-совместимости).
        async def _announce_issue_to_ops(
            self, ticket: AlertTicket, issue: JiraIssue, *, source: str
        ) -> None: ...

        def _alert_action_attachments(
            self,
            alert_post_id: str,
            *,
            title: str | None = ...,
            title_link: str | None = ...,
            confirmed: bool = ...,
        ) -> list[dict] | None: ...

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

        async def confirm_incident(
            self,
            post_id: str,
            *,
            confirmed_by_user_id: str,
            source: str,
            confirmed_at: datetime | None = ...,
        ) -> ConfirmationResult: ...

        async def handle_alert_post(self, post: MattermostPost) -> AlertTicket | None: ...

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
            await self._announce_issue_to_ops(ticket, issue, source="alert")
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
