"""Постмортем инцидента (Jira + тред): PostmortemMixin.

Генерация постмортема при завершении инцидента: LLM-отчёт в Jira-задачу, проводка
полей (валидность, END-время, Time to Fix), затем фактологическое саммари в тред
и зелёная плашка «инцидент закрыт». Методы вызываются собранным
`IncidentBotService` (см. `coordinator.py`); state (`llm`/`jira`/`mattermost`/
`repository`/`settings`) ставит конструктор координатора, summary-механика и
ops-лента живут в sibling-классах.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from mm_jira_bot.actions import INCIDENT_DONE_COLOR
from mm_jira_bot.domain import (
    ConfirmationResult,
    ConfirmationStatus,
    MattermostPost,
    runtime_timezone,
)
from mm_jira_bot.formatting import extract_alert_title
from mm_jira_bot.jira_payload import build_postmortem_description
from mm_jira_bot.logging import get_logger
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
from mm_jira_bot.service._shared import _PROMPT_KEY_POSTMORTEM

if TYPE_CHECKING:
    from mm_jira_bot.config import Settings
    from mm_jira_bot.domain import JiraIssue

# Имя логгера держим стабильным (`mm_jira_bot.service`) во всех файлах пакета —
# тесты и настроенные логгеры завязаны на него, а не на `__name__` модуля.
log = get_logger("mm_jira_bot.service")


class PostmortemMixin:
    # State устанавливает coordinator.__init__; объявляем только то, что трогает
    # этот миксин, теми же типами, что декларирует конструктор: `settings`/
    # `repository` типизированы, остальные клиенты идут без аннотаций → `Any`,
    # чтобы не ужесточать тип собранного класса (иначе фейки в тестах не проходят
    # pyright).
    settings: Settings
    repository: AlertTicketRepository
    mattermost: Any
    jira: Any
    llm: Any

    if TYPE_CHECKING:
        # Стабы sibling-методов из других классов собранного IncidentBotService —
        # pyright их иначе не видит на самом миксине. Сигнатуры повторяют реальные.
        # --- остаются в coordinator ---
        def _resolve_prompt_template(self, key: str) -> str | None: ...

        async def _announce_issue_to_ops(
            self, ticket: AlertTicket, issue: JiraIssue, *, source: str
        ) -> None: ...

        async def _resolve_user_display(self, user_id: str) -> str: ...

        # --- ThreadSummaryMixin (summary-механика) ---
        @staticmethod
        def _summary_base_props(
            thread_id_key: str, root_post_id: str, requested_by_user_id: str
        ) -> dict: ...

        async def _post_summary_placeholder(
            self, *, root_post_id: str, channel_id: str, base_props: dict, event: str
        ) -> str | None: ...

        async def _set_summary_status(
            self,
            reply_id: str | None,
            post_id: str,
            *,
            message: str,
            base_props: dict,
            event: str,
        ) -> None: ...

        async def _finalize_thread_summary_reply(
            self,
            reply_id: str | None,
            post_id: str,
            *,
            channel_id: str,
            message: str,
            base_props: dict,
            event: str,
        ) -> None: ...

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
        ) -> bool: ...

        async def _create_thread_summary_reply(
            self,
            post_id: str,
            *,
            channel_id: str,
            message: str,
            base_props: dict,
            event: str,
            color: str = ...,
        ) -> str | None: ...

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
        await self._announce_issue_to_ops(ticket, issue, source="incident_postmortem")
        await self._apply_postmortem_validity(
            ticket.mattermost_post_id, issue.key, validity_label=validity_label
        )
        await self.jira.set_end_time(issue.key, ended_at)
        await self._set_time_to_fix(issue.key, ticket, ended_at)
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
