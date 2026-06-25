"""Разделяемые между доменными mixin-классами примитивы пакета `service/`.

Этот модуль — **лист графа импортов** пакета: он ничего не импортирует обратно из
`coordinator`/доменных миксинов, поэтому константы, dataclass'ы и `SharedMixin`
(база с методами, доказанно нужными нескольким доменам сразу) живут здесь. Так
разрывается цикл «coordinator импортирует миксин → миксин импортирует имя из
coordinator (ещё не определённое)».
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mm_jira_bot.actions import NOTICE_ATTACHMENT_COLOR
from mm_jira_bot.domain import ConfirmationResult, ConfirmationStatus
from mm_jira_bot.logging import get_logger
from mm_jira_bot.retry import ApiError

if TYPE_CHECKING:
    from mm_jira_bot.config import Settings
    from mm_jira_bot.repository import AlertTicketRepository

# Распознавание Mattermost post id в ссылке/тексте (`/incident <permalink>` и
# админ-панель). Жил в `coordinator`, переехал сюда (лист графа импортов), чтобы
# `_admin.py` мог импортировать функцию без цикла; `coordinator` ре-импортирует её
# (ре-экспорт в `service/__init__.py` и тесты продолжают работать без правок).
POST_ID_PATTERN = re.compile(r"(?:^|/)(?:_redirect/)?pl/([a-z0-9]{20,32})(?:$|[/?#])")
BARE_POST_ID_PATTERN = re.compile(r"^[a-z0-9]{20,32}$")


def parse_post_id_from_text(text: str) -> str | None:
    text = text.strip()
    if BARE_POST_ID_PATTERN.fullmatch(text):
        return text
    match = POST_ID_PATTERN.search(text)
    if match:
        return match.group(1)
    return None


# Тексты плейсхолдера/ошибки тредового саммари (используются ThreadSummaryMixin и
# координатором при публикации саммари из разных потоков).
SUMMARY_PENDING_TEXT = "⏳ Генерация саммари…"
SUMMARY_FAILED_TEXT = "Не удалось сгенерировать саммари, попробуйте позже."

# DB-override keys for the runtime-editable LLM prompt templates (admin UI).
# `_PROMPT_KEY_*` читаются снаружи из `admin_api.py` через ре-экспорт в
# `service/__init__.py` — менять имена нельзя.
_PROMPT_KEY_SUMMARY = "llm_summary_prompt"
_PROMPT_KEY_POSTMORTEM = "llm_postmortem_prompt"


def _validity_action_message(result: ConfirmationResult, validity_label: str) -> str:
    if result.status == ConfirmationStatus.VALIDITY_SET:
        return f"Готово: «Валидность» = {validity_label}."
    if result.status == ConfirmationStatus.PENDING_JIRA:
        return "Задача Jira ещё создаётся — обновлю «Валидность» автоматически."
    if result.status == ConfirmationStatus.ERROR:
        return "Не удалось обновить «Валидность», попробуйте ещё раз."
    return result.message


@dataclass(frozen=True)
class ActionResult:
    """Ephemeral feedback shown to the user who clicked an alert button.

    ``update_attachments``, when set, replaces the originating post's attachments
    via the Mattermost interactive-action ``update`` response (used to swap the
    "Создать задачу" prompt for the full controls card after task creation).
    """

    message: str
    update_attachments: list[dict] | None = None


# Имя логгера держим стабильным (`mm_jira_bot.service`) во всех файлах пакета —
# тесты и настроенные логгеры завязаны на него, а не на `__name__` модуля.
log = get_logger("mm_jira_bot.service")


class SharedMixin:
    """База с методами, доказанно нужными нескольким доменам сразу.

    Метод живёт здесь, только если он реально шарится (вызывается ≥2 уже
    вынесенными миксинами либо переносимым доменом) и трогает state — иначе это
    free-функция. Класс самодостаточен: ни один метод не зовёт sibling из другого
    миксина, поэтому `TYPE_CHECKING`-стабы не нужны. State ставит
    `coordinator.__init__`; объявляем только то, что трогают эти методы.
    """

    settings: Settings
    repository: AlertTicketRepository
    mattermost: Any

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
