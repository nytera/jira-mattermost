from __future__ import annotations

import json
from collections.abc import AsyncIterator
from urllib.parse import urlparse, urlunparse

import httpx
import websockets

from mm_jira_bot.config import Settings
from mm_jira_bot.domain import MattermostPost, ReactionEvent
from mm_jira_bot.http import AsyncApiClient
from mm_jira_bot.logging import get_logger

log = get_logger(__name__)


def build_mattermost_permalink(base_url: str, post_id: str) -> str:
    return f"{base_url.rstrip('/')}/_redirect/pl/{post_id}"


def format_user_display(data: dict) -> str:
    username = (data.get("username") or "").strip()
    full_name = f"{data.get('first_name') or ''} {data.get('last_name') or ''}".strip()
    nickname = (data.get("nickname") or "").strip()
    if full_name and username:
        return f"{full_name} (@{username})"
    if username:
        return f"@{username}"
    return full_name or nickname


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


class MattermostClient(AsyncApiClient):
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        client = http_client or httpx.AsyncClient(
            base_url=settings.mattermost_url,
            timeout=20,
            headers={
                "Authorization": f"Bearer {settings.mattermost_token}",
                "Accept": "application/json",
            },
        )
        super().__init__(settings, client, own_client=http_client is None, log=log)

    def permalink(self, post_id: str) -> str:
        return build_mattermost_permalink(self._settings.mattermost_url, post_id)

    async def preflight_check(self) -> dict[str, object]:
        def parse_me(response: httpx.Response) -> dict[str, str | bool]:
            data = response.json()
            bot_user_id = str(data.get("id") or "")
            username = str(data.get("username") or "")
            return {
                "bot_user_id": bot_user_id,
                "bot_username": username,
                "bot_user_id_matches_config": (
                    bot_user_id == self._settings.mattermost_bot_user_id
                ),
            }

        me = await self._request(
            "GET",
            "/api/v4/users/me",
            error_message="Failed to get Mattermost current user",
            event="mattermost.preflight.users_me",
            parse=parse_me,
        )
        assert isinstance(me, dict)
        if not me.get("bot_user_id_matches_config"):
            log.warning(
                "mattermost.preflight.bot_user_id_mismatch",
                configured_bot_user_id=self._settings.mattermost_bot_user_id,
                actual_bot_user_id=me.get("bot_user_id"),
                actual_bot_username=me.get("bot_username"),
            )
        alert_channel_name = await self.get_channel_name(
            self._settings.mattermost_alert_channel_id
        )
        incident_channel_name = await self.get_channel_name(
            self._settings.mattermost_incident_channel_id
        )
        return {
            **me,
            "mattermost_url": self._settings.mattermost_url,
            "alert_channel_id": self._settings.mattermost_alert_channel_id,
            "alert_channel_name": alert_channel_name,
            "incident_channel_id": self._settings.mattermost_incident_channel_id,
            "incident_channel_name": incident_channel_name,
        }

    async def get_post(self, post_id: str) -> MattermostPost:
        return await self._request(
            "GET",
            f"/api/v4/posts/{post_id}",
            error_message="Failed to get Mattermost post",
            event="mattermost.get_post",
            parse=lambda response: MattermostPost.from_api(response.json()),
            mattermost_post_id=post_id,
        )

    async def get_thread_posts(self, post_id: str) -> list[MattermostPost]:
        def parse(response: httpx.Response) -> list[MattermostPost]:
            data = response.json()
            posts = data.get("posts", {})
            order = data.get("order", [])
            if isinstance(posts, dict) and isinstance(order, list):
                return [
                    MattermostPost.from_api(posts[item])
                    for item in order
                    if item in posts and isinstance(posts[item], dict)
                ]
            return []

        return await self._request(
            "GET",
            f"/api/v4/posts/{post_id}/thread",
            error_message="Failed to get Mattermost thread",
            event="mattermost.get_thread",
            parse=parse,
            mattermost_post_id=post_id,
        )

    async def get_channel_name(self, channel_id: str) -> str | None:
        def parse(response: httpx.Response) -> str | None:
            data = response.json()
            return data.get("display_name") or data.get("name")

        return await self._request(
            "GET",
            f"/api/v4/channels/{channel_id}",
            error_message="Failed to get Mattermost channel",
            event="mattermost.get_channel",
            parse=parse,
            mattermost_channel_id=channel_id,
        )

    async def get_user_display_name(self, user_id: str) -> str:
        return await self._request(
            "GET",
            f"/api/v4/users/{user_id}",
            error_message="Failed to get Mattermost user",
            event="mattermost.get_user",
            parse=lambda response: format_user_display(response.json()) or user_id,
            mattermost_user_id=user_id,
        )

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        props: dict | None = None,
        root_id: str | None = None,
    ) -> MattermostPost:
        payload: dict = {"channel_id": channel_id, "message": message}
        if root_id:
            payload["root_id"] = root_id
        if props:
            payload["props"] = props
        return await self._request(
            "POST",
            "/api/v4/posts",
            json=payload,
            error_message="Failed to create Mattermost post",
            event="mattermost.create_post",
            parse=lambda response: MattermostPost.from_api(response.json()),
            mattermost_channel_id=channel_id,
        )

    async def open_dialog(
        self,
        *,
        trigger_id: str,
        url: str,
        dialog: dict,
    ) -> None:
        await self._request(
            "POST",
            "/api/v4/actions/dialogs/open",
            json={"trigger_id": trigger_id, "url": url, "dialog": dialog},
            error_message="Failed to open Mattermost dialog",
            event="mattermost.dialog.open",
        )

    async def fetch_recent_channel_posts(
        self, channel_id: str, *, limit: int
    ) -> list[MattermostPost]:
        per_page = min(max(limit, 1), 200)

        def parse(response: httpx.Response) -> list[MattermostPost]:
            data = response.json()
            posts = data.get("posts", {})
            order = data.get("order", [])
            return [MattermostPost.from_api(posts[post_id]) for post_id in reversed(order)]

        return await self._request(
            "GET",
            f"/api/v4/channels/{channel_id}/posts",
            params={"page": 0, "per_page": per_page},
            error_message="Failed to fetch Mattermost channel posts",
            event="mattermost.fetch_recent_posts",
            parse=parse,
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
