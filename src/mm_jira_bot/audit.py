"""Audit-channel mirror for read-only (shadow) mode.

In read-only mode the bot must not write to Jira or to the real Mattermost
channels. Instead, every Mattermost write the bot *would* make is reproduced in a
dedicated audit channel, so the channel reads as "what the bot would do right now
if it were prod" — with zero prod impact.

The mirror is injected into :class:`~mm_jira_bot.mattermost.MattermostClient`
(``mattermost.audit``). Suppressed ``create_post`` / ``add_reaction`` /
``update_post`` calls are redirected here, and the mirror re-posts them to the
audit channel via ``create_post(..., allow_in_read_only=True)`` — the one write
the read-only backstop permits.

Threads are reproduced: an in-memory map from each original thread root (a real
post id, or a ``readonly-`` stub the shadow minted) to its audit post lets
replies land under the right audit root, and lets reactions/updates target the
right audit post. The map is bounded (LRU) and lost on restart — after a restart
older threads mirror flat, which is acceptable.

Mirroring is best-effort: a failed audit post is logged at WARNING and never
propagates, so the shadow keeps processing events.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import TYPE_CHECKING

from mm_jira_bot.domain import MattermostPost
from mm_jira_bot.logging import get_logger
from mm_jira_bot.mattermost import stub_mattermost_post

if TYPE_CHECKING:
    from mm_jira_bot.config import Settings
    from mm_jira_bot.mattermost import MattermostClient

log = get_logger(__name__)

# Props that correlate a post back to its source alert/incident. Stripped from
# audit copies so the shadow can never treat its own audit post as a prod
# artifact (and so the audit channel does not carry stale correlation keys).
_ADOPTION_PROP_KEYS = ("mattermost_alert_post_id", "mattermost_incident_post_id")
_THREAD_MAP_MAXSIZE = 2048


class AuditMirror:
    def __init__(self, mattermost: MattermostClient, settings: Settings) -> None:
        self._mattermost = mattermost
        self._settings = settings
        self._channel_id = settings.mattermost_audit_channel_id
        # original id (real post or readonly- stub) -> audit post id. Used both
        # as a thread map (reply -> audit root) and a post map (update/reaction
        # -> audit post).
        self._thread_map: OrderedDict[str, str] = OrderedDict()
        # Serialises anchor creation: WS events dispatch concurrently
        # (asyncio.create_task per event), so two ops on the same unseen root must
        # not each mint a duplicate anchor and split the mirrored thread.
        self._anchor_lock = asyncio.Lock()

    def _remember(self, original_id: str, audit_id: str) -> None:
        self._thread_map[original_id] = audit_id
        self._thread_map.move_to_end(original_id)
        while len(self._thread_map) > _THREAD_MAP_MAXSIZE:
            self._thread_map.popitem(last=False)

    def _audit_id_for(self, original_id: str) -> str | None:
        audit_id = self._thread_map.get(original_id)
        if audit_id is not None:
            self._thread_map.move_to_end(original_id)
        return audit_id

    @staticmethod
    def _sanitize_props(props: dict | None) -> dict | None:
        if not props:
            return props
        cleaned = {key: value for key, value in props.items() if key not in _ADOPTION_PROP_KEYS}
        return cleaned or None

    @staticmethod
    def _anchor_text(original_root_id: str, source_channel_id: str | None) -> str:
        where = f" (канал `{source_channel_id}`)" if source_channel_id else ""
        return f":link: _Зеркало треда_ — оригинал `{original_root_id}`{where}"

    async def _post(
        self, *, message: str, props: dict | None, root_id: str | None
    ) -> MattermostPost | None:
        if not self._channel_id:
            return None
        try:
            return await self._mattermost.create_post(
                channel_id=self._channel_id,
                message=message,
                props=props,
                root_id=root_id,
                allow_in_read_only=True,
            )
        except Exception as exc:  # best-effort: a failed mirror must not break the shadow
            log.warning("audit.mirror.post_failed", error=str(exc))
            return None

    async def _ensure_anchor(
        self, original_root_id: str, *, source_channel_id: str | None = None
    ) -> str | None:
        """Audit root post id for ``original_root_id``, creating an anchor post
        the first time this thread root is seen."""
        audit_root = self._audit_id_for(original_root_id)
        if audit_root is not None:
            return audit_root
        # Hold the lock across the create-then-remember so a concurrent task for the
        # same root blocks until the anchor is recorded; the re-check inside the lock
        # reuses an anchor a racing task minted while we waited.
        async with self._anchor_lock:
            audit_root = self._audit_id_for(original_root_id)
            if audit_root is not None:
                return audit_root
            anchor = await self._post(
                message=self._anchor_text(original_root_id, source_channel_id),
                props=None,
                root_id=None,
            )
            if anchor is None:
                return None
            self._remember(original_root_id, anchor.id)
            return anchor.id

    async def mirror_create_post(
        self,
        *,
        channel_id: str,
        message: str,
        props: dict | None = None,
        root_id: str | None = None,
    ) -> MattermostPost:
        """Reproduce a suppressed ``create_post`` in the audit channel and return a
        ``readonly-`` stub the caller can store/patch as if it were the real post."""
        audit_root: str | None = None
        if root_id is not None:
            audit_root = await self._ensure_anchor(root_id, source_channel_id=channel_id)
        posted = await self._post(
            message=message,
            props=self._sanitize_props(props),
            root_id=audit_root,
        )
        stub = stub_mattermost_post(
            self._settings,
            channel_id=channel_id,
            message=message,
            props=props,
            root_id=root_id,
            create_at=posted.create_at if posted is not None else 0,
        )
        if posted is not None:
            self._remember(stub.id, posted.id)
        return stub

    async def mirror_reaction(self, post_id: str, emoji_name: str) -> None:
        """Reproduce a suppressed ``add_reaction`` on the matching audit post,
        creating an anchor first if the post has not been mirrored yet."""
        audit_id = await self._ensure_anchor(post_id)
        if audit_id is None:
            return
        try:
            await self._mattermost.add_reaction(audit_id, emoji_name, allow_in_read_only=True)
        except Exception as exc:
            log.warning("audit.mirror.reaction_failed", error=str(exc))

    async def mirror_update(
        self, post_id: str, *, message: str | None = None, props: dict | None = None
    ) -> None:
        """Reproduce a suppressed ``update_post`` on the matching audit post. If the
        target was never mirrored (e.g. lost across a restart), fall back to posting
        the new text as a fresh audit line."""
        audit_id = self._audit_id_for(post_id)
        if audit_id is None:
            if message is not None:
                await self._post(message=message, props=self._sanitize_props(props), root_id=None)
            return
        try:
            await self._mattermost.update_post(
                audit_id,
                message=message,
                props=self._sanitize_props(props),
                allow_in_read_only=True,
            )
        except Exception as exc:
            log.warning("audit.mirror.update_failed", error=str(exc))
