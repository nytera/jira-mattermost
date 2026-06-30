"""Тредовое саммари (Mattermost): ThreadSummaryMixin.

Публикация LLM-саммари треда в Mattermost: плейсхолдер «⏳ Генерация саммари…»,
стриминговый live-edit ответа и финализация. Методы вызываются собранным
`IncidentBotService` (см. `coordinator.py`); state (`llm`/`mattermost`/`settings`)
ставит конструктор координатора.
"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING, Any

from mm_jira_bot.colors import NOTICE_ATTACHMENT_COLOR
from mm_jira_bot.domain import MattermostPost
from mm_jira_bot.llm import StreamProgress
from mm_jira_bot.logging import get_logger
from mm_jira_bot.postmortem import build_incident_report_prompt, format_thread_transcript
from mm_jira_bot.retry import ApiError
from mm_jira_bot.service._shared import (
    _PROMPT_KEY_SUMMARY,
    SUMMARY_FAILED_TEXT,
    SUMMARY_PENDING_TEXT,
    ActionResult,
)
from mm_jira_bot.summary import format_thread_summary_reply, format_thread_summary_streaming

if TYPE_CHECKING:
    from mm_jira_bot.config import Settings
    from mm_jira_bot.postmortem import ThreadMessage

# Имя логгера держим стабильным (`mm_jira_bot.service`) во всех файлах пакета —
# тесты и настроенные логгеры завязаны на него, а не на `__name__` модуля.
log = get_logger("mm_jira_bot.service")


class ThreadSummaryMixin:
    # State устанавливает coordinator.__init__; объявляем только то, что трогает
    # этот миксин. Типы повторяют декларации coordinator.__init__: `settings`
    # типизирован там как Settings; `mattermost`/`llm` идут без аннотаций (параметры
    # конструктора нетипизированы) — поэтому здесь `Any`, чтобы не ужесточать тип
    # собранного класса (иначе фейки в тестах перестают проходить pyright).
    settings: Settings
    mattermost: Any
    llm: Any

    if TYPE_CHECKING:
        # Стабы sibling-методов из других классов собранного IncidentBotService —
        # pyright их иначе не видит на самом миксине. Сигнатуры повторяют реальные
        # (для `_box_thread_reply` важен @staticmethod, иначе reportIncompatibleMethodOverride).
        @staticmethod
        def _box_thread_reply(
            message: str, props: dict | None, color: str
        ) -> tuple[str, dict | None]: ...

        async def _postmortem_thread_context(
            self, root_post: MattermostPost, *, reacted_by_user_id: str
        ) -> tuple[list[ThreadMessage], list[str], str]: ...

        def _resolve_prompt_template(self, key: str) -> str | None: ...

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
