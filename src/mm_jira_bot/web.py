from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from time import perf_counter
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from mm_jira_bot.admin_api import mount_admin_ui, register_admin_api
from mm_jira_bot.audit import AuditMirror
from mm_jira_bot.config import Settings
from mm_jira_bot.jira import JiraClient
from mm_jira_bot.llm import PostmortemLlmClient
from mm_jira_bot.logging import configure_logging, get_logger
from mm_jira_bot.mattermost import MattermostClient
from mm_jira_bot.metrics import register_ticket_collector
from mm_jira_bot.ops import OpsNotifier
from mm_jira_bot.repository import (
    AlertTicketRepository,
    create_database_engine,
    create_session_factory,
    init_db,
)
from mm_jira_bot.service import IncidentBotService

log = get_logger(__name__)

# Env vars removed in 0.9.0 (the no-write/stub behaviour is now driven solely by
# READ_ONLY_MODE). Warn loudly if a deployment still sets one — silently ignoring
# JIRA_CREATE_ENABLED=false on upgrade would turn a no-write deploy into a live
# prod-writing one.
_REMOVED_ENV_VARS = ("JIRA_CREATE_ENABLED", "JIRA_STUB_ISSUE_KEY")


def _warn_removed_env_vars() -> None:
    for name in _REMOVED_ENV_VARS:
        if os.environ.get(name):
            log.warning(
                "config.removed_env_var",
                variable=name,
                detail=(
                    f"{name} was removed in 0.9.0 and is now ignored; the no-write / "
                    "stub behaviour is driven solely by READ_ONLY_MODE=true. Set "
                    "READ_ONLY_MODE if you relied on the old stub mode."
                ),
            )


def _assert_audit_channel_isolated(settings: Settings) -> None:
    """Refuse to start if the audit channel collides with any channel the bot
    reads or the prod bot writes.

    The audit post is the single write the read-only backstop permits; if the
    audit channel equals a real/test/ops channel, that "safe" write lands in a
    real prod channel and breaks the zero-prod-impact guarantee. A dedicated
    channel is mandatory, so this is fatal rather than a warning.
    """
    audit = settings.mattermost_audit_channel_id
    if not audit:
        return
    handled = {
        "alert": settings.mattermost_alert_channel_id,
        "incident": settings.mattermost_incident_channel_id,
        "test_alert": settings.mattermost_test_alert_channel_id,
        "test_incident": settings.mattermost_test_incident_channel_id,
        "ops": settings.mattermost_ops_channel_id,
    }
    collisions = [
        name for name, channel_id in handled.items() if channel_id and channel_id == audit
    ]
    if collisions:
        raise RuntimeError(
            "MATTERMOST_AUDIT_CHANNEL_ID must be a dedicated channel in read-only mode; "
            f"it collides with: {', '.join(collisions)}"
        )


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
    check: Callable[[], Awaitable[dict[str, Any]]],
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


async def _database_preflight(service: IncidentBotService) -> dict[str, Any]:
    summary = await asyncio.to_thread(service.repository.stats_summary)
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
        mattermost_ops_channel_id=settings.mattermost_ops_channel_id,
        mattermost_bot_user_id=settings.mattermost_bot_user_id,
        metrics_enabled=settings.metrics_enabled,
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

    checks: list[tuple[str, Callable[[], Awaitable[dict[str, Any]]]]] = [
        ("database", lambda: _database_preflight(service)),
    ]
    mattermost_preflight = getattr(service.mattermost, "preflight_check", None)
    if callable(mattermost_preflight):
        checks.append(
            ("mattermost", cast(Callable[[], Awaitable[dict[str, Any]]], mattermost_preflight))
        )
    else:
        log.info(
            "startup.preflight.check_skipped",
            dependency="mattermost",
            reason="client does not expose preflight_check",
        )
    jira_preflight = getattr(service.jira, "preflight_check", None)
    if callable(jira_preflight):
        checks.append(("jira", cast(Callable[[], Awaitable[dict[str, Any]]], jira_preflight)))
    else:
        log.info(
            "startup.preflight.check_skipped",
            dependency="jira",
            reason="client does not expose preflight_check",
        )
    if service.llm is not None:
        llm_preflight = getattr(service.llm, "preflight_check", None)
        if callable(llm_preflight):
            checks.append(("llm", cast(Callable[[], Awaitable[dict[str, Any]]], llm_preflight)))
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


# The only firehose events the bot acts on. Everything else (typing, presence,
# channel_viewed, …) is filtered out before we spawn a task, so the read loop
# stays cheap. parse_posted_event/parse_reaction_event re-check this anyway.
_HANDLED_WS_EVENTS = frozenset({"posted", "reaction_added"})


async def _handle_ws_event(service: IncidentBotService, event: dict) -> None:
    """Run one websocket event off the read loop.

    Handling can take many seconds (postmortem/summary = LLM + Jira calls). Doing
    it inline stalls the socket read, fills the websockets receive buffer, pauses
    the transport and times out the keepalive ping (1011 disconnect). Each event
    therefore runs as its own task; its errors are logged here since the loop's
    own ``except`` no longer wraps them.
    """
    try:
        await service.handle_websocket_event(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error(
            "mattermost.event.handler_failed",
            error_type=type(exc).__name__,
            error=str(exc),
            exc_info=True,
        )


async def websocket_loop(service: IncidentBotService) -> None:
    handlers: set[asyncio.Task[None]] = set()
    while True:
        try:
            async for event in service.mattermost.websocket_events():
                if event.get("event") not in _HANDLED_WS_EVENTS:
                    continue
                # Off-load handling so a long postmortem never blocks the read
                # loop (and the keepalive). Keep a strong ref until it finishes.
                task = asyncio.create_task(_handle_ws_event(service, event))
                handlers.add(task)
                task.add_done_callback(handlers.discard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "mattermost.websocket.failed",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=True,
            )
            await asyncio.sleep(5)


async def pending_work_loop(service: IncidentBotService) -> None:
    while True:
        try:
            await service.process_pending_work()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "pending_work.failed",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=True,
            )
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
            log.error(
                "authorized_users.refresh_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=True,
            )


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

    # Read-only (shadow) mode: refuse to start on a colliding audit channel, then
    # wire the mirror so suppressed Mattermost writes are reproduced there.
    if settings.read_only_mode:
        _assert_audit_channel_isolated(settings)
        if settings.mattermost_audit_channel_id:
            service.mattermost.audit = AuditMirror(service.mattermost, settings)
        else:
            log.warning(
                "readonly.no_audit_channel",
                detail=(
                    "READ_ONLY_MODE is on but MATTERMOST_AUDIT_CHANNEL_ID is unset; "
                    "all writes are suppressed and mirrored nowhere"
                ),
            )

    # Self-health observability: the ops handler counts every error event
    # (bot_errors_total) and, when an ops channel is set, posts it; the metrics
    # collector exposes ticket gauges on /metrics scrape.
    ops_notifier: OpsNotifier | None = None
    if settings.mattermost_ops_channel_id or settings.metrics_enabled:
        ops_notifier = OpsNotifier(service.mattermost, settings)
        ops_notifier.install()
    if settings.metrics_enabled:
        register_ticket_collector(service.repository)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.service = service
        app.state.owns_clients = owns_clients
        app.state.background_tasks = []
        _warn_removed_env_vars()
        if settings.read_only_mode:
            log.warning(
                "readonly.enabled",
                database_url=_redact_database_url(settings.database_url),
                audit_channel=settings.mattermost_audit_channel_id,
                test_alert_channel=settings.mattermost_test_alert_channel_id,
                test_incident_channel=settings.mattermost_test_incident_channel_id,
            )
        # Bind the ops queue before preflight so early startup errors (preflight,
        # backfill) buffer instead of being dropped before drain() starts.
        if ops_notifier is not None and ops_notifier.posts_to_channel:
            ops_notifier.activate()
        await run_startup_preflight(service)
        await service.resolve_authorized_users()
        if settings.enable_backfill_on_startup:
            try:
                await service.backfill_recent_alerts()
            except Exception as exc:
                log.error(
                    "startup.backfill_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    exc_info=True,
                )
        if settings.enable_websocket:
            app.state.background_tasks.append(asyncio.create_task(websocket_loop(service)))
        app.state.background_tasks.append(asyncio.create_task(pending_work_loop(service)))
        if ops_notifier is not None and ops_notifier.posts_to_channel:
            app.state.background_tasks.append(asyncio.create_task(ops_notifier.drain()))
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

    @app.middleware("http")
    async def error_boundary(request: Request, call_next):
        """Last-resort error boundary for HTTP endpoints.

        An unhandled exception in a route otherwise yields a bare 500 with no
        structured event. Here it becomes an ``ERROR`` event (with traceback)
        that the ops handler counts/forwards, and a clean JSON 500 for the
        client. ``CancelledError`` is propagated so shutdown stays cooperative.
        """
        try:
            return await call_next(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "http.request.failed",
                method=request.method,
                path=request.url.path,
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=True,
            )
            return JSONResponse({"error": "Internal server error."}, status_code=500)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    if settings.metrics_enabled:

        @app.get("/metrics")
        async def metrics() -> Response:
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/mattermost/slash/incident")
    async def incident_slash_command(request: Request) -> JSONResponse:
        try:
            raw_body = (await request.body()).decode("utf-8")
        except UnicodeDecodeError:
            log.warning("http.request.bad_body", path=request.url.path)
            return JSONResponse(
                {"response_type": "ephemeral", "text": "Malformed request body."},
                status_code=400,
            )
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

    async def _parse_json_body(request: Request) -> dict | None:
        """Return the JSON body, or ``None`` after logging a malformed payload."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            log.warning("http.request.bad_json", path=request.url.path)
            return None
        return body if isinstance(body, dict) else {}

    @app.post("/mattermost/actions/alert")
    async def alert_action(request: Request) -> JSONResponse:
        payload = await _parse_json_body(request)
        if payload is None:
            return JSONResponse({"error": "Malformed request body."}, status_code=400)
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
        payload = await _parse_json_body(request)
        if payload is None:
            return JSONResponse({"error": "Malformed request body."}, status_code=400)
        result = await service.handle_feedback_dialog_submission(
            user_id=payload.get("user_id", ""),
            state=payload.get("state", ""),
            submission=payload.get("submission") or {},
            cancelled=bool(payload.get("cancelled")),
        )
        if result.message:
            return JSONResponse({"error": result.message})
        return JSONResponse({})

    if settings.admin_ui_enabled:
        register_admin_api(app, service)
        mount_admin_ui(app)

    return app
