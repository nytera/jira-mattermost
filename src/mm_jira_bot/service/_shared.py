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

from mm_jira_bot.colors import NOTICE_ATTACHMENT_COLOR
from mm_jira_bot.logging import get_logger
from mm_jira_bot.retry import ApiError

if TYPE_CHECKING:
    from mm_jira_bot.config import Settings
    from mm_jira_bot.repository import AlertTicketRepository

# Распознавание Mattermost post id в ссылке/тексте (`/incident <permalink>`).
# Живут здесь (лист графа импортов) и ре-экспортируются через `service/__init__.py`.
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


@dataclass(frozen=True)
class ActionResult:
    """Short human-facing message returned by thread-summary actions."""

    message: str


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

    def _is_test_channel(self, channel_id: str) -> bool:
        """True if ``channel_id`` is a configured test alert/incident channel — but
        only under ``read_only_mode``, mirroring ``_is_alert_channel``.

        Test channels are a shadow-only live sandbox: their Mattermost traffic runs
        the full workflow with **real** posts/threads/reactions (not mirrored to the
        audit channel), while Jira stays globally stubbed by read-only. In a normal
        deployment test channels are never active — a leftover env var must not spawn
        test tickets that pollute prod state."""
        if not self.settings.read_only_mode:
            return False
        return channel_id is not None and channel_id in (
            self.settings.mattermost_test_alert_channel_id,
            self.settings.mattermost_test_incident_channel_id,
        )

    def _is_alert_channel(self, channel_id: str) -> bool:
        """Alert channel — the real one always, plus (read-only only) the configured
        test alert channel.

        The shadow treats the test alert channel as a first-class alert channel so
        test traffic exercises the same path as prod traffic. The test channel is
        folded in ONLY under ``read_only_mode``: in a normal deployment a leftover
        ``MATTERMOST_TEST_ALERT_CHANNEL_ID`` must never route real traffic into the
        live alert path (which would create real Jira issues / Mattermost writes).
        """
        if channel_id == self.settings.mattermost_alert_channel_id:
            return True
        return (
            self.settings.read_only_mode
            and channel_id == self.settings.mattermost_test_alert_channel_id
        )

    def _is_incident_channel(self, channel_id: str) -> bool:
        """Incident channel — the real one always, plus (read-only only) the
        configured test incident channel. See ``_is_alert_channel`` for why the test
        channel is gated by ``read_only_mode``."""
        if channel_id == self.settings.mattermost_incident_channel_id:
            return True
        return (
            self.settings.read_only_mode
            and channel_id == self.settings.mattermost_test_incident_channel_id
        )

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
