from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from typing import Any, cast

from mm_jira_bot.domain import (
    JiraIssue,
    MattermostPost,
    ReactionEvent,
)
from mm_jira_bot.repository import (
    AlertTicketRepository,
    create_database_engine,
    create_session_factory,
    init_db,
)
from mm_jira_bot.service import IncidentBotService

POST_ID = "abcdefghijklmnopqrstuvwx01"


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    return cast(dict[str, object], cast(Any, record).extra_fields)


class FakeMattermostClient:
    def __init__(self) -> None:
        self.posts: dict[str, MattermostPost] = {}
        self.created_posts: list[dict] = []
        self.updated_posts: list[dict] = []
        self.display_names: dict[str, str] = {}
        self.username_to_id: dict[str, str] = {}
        self.usernames_lookups: list[list[str]] = []
        self.group_name_to_id: dict[str, str] = {}
        self.group_members: dict[str, set[str]] = {}
        self.group_lookups: list[list[str]] = []
        self.reactions: list[tuple[str, str]] = []
        self.bot_user_id_from_api = "bot-user"
        self.adopted_bot_user_id: str | None = None

    async def add_reaction(self, post_id: str, emoji_name: str) -> None:
        self.reactions.append((post_id, emoji_name))

    async def fetch_bot_user_id(self) -> str:
        return self.bot_user_id_from_api

    def adopt_resolved_bot_user_id(self, bot_user_id: str) -> None:
        self.adopted_bot_user_id = bot_user_id

    def permalink(self, post_id: str) -> str:
        return f"https://mattermost.example.com/_redirect/pl/{post_id}"

    async def get_channel_name(self, channel_id: str) -> str:
        return "alerts"

    async def get_post(self, post_id: str) -> MattermostPost:
        return self.posts[post_id]

    async def get_thread_posts(self, post_id: str):
        root = self.posts[post_id]
        replies = [post for post in self.posts.values() if post.root_id == post_id]
        return sorted([root, *replies], key=lambda post: (post.create_at, post.id))

    async def get_user_display_name(self, user_id: str) -> str:
        if user_id in self.display_names:
            return self.display_names[user_id]
        return f"@{user_id}"

    async def get_user_ids_by_usernames(self, usernames: list[str]) -> dict[str, str]:
        self.usernames_lookups.append(list(usernames))
        return {
            name: self.username_to_id[name] for name in usernames if name in self.username_to_id
        }

    async def get_group_ids_by_names(self, names: list[str]) -> dict[str, str]:
        self.group_lookups.append(list(names))
        return {
            name: self.group_name_to_id[name] for name in names if name in self.group_name_to_id
        }

    async def get_group_member_ids(self, group_id: str) -> set[str]:
        return set(self.group_members.get(group_id, set()))

    async def create_post(
        self,
        *,
        channel_id: str,
        message: str,
        props: dict | None = None,
        root_id: str | None = None,
    ):
        post = MattermostPost(
            id=f"incidentpost{len(self.created_posts):014d}",
            channel_id=channel_id,
            user_id="bot-user",
            message=message,
            create_at=1_700_000_100_000,
            root_id=root_id,
            props=props,
        )
        self.created_posts.append(
            {
                "channel_id": channel_id,
                "message": message,
                "props": props,
                "root_id": root_id,
                "post": post,
            }
        )
        self.posts[post.id] = post
        return post

    async def update_post(self, post_id: str, *, message=None, props=None) -> None:
        self.updated_posts.append({"post_id": post_id, "message": message, "props": props})
        if post_id in self.posts:
            changes = {}
            if message is not None:
                changes["message"] = message
            if props is not None:
                changes["props"] = props
            self.posts[post_id] = replace(self.posts[post_id], **changes)

    async def fetch_recent_channel_posts(self, channel_id: str, *, limit: int):
        return []

    async def aclose(self) -> None:
        return None


class FakeJiraClient:
    def __init__(self) -> None:
        self.created_payloads: list[dict] = []
        self.valid_updates: list[tuple[str, bool]] = []
        self.comments: list[tuple[str, str, str]] = []
        self.valid_by_issue: dict[str, bool] = {}
        self.validity_updates: list[tuple[str, str]] = []
        self.validity_end_updates: list[tuple[str, datetime]] = []
        self.end_updates: list[tuple[str, datetime]] = []
        self.time_to_fix_updates: list[tuple[str, int]] = []
        self.validity_by_issue: dict[str, str] = {}
        self.descriptions: list[tuple[str, str]] = []
        self.postmortem_payloads: list[dict] = []
        self.generic_comments: list[tuple[str, str]] = []
        self.links: list[tuple[str, str]] = []

    async def link_child_of(self, child_key: str, parent_key: str) -> None:
        self.links.append((child_key, parent_key))

    async def create_issue(
        self,
        post,
        *,
        message_url: str,
        channel_name: str | None,
    ):
        key = f"OPS-{len(self.created_payloads) + 1}"
        self.created_payloads.append(
            {
                "post": post,
                "message_url": message_url,
                "channel_name": channel_name,
            }
        )
        self.valid_by_issue[key] = False
        return JiraIssue(key=key, url=f"https://jira.example.com/browse/{key}")

    async def create_postmortem_issue(
        self,
        post,
        *,
        message_url: str,
        channel_name: str | None,
        summary: str,
        description: str,
    ):
        key = f"OPS-{len(self.created_payloads) + 1}"
        self.created_payloads.append(
            {
                "post": post,
                "message_url": message_url,
                "channel_name": channel_name,
            }
        )
        self.postmortem_payloads.append(
            {
                "post": post,
                "message_url": message_url,
                "channel_name": channel_name,
                "summary": summary,
                "description": description,
            }
        )
        self.valid_by_issue[key] = False
        return JiraIssue(key=key, url=f"https://jira.example.com/browse/{key}")

    async def get_valid_incident(self, issue_key: str):
        return self.valid_by_issue.get(issue_key, False)

    async def set_valid_incident(self, issue_key: str, value: bool):
        self.valid_updates.append((issue_key, value))
        self.valid_by_issue[issue_key] = value

    async def set_validity(
        self, issue_key: str, option_value: str, *, ended_at: datetime | None = None
    ):
        self.validity_updates.append((issue_key, option_value))
        if ended_at is not None:
            self.validity_end_updates.append((issue_key, ended_at))
        self.validity_by_issue[issue_key] = option_value

    async def set_end_time(self, issue_key: str, ended_at: datetime):
        self.end_updates.append((issue_key, ended_at))

    async def set_time_to_fix(self, issue_key: str, minutes: int):
        self.time_to_fix_updates.append((issue_key, minutes))

    async def set_description(self, issue_key: str, description: str):
        self.descriptions.append((issue_key, description))

    async def add_confirmation_comment(
        self, issue_key: str, *, incident_message_url: str, confirmed_by_user_id: str
    ):
        self.comments.append((issue_key, incident_message_url, confirmed_by_user_id))

    async def add_comment(self, issue_key: str, body: str):
        self.generic_comments.append((issue_key, body))

    async def aclose(self) -> None:
        return None


class FakeLlmClient:
    def __init__(
        self,
        report: str = (
            "[INC] 15.11.2023 - Ошибки API\n"
            "Участники инцидента: Иван Иванов (@ivanov.ivan), Петр Петров (@petrov.petr)\n"
            "Автор постмортема: Иван Иванов (@ivanov.ivan)\n"
            "##Сводка\n"
            "API начал отвечать 500.\n"
            "##Решение\n"
            "Откатили релиз.\n"
            "##Извлеченные уроки\n"
            "###Что было сделано хорошо / В чем повезло\n"
            " - Быстро нашли проблему.\n"
            "###Что пошло не так / В чем не повезло\n"
            " - не указано\n"
            "##Action Items\n"
            " - Завести алерт на рост 500.\n"
            "##Хронология\n"
            "01:15 - Обнаружили проблему\n"
            "01:20 - Откатили релиз"
        ),
    ) -> None:
        self.report = report
        # Closeout (END + title) prompts seen at incident finalize.
        self.prompts: list[str] = []
        self.summary_prompts: list[str] = []
        self.summary = "Суть: всё сломалось.\nСтатус: в работе."
        # Closeout end-time answer; default UNKNOWN so tests keep the legacy
        # reaction-time behavior unless they opt into a derived value. The title
        # line of the closeout answer reuses the report's first `[INC] …` line.
        self.end_time = "UNKNOWN"

    @property
    def closeout_title(self) -> str:
        first = self.report.splitlines()[0].strip() if self.report.strip() else ""
        return first or "[INC] Инцидент"

    async def resolve_incident_closeout(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return f"END: {self.end_time}\nTITLE: {self.closeout_title}"

    async def generate_summary(self, prompt: str, *, on_progress=None) -> str:
        self.summary_prompts.append(prompt)
        if on_progress is not None:
            await on_progress(self.summary)
        return self.summary

    async def aclose(self) -> None:
        return None


def make_alert(
    post_id: str = POST_ID,
    channel_id: str = "alerts-channel",
    message: str = "CPU usage is above 95%",
    props: dict | None = None,
    is_bot: bool = True,
    post_type: str = "",
) -> MattermostPost:
    post_props = {"from_webhook": "true"} if is_bot else {}
    if props:
        post_props.update(props)
    return MattermostPost(
        id=post_id,
        channel_id=channel_id,
        user_id="author-user",
        message=message,
        create_at=1_700_000_000_000,
        channel_name="alerts",
        props=post_props,
        post_type=post_type,
    )


class _FakeStreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _capture_bot_logs(records: list[logging.LogRecord]):
    """Attach a record collector to ``mm_jira_bot``.

    ``create_app`` runs ``configure_logging`` which clears root handlers (and so
    pytest's ``caplog``), so capture on the bot logger after the app is built.
    """
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("mm_jira_bot")
    logger.addHandler(handler)
    return logger, handler


def _build_service(settings):
    engine = create_database_engine(settings.database_url)
    init_db(engine)
    repository = AlertTicketRepository(create_session_factory(engine))
    return IncidentBotService(
        settings=settings,
        repository=repository,
        mattermost_client=FakeMattermostClient(),
        jira_client=FakeJiraClient(),
    )


def _issue_replies(service, post_id, *, issue_key="OPS-1"):
    return [
        created
        for created in service.mattermost.created_posts
        if created["root_id"] == post_id
        and (created["props"] or {}).get("jira_issue_key") == issue_key
    ]


def _issue_reply(service, post_id, *, issue_key="OPS-1"):
    replies = _issue_replies(service, post_id, issue_key=issue_key)
    assert len(replies) == 1
    return replies[0]


def _reply_text(reply):
    """Visible text of a bot thread reply, whether it is a bare message or a
    boxed attachment notice (plain notices render as a single colored block)."""
    if reply.get("message"):
        return reply["message"]
    attachments = (reply.get("props") or {}).get("attachments") or []
    return attachments[0].get("text", "") if attachments else ""


def _manual_post(
    post_id="incidentroot00000000000002", *, user_id="human", props=None, root_id=None
):
    return MattermostPost(
        id=post_id,
        channel_id="incidents-channel",
        user_id=user_id,
        message="Лежим в проде, 500 на /pay",
        create_at=1_700_000_000_000,
        channel_name="incidents",
        root_id=root_id,
        props=props,
    )


# --- Time to fix ------------------------------------------------------------


async def _confirm_and_close_incident(service, *, closed_at_ms: int):
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    ticket = service.repository.get_by_post_id(post.id)
    return await service.handle_reaction(
        ReactionEvent(
            post_id=ticket.incident_post_id,
            user_id="validator",
            emoji_name="white_check_mark",
            create_at=closed_at_ms,
        )
    )


# --- Incident validity emoji + postmortem idempotency -----------------------


async def _confirmed_incident(service):
    """Confirm an alert into an incident and return its ticket."""
    post = make_alert()
    service.mattermost.posts[post.id] = post
    await service.handle_alert_post(post)
    await service.handle_reaction(
        ReactionEvent(post_id=post.id, user_id="validator", emoji_name="incident", create_at=1)
    )
    return service.repository.get_by_post_id(post.id)
