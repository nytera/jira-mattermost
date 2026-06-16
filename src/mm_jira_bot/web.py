from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mm_jira_bot.config import Settings
from mm_jira_bot.debug_admin import register_debug_admin
from mm_jira_bot.jira import JiraClient
from mm_jira_bot.logging import configure_logging, get_logger
from mm_jira_bot.mattermost import MattermostClient
from mm_jira_bot.repository import (
    AlertTicketRepository,
    create_database_engine,
    create_session_factory,
    init_db,
)
from mm_jira_bot.service import IncidentBotService

log = get_logger(__name__)


async def websocket_loop(service: IncidentBotService) -> None:
    while True:
        try:
            async for event in service.mattermost.websocket_events():
                await service.handle_websocket_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("mattermost.websocket.failed", error=str(exc))
            await asyncio.sleep(5)


async def pending_work_loop(service: IncidentBotService) -> None:
    while True:
        try:
            await service.process_pending_work()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("pending_work.failed", error=str(exc))
        await asyncio.sleep(service.settings.pending_work_interval_seconds)


def create_app(
    settings: Settings | None = None,
    *,
    service: IncidentBotService | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)

    if service is None:
        engine = create_database_engine(settings.database_url)
        init_db(engine)
        repository = AlertTicketRepository(create_session_factory(engine))
        mattermost_client = MattermostClient(settings)
        jira_client = JiraClient(settings)
        service = IncidentBotService(
            settings=settings,
            repository=repository,
            mattermost_client=mattermost_client,
            jira_client=jira_client,
        )
        owns_clients = True
    else:
        owns_clients = False

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.service = service
        app.state.owns_clients = owns_clients
        app.state.background_tasks = []
        if settings.enable_backfill_on_startup:
            try:
                await service.backfill_recent_alerts()
            except Exception as exc:
                log.error("startup.backfill_failed", error=str(exc))
        if settings.enable_websocket:
            app.state.background_tasks.append(asyncio.create_task(websocket_loop(service)))
        app.state.background_tasks.append(asyncio.create_task(pending_work_loop(service)))
        try:
            yield
        finally:
            for task in app.state.background_tasks:
                task.cancel()
            for task in app.state.background_tasks:
                with suppress(asyncio.CancelledError):
                    await task
            if app.state.owns_clients:
                await service.mattermost.aclose()
                await service.jira.aclose()

    app = FastAPI(title="Mattermost Jira Incident Bot", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/mattermost/slash/incident")
    async def incident_slash_command(request: Request) -> JSONResponse:
        raw_body = (await request.body()).decode("utf-8")
        form = {key: values[0] for key, values in parse_qs(raw_body).items()}

        slash_token = settings.mattermost_slash_token
        if slash_token and form.get("token") != slash_token:
            log.warning("mattermost.slash_command.invalid_token",
                user_id=form.get("user_id"),
            )
            return JSONResponse(
                {"response_type": "ephemeral", "text": "Invalid slash command token."},
                status_code=403,
            )

        response = await service.handle_slash_command(
            user_id=form.get("user_id", ""),
            text=form.get("text", ""),
        )
        return JSONResponse(
            {"response_type": response.response_type, "text": response.text}
        )

    if settings.debug_admin_enabled:
        register_debug_admin(app, service)

    return app
