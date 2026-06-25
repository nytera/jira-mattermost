"""Админ-операции сервиса: AdminMixin.

Операционные действия, которые админ-UI (`admin_api.py`) вызывает по HTTP поверх
обычных доменных потоков: создать Jira-задачу для алерта по ссылке/post id
(`admin_create_from_link`, переиспользует `handle_alert_post`), пересоздать задачу
(`admin_recreate_jira_issue`) и тонкие обёртки lifecycle (confirm / end+постмортем /
validity / саммари треда), которые резолвят пост и делегируют в сиблинг-методы.
State (`settings`/`repository`/`mattermost`) ставит конструктор координатора; сами
потоки живут в соседних миксинах и объявлены здесь как `TYPE_CHECKING`-стабы.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from mm_jira_bot.domain import (
    ConfirmationResult,
    ConfirmationStatus,
    JiraIssue,
    MattermostPost,
    backend_now,
)
from mm_jira_bot.formatting import is_resolved_alert
from mm_jira_bot.logging import get_logger
from mm_jira_bot.repository import AlertTicket, AlertTicketRepository
from mm_jira_bot.retry import ApiError
from mm_jira_bot.service._shared import ActionResult, parse_post_id_from_text

if TYPE_CHECKING:
    from mm_jira_bot.config import Settings

# Имя логгера держим стабильным (`mm_jira_bot.service`) во всех файлах пакета —
# тесты и настроенные логгеры завязаны на него, а не на `__name__` модуля.
log = get_logger("mm_jira_bot.service")


@dataclass(frozen=True)
class AdminCreateFromLinkResult:
    ok: bool
    status: str
    message: str
    mattermost_post_id: str | None = None
    jira_issue_key: str | None = None
    jira_issue_url: str | None = None


@dataclass(frozen=True)
class AdminJiraRecreateResult:
    ok: bool
    status: str
    message: str
    mattermost_post_id: str
    jira_issue_key: str | None = None
    jira_issue_url: str | None = None
    previous_jira_issue_key: str | None = None
    previous_jira_issue_url: str | None = None


class AdminMixin:
    # State устанавливает coordinator.__init__; объявляем только то, что трогает
    # этот миксин, теми же типами, что декларирует конструктор: `settings`/
    # `repository` типизированы, клиент `mattermost` идёт без аннотаций → `Any`.
    settings: Settings
    repository: AlertTicketRepository
    mattermost: Any

    if TYPE_CHECKING:
        # Стабы sibling-методов из других классов собранного IncidentBotService —
        # pyright их иначе не видит на самом миксине. Сигнатуры повторяют реальные
        # (kw-only `*` и имена параметров важны для override-совместимости).
        # --- AlertMixin ---
        async def handle_alert_post(self, post: MattermostPost) -> AlertTicket | None: ...

        async def apply_validity_label(
            self,
            post_id: str,
            *,
            validity_label: str,
            validity_set_at: datetime | None = ...,
            source: str,
        ) -> ConfirmationResult: ...

        # --- IncidentMixin ---
        async def confirm_incident(
            self,
            post_id: str,
            *,
            confirmed_by_user_id: str,
            source: str,
            confirmed_at: datetime | None = ...,
        ) -> ConfirmationResult: ...

        async def handle_incident_checkmark(
            self,
            post: MattermostPost,
            *,
            reacted_by_user_id: str,
            ended_at: datetime,
            source: str,
            validity_label: str | None = ...,
        ) -> ConfirmationResult: ...

        # --- JiraSyncMixin ---
        async def _create_jira_issue(self, ticket: AlertTicket) -> JiraIssue: ...

        async def _update_jira_for_confirmation(
            self, ticket: AlertTicket, *, confirmed_by: str
        ) -> None: ...

        # --- ThreadSummaryMixin ---
        async def generate_thread_summary(
            self, alert_post: MattermostPost, *, requested_by_user_id: str, source: str
        ) -> ActionResult: ...

        # --- остаются в coordinator ---
        async def _announce_issue_to_ops(
            self, ticket: AlertTicket, issue: JiraIssue, *, source: str
        ) -> None: ...

        async def _resolve_user_display(self, user_id: str) -> str: ...

    def _admin_actor_id(self) -> str:
        """Mattermost user id attributed to UI-driven lifecycle actions.

        Defaults to ``ADMIN_MM_USER_ID`` so confirmations/ends carry a real
        identity in Jira/Mattermost; falls back to the ``admin-ui`` label when
        unset (``_resolve_user_display`` degrades gracefully on an unknown id).
        """
        return self.settings.admin_mm_user_id or "admin-ui"

    async def admin_create_from_link(self, link: str) -> AdminCreateFromLinkResult:
        """Create (or fetch) a Jira issue for an alert given its Band link/post id.

        Reuses the normal :meth:`handle_alert_post` flow, but resolves the post
        from a pasted permalink and returns explicit feedback for the admin UI.
        """
        post_id = parse_post_id_from_text(link)
        if post_id is None:
            return AdminCreateFromLinkResult(
                ok=False,
                status="invalid_link",
                message="Не удалось распознать ссылку или post id.",
            )

        try:
            post = await self.mattermost.get_post(post_id)
        except ApiError as exc:
            log.error(
                "admin.create_from_link.post_lookup_failed",
                mattermost_post_id=post_id,
                error=str(exc),
            )
            return AdminCreateFromLinkResult(
                ok=False,
                status="post_not_found",
                message=f"Не удалось прочитать сообщение `{post_id}`: {exc}",
                mattermost_post_id=post_id,
            )

        if post.channel_id != self.settings.mattermost_alert_channel_id:
            return AdminCreateFromLinkResult(
                ok=False,
                status="skipped",
                message="Сообщение не в канале алертов.",
                mattermost_post_id=post_id,
            )
        if is_resolved_alert(post.message):
            return AdminCreateFromLinkResult(
                ok=False,
                status="skipped",
                message="Это resolved-алерт — задача не создаётся.",
                mattermost_post_id=post_id,
            )

        existing = self.repository.get_by_post_id(post_id)
        already_had_issue = bool(existing and existing.jira_issue_key)

        ticket = await self.handle_alert_post(post)
        if ticket is None:
            return AdminCreateFromLinkResult(
                ok=False,
                status="skipped",
                message="Сообщение пропущено (бот, не алерт-канал или resolved).",
                mattermost_post_id=post_id,
            )
        if ticket.jira_issue_key:
            return AdminCreateFromLinkResult(
                ok=True,
                status="exists" if already_had_issue else "created",
                message=("Задача уже существовала." if already_had_issue else "Задача создана."),
                mattermost_post_id=post_id,
                jira_issue_key=ticket.jira_issue_key,
                jira_issue_url=ticket.jira_issue_url,
            )
        return AdminCreateFromLinkResult(
            ok=False,
            status="error",
            message=ticket.last_error or "Создание задачи не удалось, см. логи.",
            mattermost_post_id=post_id,
        )

    async def admin_recreate_jira_issue(
        self, post_id: str, *, force: bool = False
    ) -> AdminJiraRecreateResult:
        ticket = self.repository.get_by_post_id(post_id)
        if ticket is None:
            return AdminJiraRecreateResult(
                ok=False,
                status="not_found",
                message=f"Alert ticket for post_id={post_id} was not found.",
                mattermost_post_id=post_id,
            )
        if ticket.jira_issue_key and not force:
            return AdminJiraRecreateResult(
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
                "admin.jira_issue.recreate_failed",
                mattermost_post_id=post_id,
                force=force,
                error=str(exc),
            )
            return AdminJiraRecreateResult(
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
            confirmed_by = updated_ticket.confirmed_by_user_id or self._admin_actor_id()
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
                    "admin.jira_issue.confirmation_reapply_failed",
                    mattermost_post_id=post_id,
                    jira_issue_key=issue.key,
                    error=str(exc),
                )
                return AdminJiraRecreateResult(
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
            "admin.jira_issue.recreated",
            mattermost_post_id=post_id,
            jira_issue_key=issue.key,
            previous_jira_issue_key=previous_key,
            force=force,
        )
        return AdminJiraRecreateResult(
            ok=True,
            status="recreated" if force and previous_key else "created",
            message="Jira issue created.",
            mattermost_post_id=post_id,
            jira_issue_key=issue.key,
            jira_issue_url=issue.url,
            previous_jira_issue_key=previous_key,
            previous_jira_issue_url=previous_url,
        )

    async def admin_confirm_incident(self, post_id: str) -> ConfirmationResult:
        """Publish + confirm a valid incident from the UI (delegates to the
        normal confirmation flow, alert-keyed by ``post_id``)."""
        return await self.confirm_incident(
            post_id, confirmed_by_user_id=self._admin_actor_id(), source="admin_ui"
        )

    async def admin_end_incident(
        self, post_id: str, *, ended_at: datetime | None = None
    ) -> ConfirmationResult:
        """End an incident from the UI: resolve the incident post and run the
        checkmark flow (END time + Time-to-Fix + postmortem). Incident-keyed —
        uses ``incident_post_id``, not the alert ``post_id``. Idempotent: a second
        call on a finalized incident leaves the postmortem untouched."""
        ticket = self.repository.get_by_post_id(post_id)
        if ticket is None:
            return ConfirmationResult(
                status=ConfirmationStatus.NOT_FOUND,
                message=f"Тикет для post_id={post_id} не найден.",
            )
        if not ticket.incident_post_id:
            return ConfirmationResult(
                status=ConfirmationStatus.NOT_FOUND,
                message="Инцидент ещё не опубликован — завершать нечего.",
            )
        post = await self.mattermost.get_post(ticket.incident_post_id)
        return await self.handle_incident_checkmark(
            post,
            reacted_by_user_id=self._admin_actor_id(),
            ended_at=ended_at or backend_now(),
            source="admin_ui",
        )

    async def admin_set_validity(self, post_id: str, *, validity_label: str) -> ConfirmationResult:
        """Set the Jira «Валидность» field from the UI (lightweight path)."""
        return await self.apply_validity_label(
            post_id, validity_label=validity_label, source="admin_ui"
        )

    async def admin_generate_summary(self, post_id: str) -> ActionResult:
        """Generate the alert thread summary from the UI (alert-keyed)."""
        try:
            post = await self.mattermost.get_post(post_id)
        except ApiError as exc:
            return ActionResult(message=f"Не удалось прочитать сообщение `{post_id}`: {exc}")
        return await self.generate_thread_summary(
            post, requested_by_user_id=self._admin_actor_id(), source="admin_ui"
        )
