from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from mm_jira_bot.config import Settings
from mm_jira_bot.jira import JiraClient
from mm_jira_bot.logging import configure_logging, log_event
from mm_jira_bot.mattermost import MattermostClient
from mm_jira_bot.repository import (
    AlertTicket,
    AlertTicketRepository,
    create_database_engine,
    create_session_factory,
    init_db,
)
from mm_jira_bot.service import IncidentBotService

logger = logging.getLogger(__name__)


def _datetime_iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _message_preview(message: str, *, limit: int = 160) -> str:
    compact = " ".join(line.strip() for line in message.splitlines() if line.strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."


def _ticket_to_debug_dict(ticket: AlertTicket, *, full: bool = False) -> dict:
    data = {
        "id": ticket.id,
        "mattermost_post_id": ticket.mattermost_post_id,
        "mattermost_channel_id": ticket.mattermost_channel_id,
        "mattermost_channel_name": ticket.mattermost_channel_name,
        "mattermost_message_url": ticket.mattermost_message_url,
        "mattermost_author_id": ticket.mattermost_author_id,
        "mattermost_message_created_at": _datetime_iso(
            ticket.mattermost_message_created_at
        ),
        "mattermost_message_preview": _message_preview(ticket.mattermost_message_text),
        "jira_issue_key": ticket.jira_issue_key,
        "jira_issue_url": ticket.jira_issue_url,
        "valid_incident": ticket.valid_incident,
        "incident_post_id": ticket.incident_post_id,
        "incident_message_url": ticket.incident_message_url,
        "confirmed_by_user_id": ticket.confirmed_by_user_id,
        "confirmed_at": _datetime_iso(ticket.confirmed_at),
        "creation_status": ticket.creation_status,
        "confirmation_status": ticket.confirmation_status,
        "pending_confirmation_by_user_id": ticket.pending_confirmation_by_user_id,
        "pending_confirmation_at": _datetime_iso(ticket.pending_confirmation_at),
        "jira_confirmation_comment_added": ticket.jira_confirmation_comment_added,
        "last_error": ticket.last_error,
        "created_at": _datetime_iso(ticket.created_at),
        "updated_at": _datetime_iso(ticket.updated_at),
    }
    if full:
        data["mattermost_message_text"] = ticket.mattermost_message_text
    return data


DEBUG_ADMIN_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mattermost Jira Bot Debug Admin</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { margin: 24px; }
    header { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    h1 { font-size: 24px; margin: 0; }
    table { border-collapse: collapse; width: 100%; margin-top: 16px; }
    th, td { border-bottom: 1px solid #9995; padding: 8px; text-align: left; vertical-align: top; }
    th { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; }
    button, input, select { font: inherit; }
    button { cursor: pointer; }
    .toolbar { display: flex; gap: 8px; align-items: center; margin: 16px 0; flex-wrap: wrap; }
    .summary { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 16px; }
    .pill { border: 1px solid #9995; border-radius: 6px; padding: 8px 10px; }
    .message { max-width: 420px; }
    .error { color: #b00020; }
    .ok { color: #0b6b2b; }
  </style>
</head>
<body>
  <header>
    <h1>Mattermost Jira Bot Debug Admin</h1>
    <button onclick="loadData()">Refresh</button>
  </header>
  <section id="summary" class="summary"></section>
  <section class="toolbar">
    <label>Status <input id="status" placeholder="failed_jira"></label>
    <label>Limit <input id="limit" type="number" min="1" max="200" value="50"></label>
    <button onclick="loadData()">Apply</button>
    <span id="notice"></span>
  </section>
  <table>
    <thead>
      <tr>
        <th>Post</th><th>Jira</th><th>Status</th><th>Message</th><th>Actions</th>
      </tr>
    </thead>
    <tbody id="alerts"></tbody>
  </table>
  <script>
    async function getJson(url, options) {
      const response = await fetch(url, options);
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || response.statusText);
      return data;
    }
    function escapeHtml(value) {
      return String(value || "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }
    function link(url, text) {
      return url
        ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(text)}</a>`
        : "";
    }
    async function loadData() {
      const notice = document.getElementById("notice");
      notice.textContent = "Loading...";
      try {
        const summary = await getJson("/debug/admin/api/summary");
        document.getElementById("summary").innerHTML = [
          ["Total", summary.total],
          ["Pending Jira", summary.pending_jira],
          ["Failed", summary.failed],
          ["Confirmed", summary.confirmed],
        ].map(([label, value]) => `<div class="pill"><b>${label}</b>: ${value}</div>`).join("");
        const params = new URLSearchParams();
        params.set("limit", document.getElementById("limit").value || "50");
        const status = document.getElementById("status").value.trim();
        if (status) params.set("status", status);
        const rows = await getJson(`/debug/admin/api/alerts?${params}`);
        document.getElementById("alerts").innerHTML = rows.alerts.map((item) => `
          <tr>
            <td>${link(item.mattermost_message_url, item.mattermost_post_id)}</td>
            <td>${item.jira_issue_url ? link(item.jira_issue_url, item.jira_issue_key) : ""}</td>
            <td>${escapeHtml(item.creation_status)}<br>${escapeHtml(item.confirmation_status)}</td>
            <td class="message">${escapeHtml(item.mattermost_message_preview)}<br><span class="error">${escapeHtml(item.last_error)}</span></td>
            <td>
              <button onclick="recreate('${item.mattermost_post_id}', false)">Retry</button>
              <button onclick="recreate('${item.mattermost_post_id}', true)">Force</button>
            </td>
          </tr>
        `).join("");
        notice.textContent = "";
      } catch (error) {
        notice.className = "error";
        notice.textContent = error.message;
      }
    }
    async function recreate(postId, force) {
      const notice = document.getElementById("notice");
      notice.className = "";
      notice.textContent = "Running...";
      try {
        const result = await getJson(`/debug/admin/api/alerts/${postId}/jira/recreate?force=${force}`, {method: "POST"});
        notice.className = "ok";
        notice.textContent = `${result.status}: ${result.jira_issue_key || result.message}`;
        await loadData();
      } catch (error) {
        notice.className = "error";
        notice.textContent = error.message;
      }
    }
    loadData();
  </script>
</body>
</html>
"""


async def websocket_loop(service: IncidentBotService) -> None:
    while True:
        try:
            async for event in service.mattermost.websocket_events():
                await service.handle_websocket_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(logger, logging.ERROR, "mattermost.websocket.failed", error=str(exc))
            await asyncio.sleep(5)


async def pending_work_loop(service: IncidentBotService) -> None:
    while True:
        try:
            await service.process_pending_work()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(logger, logging.ERROR, "pending_work.failed", error=str(exc))
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
                log_event(logger, logging.ERROR, "startup.backfill_failed", error=str(exc))
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
            log_event(
                logger,
                logging.WARNING,
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
        return JSONResponse(
            {"response_type": response.response_type, "text": response.text}
        )

    if settings.debug_admin_enabled:

        @app.get("/debug/admin", response_class=HTMLResponse)
        async def debug_admin() -> HTMLResponse:
            return HTMLResponse(DEBUG_ADMIN_HTML)

        @app.get("/debug/admin/api/summary")
        async def debug_admin_summary() -> dict:
            return service.repository.debug_summary()

        @app.get("/debug/admin/api/alerts")
        async def debug_admin_alerts(
            limit: int = 50, status: str | None = None
        ) -> dict:
            tickets = service.repository.list_alerts(limit=limit, status=status)
            return {
                "alerts": [_ticket_to_debug_dict(ticket) for ticket in tickets],
                "limit": min(max(limit, 1), 200),
                "status": status,
            }

        @app.get("/debug/admin/api/alerts/{post_id}")
        async def debug_admin_alert_detail(post_id: str) -> dict:
            ticket = service.repository.get_by_post_id(post_id)
            if ticket is None:
                raise HTTPException(status_code=404, detail="Alert ticket not found.")
            return _ticket_to_debug_dict(ticket, full=True)

        @app.post("/debug/admin/api/alerts/{post_id}/jira/recreate")
        async def debug_admin_recreate_jira(
            post_id: str, force: bool = False
        ) -> JSONResponse:
            result = await service.debug_recreate_jira_issue(post_id, force=force)
            status_code = 200
            if result.status == "not_found":
                status_code = 404
            elif result.status == "conflict":
                status_code = 409
            elif not result.ok:
                status_code = 502
            return JSONResponse(result.__dict__, status_code=status_code)

    return app
