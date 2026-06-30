"""Forward bot error events to a dedicated Mattermost ops channel.

Every ``log.error`` from ``mm_jira_bot.*`` already names a structured event, so a
single logging handler is the whole feature: when an ops channel is configured it
enqueues a compact payload for :meth:`OpsNotifier.drain` to post as a colored
attachment.

Hardening (see plan):

* **Best-effort** — delivery failures are swallowed and logged at WARNING; the
  drain loop never dies.
* **No recursion** — a ``_posting`` contextvar is set while the notifier posts;
  any log record emitted during that window is dropped, so a delivery failure
  cannot feed itself regardless of how the HTTP layer logs.
* **Anti-storm** — a per-event cooldown suppresses repeats; a bounded queue drops
  (and counts) overflow instead of growing without bound.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from mm_jira_bot.colors import OPS_ALERT_COLOR
from mm_jira_bot.logging import LOGGER_PREFIX, get_logger

if TYPE_CHECKING:
    from mm_jira_bot.config import Settings
    from mm_jira_bot.mattermost import MattermostClient

log = get_logger(__name__)

_BOT_LOGGER = LOGGER_PREFIX.rstrip(".")
_QUEUE_MAXSIZE = 100
# Fields not worth echoing into the channel message.
_SKIP_FIELDS = frozenset({"event"})

_posting: ContextVar[bool] = ContextVar("ops_posting", default=False)


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    extra = getattr(record, "extra_fields", None)
    return dict(extra) if isinstance(extra, dict) else {}


def _format_message(event: str, fields: dict[str, Any]) -> str:
    lines = [f":rotating_light: **{event}**"]
    detail = " ".join(
        f"`{key}={value}`" for key, value in fields.items() if key not in _SKIP_FIELDS
    )
    if detail:
        lines.append(detail)
    return "\n".join(lines)


class OpsLogHandler(logging.Handler):
    """Count error events and (once activated) enqueue them for the ops channel."""

    def __init__(self, *, cooldown_seconds: int) -> None:
        super().__init__(level=logging.ERROR)
        self._cooldown = cooldown_seconds
        self._last_sent: dict[str, float] = {}
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def activate(
        self, queue: asyncio.Queue[dict[str, Any]], loop: asyncio.AbstractEventLoop
    ) -> None:
        """Wire the live queue/loop so emitted events start reaching the channel."""
        self._queue = queue
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.ERROR or _posting.get():
                return
            fields = _record_fields(record)
            event = str(fields.get("event") or record.getMessage())
            if self._queue is None or self._loop is None:
                return
            now = time.monotonic()
            last = self._last_sent.get(event)
            if last is not None and now - last < self._cooldown:
                return
            self._last_sent[event] = now
            payload = {"event": event, "fields": fields}
            self._loop.call_soon_threadsafe(self._enqueue, payload)
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)

    def _enqueue(self, payload: dict[str, Any]) -> None:
        if self._queue is None:
            return
        # Drop on overflow (best-effort; the queue is intentionally bounded).
        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(payload)


class OpsNotifier:
    """Owns the ops log handler and the async drain loop that posts to Mattermost."""

    def __init__(self, mattermost: MattermostClient, settings: Settings) -> None:
        self._mattermost = mattermost
        self._channel_id = settings.mattermost_ops_channel_id
        self._handler = OpsLogHandler(cooldown_seconds=settings.ops_cooldown_seconds)
        self._queue: asyncio.Queue[dict[str, Any]] | None = None

    @property
    def posts_to_channel(self) -> bool:
        return bool(self._channel_id)

    def install(self) -> None:
        """Attach the handler to the ``mm_jira_bot`` logger (idempotent)."""
        logger = logging.getLogger(_BOT_LOGGER)
        for existing in [h for h in logger.handlers if isinstance(h, OpsLogHandler)]:
            logger.removeHandler(existing)
        logger.addHandler(self._handler)

    def activate(self) -> None:
        """Bind a live queue/loop so events buffer immediately.

        Called at the top of the lifespan, *before* preflight, so early startup
        errors (preflight/backfill) are buffered instead of dropped before
        :meth:`drain` starts consuming. Must run inside the event loop.
        """
        self._queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._handler.activate(self._queue, asyncio.get_running_loop())

    async def drain(self) -> None:
        """Post queued events until cancelled (queue is bound by :meth:`activate`)."""
        assert self._queue is not None, "activate() must run before drain()"
        while True:
            payload = await self._queue.get()
            await self._post(payload)

    async def _post(self, payload: dict[str, Any]) -> None:
        if not self._channel_id:
            return
        message = _format_message(payload["event"], payload.get("fields", {}))
        token = _posting.set(True)
        try:
            await self._mattermost.create_post(
                channel_id=self._channel_id,
                message="",
                props={
                    "attachments": [
                        {"fallback": message, "color": OPS_ALERT_COLOR, "text": message}
                    ]
                },
            )
        except Exception as exc:  # ApiError or transport error: never kill the loop
            log.warning("ops.alert.post_failed", alert_event=payload.get("event"), error=str(exc))
        finally:
            _posting.reset(token)
