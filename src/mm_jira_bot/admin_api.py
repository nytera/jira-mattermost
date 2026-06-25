"""Admin UI backend: JSON API (`/admin/api/*`) + static SPA mount (`/admin`).

Replaces the old debug panel. ``register_admin_api`` attaches the JSON routes
(all behind a Bearer-token dependency) inline on ``app`` so the AST scanner in
``scripts/gen_service_map.py`` keeps picking them up; ``mount_admin_ui`` serves
the built React bundle from ``admin_static/`` (no-op when the build is absent, so
``pip install -e`` and tests work without Node). Auth model: a single shared
``ADMIN_UI_TOKEN`` — there is no per-user identity, so front the service with a
reverse proxy / firewall (see ``docs/admin-ui.md``).
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mm_jira_bot.config import Settings
from mm_jira_bot.domain import ConfirmationResult, ConfirmationStatus
from mm_jira_bot.logging import LEVEL_NAME_TO_NUMBER, get_log_buffer, get_logger
from mm_jira_bot.postmortem import DEFAULT_POSTMORTEM_PROMPT, DEFAULT_SUMMARY_PROMPT
from mm_jira_bot.repository import AlertTicket
from mm_jira_bot.service import (
    _PROMPT_KEY_POSTMORTEM,
    _PROMPT_KEY_SUMMARY,
    IncidentBotService,
)

log = get_logger(__name__)

_ADMIN_STATIC_DIR = Path(__file__).resolve().parent / "admin_static"

# Runtime-editable prompt templates surfaced in the Settings page. Each entry
# binds a DB-override key to its UI label and built-in default.
_EDITABLE_PROMPTS: tuple[tuple[str, str, str], ...] = (
    (_PROMPT_KEY_SUMMARY, "Саммари треда (Mattermost)", DEFAULT_SUMMARY_PROMPT),
    (_PROMPT_KEY_POSTMORTEM, "Постмортем (Jira)", DEFAULT_POSTMORTEM_PROMPT),
)


def _datetime_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid ended_at; use ISO 8601.") from exc


def _prompt_settings_payload(service: IncidentBotService) -> dict:
    """Effective prompt per editable key plus its source (db/env/default) so the
    UI can show where the value comes from and offer a reset-to-default."""
    prompts = []
    for key, label, default in _EDITABLE_PROMPTS:
        db_value = service.repository.get_setting(key)
        env_value = service._prompt_env_default(key)
        if db_value is not None:
            value, source = db_value, "db"
        elif env_value:
            value, source = env_value, "env"
        else:
            value, source = default, "default"
        prompts.append(
            {"key": key, "label": label, "value": value, "source": source, "default": default}
        )
    return {"prompts": prompts}


def _message_preview(message: str, *, limit: int = 160) -> str:
    compact = " ".join(line.strip() for line in message.splitlines() if line.strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."


def _validity_status(ticket: AlertTicket) -> str | None:
    if ticket.valid_incident:
        return "Валидный"
    return ticket.validity_label


def _ticket_to_dict(ticket: AlertTicket, *, full: bool = False) -> dict:
    validity_status = _validity_status(ticket)
    data = {
        "id": ticket.id,
        "mattermost_post_id": ticket.mattermost_post_id,
        "mattermost_channel_id": ticket.mattermost_channel_id,
        "mattermost_channel_name": ticket.mattermost_channel_name,
        "mattermost_message_url": ticket.mattermost_message_url,
        "mattermost_author_id": ticket.mattermost_author_id,
        "mattermost_message_created_at": _datetime_iso(ticket.mattermost_message_created_at),
        "mattermost_alert_title": ticket.mattermost_alert_title,
        "mattermost_message_preview": _message_preview(ticket.mattermost_message_text),
        "jira_issue_key": ticket.jira_issue_key,
        "jira_issue_url": ticket.jira_issue_url,
        "valid_incident": ticket.valid_incident,
        "incident_post_id": ticket.incident_post_id,
        "incident_message_url": ticket.incident_message_url,
        "confirmed_by_user_id": ticket.confirmed_by_user_id,
        "confirmed_at": _datetime_iso(ticket.confirmed_at),
        "resolved_at": _datetime_iso(ticket.resolved_at),
        "creation_status": ticket.creation_status,
        "confirmation_status": ticket.confirmation_status,
        "pending_confirmation_by_user_id": ticket.pending_confirmation_by_user_id,
        "pending_confirmation_at": _datetime_iso(ticket.pending_confirmation_at),
        "jira_confirmation_comment_added": ticket.jira_confirmation_comment_added,
        "validity_label": ticket.validity_label,
        "validity_status": validity_status,
        "validity_is_empty": validity_status is None,
        "last_error": ticket.last_error,
        "created_at": _datetime_iso(ticket.created_at),
        "updated_at": _datetime_iso(ticket.updated_at),
    }
    if full:
        data["mattermost_message_text"] = ticket.mattermost_message_text
    return data


def _confirmation_response(result: ConfirmationResult) -> JSONResponse:
    status_code = 200
    if result.status == ConfirmationStatus.NOT_FOUND:
        status_code = 404
    elif result.status == ConfirmationStatus.ERROR:
        status_code = 502
    return JSONResponse(
        {
            "status": result.status.value,
            "message": result.message,
            "jira_issue_url": result.jira_issue_url,
            "incident_message_url": result.incident_message_url,
        },
        status_code=status_code,
    )


def _require_token(settings: Settings):
    """Build the Bearer-token auth dependency for the admin API.

    No configured token → 503 (misconfiguration, not a client error). Missing or
    wrong header → 401. ``secrets.compare_digest`` avoids timing leaks.
    """

    async def verify(authorization: str | None = Header(default=None)) -> None:
        token = settings.admin_ui_token
        if not token:
            raise HTTPException(status_code=503, detail="Admin UI token is not configured.")
        if not secrets.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(
                status_code=401,
                detail="Missing or invalid admin token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return verify


def register_admin_api(app: FastAPI, service: IncidentBotService) -> None:
    auth = Depends(_require_token(service.settings))
    repo = service.repository

    @app.get("/admin/api/stats", dependencies=[auth])
    async def admin_stats() -> dict:
        return await asyncio.to_thread(repo.admin_stats)

    @app.get("/admin/api/summary", dependencies=[auth])
    async def admin_summary() -> dict:
        return await asyncio.to_thread(repo.stats_summary)

    @app.get("/admin/api/alerts", dependencies=[auth])
    async def admin_alerts(
        limit: int = 50, status: str | None = None, validity: str | None = None
    ) -> dict:
        tickets = await asyncio.to_thread(
            repo.list_alerts, limit=limit, status=status, validity=validity
        )
        return {
            "alerts": [_ticket_to_dict(ticket) for ticket in tickets],
            "limit": min(max(limit, 1), 200),
            "status": status,
            "validity": validity,
        }

    @app.get("/admin/api/alerts/{post_id}", dependencies=[auth])
    async def admin_alert_detail(post_id: str) -> dict:
        ticket = await asyncio.to_thread(repo.get_by_post_id, post_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail="Alert ticket not found.")
        return _ticket_to_dict(ticket, full=True)

    @app.get("/admin/api/alerts/{post_id}/feedback", dependencies=[auth])
    async def admin_alert_feedback(post_id: str) -> dict:
        feedback = await asyncio.to_thread(repo.list_feedback, post_id)
        return {
            "feedback": [
                {
                    "id": item.id,
                    "user_id": item.user_id,
                    "user_display_name": item.user_display_name,
                    "message": item.message,
                    "created_at": _datetime_iso(item.created_at),
                }
                for item in feedback
            ]
        }

    @app.get("/admin/api/logs", dependencies=[auth])
    async def admin_logs(limit: int = 300, level: str | None = None) -> dict:
        buffer = get_log_buffer()
        if buffer is None:
            return {"logs": [], "available": False}
        min_levelno = LEVEL_NAME_TO_NUMBER.get((level or "").upper(), 0)
        return {
            "logs": buffer.records(limit=min(max(limit, 1), 2000), min_levelno=min_levelno),
            "available": True,
        }

    @app.get("/admin/api/settings", dependencies=[auth])
    async def admin_settings() -> dict:
        return await asyncio.to_thread(_prompt_settings_payload, service)

    @app.post("/admin/api/settings/{key}", dependencies=[auth])
    async def admin_save_setting(key: str, value: str = Body(..., embed=True)) -> JSONResponse:
        if key not in {prompt_key for prompt_key, _, _ in _EDITABLE_PROMPTS}:
            raise HTTPException(status_code=404, detail="Unknown setting key.")
        await asyncio.to_thread(repo.set_setting, key, value)
        return JSONResponse(await asyncio.to_thread(_prompt_settings_payload, service))

    @app.post("/admin/api/settings/{key}/reset", dependencies=[auth])
    async def admin_reset_setting(key: str) -> JSONResponse:
        if key not in {prompt_key for prompt_key, _, _ in _EDITABLE_PROMPTS}:
            raise HTTPException(status_code=404, detail="Unknown setting key.")
        await asyncio.to_thread(repo.delete_setting, key)
        return JSONResponse(await asyncio.to_thread(_prompt_settings_payload, service))

    @app.post("/admin/api/alerts/create-from-link", dependencies=[auth])
    async def admin_create_from_link(link: str = Body(..., embed=True)) -> JSONResponse:
        result = await service.admin_create_from_link(link)
        return JSONResponse(result.__dict__)

    @app.post("/admin/api/alerts/{post_id}/jira/recreate", dependencies=[auth])
    async def admin_recreate_jira(post_id: str, force: bool = False) -> JSONResponse:
        result = await service.admin_recreate_jira_issue(post_id, force=force)
        status_code = 200
        if result.status == "not_found":
            status_code = 404
        elif result.status == "conflict":
            status_code = 409
        elif not result.ok:
            status_code = 502
        return JSONResponse(result.__dict__, status_code=status_code)

    @app.post("/admin/api/alerts/{post_id}/confirm", dependencies=[auth])
    async def admin_confirm(post_id: str) -> JSONResponse:
        return _confirmation_response(await service.admin_confirm_incident(post_id))

    @app.post("/admin/api/alerts/{post_id}/end", dependencies=[auth])
    async def admin_end(
        post_id: str, ended_at: str | None = Body(default=None, embed=True)
    ) -> JSONResponse:
        result = await service.admin_end_incident(post_id, ended_at=_parse_iso(ended_at))
        return _confirmation_response(result)

    @app.post("/admin/api/alerts/{post_id}/validity", dependencies=[auth])
    async def admin_validity(
        post_id: str, validity_label: str = Body(..., embed=True)
    ) -> JSONResponse:
        result = await service.admin_set_validity(post_id, validity_label=validity_label)
        return _confirmation_response(result)

    @app.post("/admin/api/alerts/{post_id}/postmortem", dependencies=[auth])
    async def admin_postmortem(post_id: str) -> JSONResponse:
        return _confirmation_response(await service.admin_generate_postmortem(post_id))

    @app.post("/admin/api/alerts/{post_id}/summary", dependencies=[auth])
    async def admin_summary_action(post_id: str) -> JSONResponse:
        result = await service.admin_generate_summary(post_id)
        return JSONResponse({"message": result.message})


def mount_admin_ui(app: FastAPI) -> None:
    """Serve the built React SPA from ``admin_static/`` with client-side routing.

    The asset mount and SPA catch-all are registered last (after the JSON API and
    the bot's own routes) so ``/admin/api/*``, ``/healthz``, ``/metrics`` and the
    Mattermost endpoints keep precedence. No-op (warning only) when the build is
    missing, so the API and tests run without a Node build. Served unauthenticated
    on purpose — the browser must load the bundle before it has the token; the API
    behind it enforces auth."""
    index = _ADMIN_STATIC_DIR / "index.html"
    if not index.exists():
        log.warning("admin_ui.build_missing", path=str(_ADMIN_STATIC_DIR))
        return

    assets = _ADMIN_STATIC_DIR / "assets"
    if assets.is_dir():
        app.mount("/admin/assets", StaticFiles(directory=assets), name="admin-assets")

    @app.get("/admin")
    async def admin_index() -> FileResponse:
        return FileResponse(index)

    @app.get("/admin/{path:path}")
    async def admin_spa(path: str) -> FileResponse:
        return FileResponse(index)
