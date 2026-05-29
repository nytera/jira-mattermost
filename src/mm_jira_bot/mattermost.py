from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from urllib.parse import urlparse, urlunparse

import httpx
import websockets

from mm_jira_bot.config import Settings
from mm_jira_bot.domain import MattermostPost, ReactionEvent
from mm_jira_bot.retry import ApiError, is_retryable_status, retry_async

logger = logging.getLogger(__name__)


def build_mattermost_permalink(base_url: str, post_id: str) -> str:
    return f"{base_url.rstrip('/')}/_redirect/pl/{post_id}"


def websocket_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse(
        (
            scheme,
            parsed.netloc,
            f"{parsed.path.rstrip('/')}/api/v4/websocket",
            "",
            "",
            "",
        )
    )


def parse_posted_event(payload: dict) -> MattermostPost | None:
    if payload.get("event") != "posted":
        return None
    data = payload.get("data") or {}
    raw_post = data.get("post")
    if not raw_post:
        return None
    post_data = json.loads(raw_post) if isinstance(raw_post, str) else raw_post
    return MattermostPost.from_api(
        post_data,
        channel_name=data.get("channel_name") or data.get("channel_display_name"),
    )


def parse_reaction_event(payload: dict) -> ReactionEvent | None:
    if payload.get("event") != "reaction_added":
        return None
    data = payload.get("data") or {}
    raw_reaction = data.get("reaction")
    if not raw_reaction:
        return None
    reaction = json.loads(raw_reaction) if isinstance(raw_reaction, str) else raw_reaction
    return ReactionEvent(
        post_id=reaction["post_id"],
        user_id=reaction["user_id"],
        emoji_name=reaction["emoji_name"],
        create_at=int(reaction.get("create_at") or 0),
    )


class MattermostClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._own_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=settings.mattermost_url,
            timeout=20,
            headers={
                "Authorization": f"Bearer {settings.mattermost_token}",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    def permalink(self, post_id: str) -> str:
        return build_mattermost_permalink(self._settings.mattermost_url, post_id)

    async def get_post(self, post_id: str) -> MattermostPost:
        async def operation() -> MattermostPost:
            response = await self._client.get(f"/api/v4/posts/{post_id}")
            self._raise_for_status(response, "Failed to get Mattermost post")
            return MattermostPost.from_api(response.json())

        return await retry_async(
            operation,
            attempts=self._settings.api_retry_attempts,
            base_delay_seconds=self._settings.api_retry_base_delay_seconds,
            logger=logger,
            event="mattermost.get_post",
            mattermost_post_id=post_id,
        )

    async def get_channel_name(self, channel_id: str) -> str | None:
        async def operation() -> str | None:
            response = await self._client.get(f"/api/v4/channels/{channel_id}")
            self._raise_for_status(response, "Failed to get Mattermost channel")
            data = response.json()
            return data.get("display_name") or data.get("name")

        return await retry_async(
            operation,
            attempts=self._settings.api_retry_attempts,
            base_delay_seconds=self._settings.api_retry_base_delay_seconds,
            logger=logger,
            event="mattermost.get_channel",
            mattermost_channel_id=channel_id,
        )

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        props: dict | None = None,
    ) -> MattermostPost:
        payload: dict = {"channel_id": channel_id, "message": message}
        if props:
            payload["props"] = props

        async def operation() -> MattermostPost:
            response = await self._client.post("/api/v4/posts", json=payload)
            self._raise_for_status(response, "Failed to create Mattermost post")
            return MattermostPost.from_api(response.json())

        return await retry_async(
            operation,
            attempts=self._settings.api_retry_attempts,
            base_delay_seconds=self._settings.api_retry_base_delay_seconds,
            logger=logger,
            event="mattermost.create_post",
            mattermost_channel_id=channel_id,
        )

    async def fetch_recent_channel_posts(
        self, channel_id: str, *, limit: int
    ) -> list[MattermostPost]:
        per_page = min(max(limit, 1), 200)

        async def operation() -> list[MattermostPost]:
            response = await self._client.get(
                f"/api/v4/channels/{channel_id}/posts",
                params={"page": 0, "per_page": per_page},
            )
            self._raise_for_status(response, "Failed to fetch Mattermost channel posts")
            data = response.json()
            posts = data.get("posts", {})
            order = data.get("order", [])
            return [MattermostPost.from_api(posts[post_id]) for post_id in reversed(order)]

        return await retry_async(
            operation,
            attempts=self._settings.api_retry_attempts,
            base_delay_seconds=self._settings.api_retry_base_delay_seconds,
            logger=logger,
            event="mattermost.fetch_recent_posts",
            mattermost_channel_id=channel_id,
        )

    async def websocket_events(self) -> AsyncIterator[dict]:
        url = websocket_url(self._settings.mattermost_url)
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(
                json.dumps(
                    {
                        "seq": 1,
                        "action": "authentication_challenge",
                        "data": {"token": self._settings.mattermost_token},
                    }
                )
            )
            async for raw_message in ws:
                yield json.loads(raw_message)

    def _raise_for_status(self, response: httpx.Response, message: str) -> None:
        if response.is_success:
            return
        raise ApiError(
            f"{message}: HTTP {response.status_code} {response.text}",
            status_code=response.status_code,
            retryable=is_retryable_status(response.status_code),
        )
