from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from time import perf_counter
from urllib.parse import parse_qs, urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mm_jira_bot.config import Settings
from mm_jira_bot.debug_admin import register_debug_admin
from mm_jira_bot.jira import JiraClient
from mm_jira_bot.llm import PostmortemLlmClient
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


def _redact_database_url(database_url: str) -> str:
    try:
        parsed = urlsplit(database_url)
    except ValueError:
        return "<invalid>"
    if parsed.password is None:
        return database_url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    username = parsed.username or ""
    userinfo = f"{username}:***@" if username else ""
    return urlunsplit(
        (
            parsed.scheme,
            f"{userinfo}{host}{port}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def _token_format(token: str | None) -> str:
    if not token:
        return "missing"
    if token.count(".") == 2:
        return "jwt_like"
    return "opaque"


async def _run_dependency_check(
    dependency: str,
    check: Callable[[], Awaitable[dict[str, object]]],
) -> bool:
    started_at = perf_counter()
    log.info("startup.preflight.check_started", dependency=dependency)
    try:
        details = await check()
    except Exception as exc:
        log.error(
            "startup.preflight.check_failed",
            dependency=dependency,
            duration_ms=int((perf_counter() - started_at) * 1000),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return False
    log.info(
        "startup.preflight.check_ok",
        dependency=dependency,
        duration_ms=int((perf_counter() - started_at) * 1000),
        **details,
    )
    return True


async def _database_preflight(service: IncidentBotService) -> dict[str, object]:
    summary = await asyncio.to_thread(service.repository.debug_summary)
    return {
        "database_url": _redact_database_url(service.settings.database_url),
        "ticket_total": summary.get("total"),
        "pending_jira": summary.get("pending_jira"),
        "failed": summary.get("failed"),
        "confirmed": summary.get("confirmed"),
    }


async def run_startup_preflight(service: IncidentBotService) -> None:
    settings = service.settings
    log.info(
        "startup.configuration",
        database_url=_redact_database_url(settings.database_url),
        mattermost_url=settings.mattermost_url,
        mattermost_alert_channel_id=settings.mattermost_alert_channel_id,
        mattermost_incident_channel_id=settings.mattermost_incident_channel_id,
        mattermost_bot_user_id=settings.mattermost_bot_user_id,
        enable_websocket=settings.enable_websocket,
        enable_backfill_on_startup=settings.enable_backfill_on_startup,
        interactive_buttons_enabled=(
            settings.interactive_buttons_enabled and bool(settings.service_public_url)
        ),
        jira_base_url=settings.jira_base_url,
        jira_project_key=settings.jira_project_key,
        jira_issue_type=settings.jira_issue_type,
        jira_start_field_configured=bool(settings.jira_start_field),
        jira_end_field_configured=bool(settings.jira_end_field),
        llm_enabled=service.llm is not None,
        llm_base_url=settings.llm_base_url,
        llm_model=settings.llm_model,
        llm_api_token_configured=bool(settings.llm_api_token),
        llm_api_token_format=_token_format(settings.llm_api_token),
        llm_max_tokens=settings.llm_max_tokens,
        llm_thread_max_chars=settings.llm_thread_max_chars,
        llm_postmortem_prompt_customized=settings.llm_postmortem_prompt is not None,
        llm_summary_prompt_customized=settings.llm_summary_prompt is not None,
        llm_stream=settings.llm_stream,
        llm_read_timeout=settings.llm_read_timeout,
    )

    checks: list[tuple[str, Callable[[], Awaitable[dict[str, object]]]]] = [
        ("database", lambda: _database_preflight(service)),
    ]
    mattermost_preflight = getattr(service.mattermost, "preflight_check", None)
    if callable(mattermost_preflight):
        checks.append(("mattermost", mattermost_preflight))
    else:
        log.info(
            "startup.preflight.check_skipped",
            dependency="mattermost",
            reason="client does not expose preflight_check",
        )
    jira_preflight = getattr(service.jira, "preflight_check", None)
    if callable(jira_preflight):
        checks.append(("jira", jira_preflight))
    else:
        log.info(
            "startup.preflight.check_skipped",
            dependency="jira",
            reason="client does not expose preflight_check",
        )
    if service.llm is not None:
        llm_preflight = getattr(service.llm, "preflight_check", None)
        if callable(llm_preflight):
            checks.append(("llm", llm_preflight))
        else:
            log.info(
                "startup.preflight.check_skipped",
                dependency="llm",
                reason="client does not expose preflight_check",
            )
    else:
        log.info(
            "startup.preflight.check_skipped",
            dependency="llm",
            reason="not configured",
        )

    results = await asyncio.gather(
        *[_run_dependency_check(dependency, check) for dependency, check in checks]
    )
    failed_count = len([result for result in results if not result])
    log.info(
        "startup.preflight.completed",
        dependency_count=len(results),
        failed_count=failed_count,
    )


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


async def authorized_users_refresh_loop(service: IncidentBotService) -> None:
    """Periodically re-resolve the allowlist so group membership changes apply."""
    while True:
        await asyncio.sleep(service.settings.mattermost_authorized_refresh_seconds)
        try:
            await service.resolve_authorized_users()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("authorized_users.refresh_failed", error=str(exc))


def create_app(
    settings: Settings | None = None,
    *,
    service: IncidentBotService | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level, settings.log_format)

    if service is None:
        engine = create_database_engine(settings.database_url)
        init_db(engine)
        repository = AlertTicketRepository(create_session_factory(engine))
        mattermost_client = MattermostClient(settings)
        jira_client = JiraClient(settings)
        llm_client = PostmortemLlmClient(settings) if settings.llm_api_token else None
        service = IncidentBotService(
            settings=settings,
            repository=repository,
            mattermost_client=mattermost_client,
            jira_client=jira_client,
            llm_client=llm_client,
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
        await run_startup_preflight(service)
        await service.resolve_authorized_users()
        if settings.enable_backfill_on_startup:
            try:
                await service.backfill_recent_alerts()
            except Exception as exc:
                log.error("startup.backfill_failed", error=str(exc))
        if settings.enable_websocket:
            app.state.background_tasks.append(asyncio.create_task(websocket_loop(service)))
        app.state.background_tasks.append(asyncio.create_task(pending_work_loop(service)))
        if settings.mattermost_authorized_usernames:
            app.state.background_tasks.append(
                asyncio.create_task(authorized_users_refresh_loop(service))
            )
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
                if service.llm is not None:
                    await service.llm.aclose()

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
            log.warning(
                "mattermost.slash_command.invalid_token",
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
        return JSONResponse({"response_type": response.response_type, "text": response.text})

    @app.post("/mattermost/actions/alert")
    async def alert_action(request: Request) -> JSONResponse:
        payload = await request.json()
        context = payload.get("context") or {}

        result = await service.handle_alert_action(
            action=context.get("action", ""),
            alert_post_id=context.get("alert_post_id", ""),
            user_id=payload.get("user_id", ""),
            user_name=payload.get("user_name", ""),
            channel_id=payload.get("channel_id", ""),
            selected_option=context.get("selected_option") or payload.get("selected_option", ""),
            trigger_id=payload.get("trigger_id", ""),
            source=context.get("source", "alert"),
            incident_post_id=context.get("incident_post_id", ""),
        )
        body: dict = {"ephemeral_text": result.message}
        if result.update_attachments is not None:
            # Replace the originating post's controls (e.g. swap "Создать задачу"
            # for the full card) via the Mattermost interactive-action update.
            body["update"] = {"props": {"attachments": result.update_attachments}}
        return JSONResponse(body)

    @app.post("/mattermost/dialogs/feedback")
    async def feedback_dialog(request: Request) -> JSONResponse:
        payload = await request.json()
        result = await service.handle_feedback_dialog_submission(
            user_id=payload.get("user_id", ""),
            state=payload.get("state", ""),
            submission=payload.get("submission") or {},
            cancelled=bool(payload.get("cancelled")),
        )
        if result.message:
            return JSONResponse({"error": result.message})
        return JSONResponse({})

    if settings.debug_admin_enabled:
        register_debug_admin(app, service)

    return app
