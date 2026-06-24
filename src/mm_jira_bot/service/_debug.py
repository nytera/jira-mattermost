"""Debug-админка: DebugMixin.

Два административных входа отладочной панели (`debug_admin.py`): создать Jira-задачу
для алерта по ссылке/post id (`debug_create_from_link`, переиспользует обычный
`handle_alert_post`) и пересоздать Jira-задачу для существующего тикета
(`debug_recreate_jira_issue`). Методы вызываются собранным `IncidentBotService`
через MRO; state (`settings`/`repository`/`mattermost`) ставит конструктор
координатора, а alert-обработка, Jira-проводка и резолв пользователя живут в
sibling-классах.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mm_jira_bot.domain import JiraIssue, MattermostPost, backend_now
from mm_jira_bot.formatting import is_resolved_alert
from mm_jira_bot.logging import get_logger
from mm_jira_bot.repository import AlertTicket, AlertTicketRepository
from mm_jira_bot.retry import ApiError
from mm_jira_bot.service._shared import parse_post_id_from_text

if TYPE_CHECKING:
    from mm_jira_bot.config import Settings

# Имя логгера держим стабильным (`mm_jira_bot.service`) во всех файлах пакета —
# тесты и настроенные логгеры завязаны на него, а не на `__name__` модуля.
log = get_logger("mm_jira_bot.service")


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


class DebugMixin:
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

        # --- JiraSyncMixin ---
        async def _create_jira_issue(self, ticket: AlertTicket) -> JiraIssue: ...

        async def _update_jira_for_confirmation(
            self, ticket: AlertTicket, *, confirmed_by: str
        ) -> None: ...

        # --- остаются в coordinator ---
        async def _announce_issue_to_ops(
            self, ticket: AlertTicket, issue: JiraIssue, *, source: str
        ) -> None: ...

        async def _resolve_user_display(self, user_id: str) -> str: ...

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
