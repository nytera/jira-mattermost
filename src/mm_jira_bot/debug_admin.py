from __future__ import annotations

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from mm_jira_bot.logging import LEVEL_NAME_TO_NUMBER, get_log_buffer
from mm_jira_bot.repository import AlertTicket
from mm_jira_bot.service import IncidentBotService


def _datetime_iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _message_preview(message: str, *, limit: int = 160) -> str:
    compact = " ".join(line.strip() for line in message.splitlines() if line.strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."


def _validity_status(ticket: AlertTicket) -> str | None:
    if ticket.valid_incident:
        return "Валидный"
    return ticket.validity_label


def _ticket_to_debug_dict(ticket: AlertTicket, *, full: bool = False) -> dict:
    validity_status = _validity_status(ticket)
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


DEBUG_ADMIN_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Jira Bot · Debug</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #0f1419; --panel: #1a212b; --panel-2: #222b38; --border: #2d3845;
      --text: #e6edf3; --muted: #8b98a8; --accent: #4c8dff; --accent-2: #2563eb;
      --ok: #2ea043; --warn: #d29922; --err: #f85149; --chip: #2d3845;
      --radius: 10px; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    }
    @media (prefers-color-scheme: light) {
      :root {
        --bg: #f5f7fa; --panel: #fff; --panel-2: #f0f3f7; --border: #d8dee6;
        --text: #1a212b; --muted: #5a6675; --chip: #eaeef3;
      }
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-size: 14px; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    button, input, select { font: inherit; color: inherit; }
    button {
      cursor: pointer; background: var(--panel-2); border: 1px solid var(--border);
      border-radius: 8px; padding: 6px 12px; transition: .12s;
    }
    button:hover { border-color: var(--accent); }
    button.primary { background: var(--accent-2); border-color: var(--accent-2); color: #fff; }
    button.primary:hover { filter: brightness(1.1); }
    button.ghost { background: transparent; }
    input, select {
      background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px;
    }
    input:focus, select:focus { outline: none; border-color: var(--accent); }
    header {
      position: sticky; top: 0; z-index: 5; background: var(--panel);
      border-bottom: 1px solid var(--border); padding: 14px 24px;
      display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    }
    header h1 { font-size: 17px; margin: 0; display: flex; align-items: center; gap: 8px; }
    header .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 8px var(--ok); }
    header .spacer { flex: 1; }
    .main { padding: 20px 24px 60px; max-width: 1400px; margin: 0 auto; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 18px; }
    .card {
      background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
      padding: 14px 16px; cursor: pointer; transition: .12s;
    }
    .card:hover { border-color: var(--accent); }
    .card.active { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
    .card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
    .card .value { font-size: 26px; font-weight: 600; margin-top: 4px; }
    .panel { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
    .panel-head { padding: 12px 16px; border-bottom: 1px solid var(--border); display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .panel-head h2 { font-size: 14px; margin: 0; }
    .grow { flex: 1; }
    .create-bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 14px 16px; background: var(--panel-2); border-bottom: 1px solid var(--border); }
    .create-bar input { flex: 1; min-width: 260px; }
    .tabs { display: flex; gap: 4px; }
    .tab { padding: 6px 14px; border-radius: 8px 8px 0 0; border: 1px solid transparent; cursor: pointer; color: var(--muted); }
    .tab.active { color: var(--text); background: var(--panel); border-color: var(--border); border-bottom-color: var(--panel); }
    table { border-collapse: collapse; width: 100%; }
    th, td { padding: 9px 14px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--border); }
    th { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
    tbody tr:hover { background: var(--panel-2); }
    .message { max-width: 460px; word-break: break-word; }
    .preview { color: var(--text); }
    .err-text { color: var(--err); font-size: 12px; display: block; margin-top: 4px; word-break: break-word; }
    .mono { font-family: var(--mono); font-size: 12px; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 600; background: var(--chip); white-space: nowrap; }
    .badge.ok { background: color-mix(in srgb, var(--ok) 22%, transparent); color: var(--ok); }
    .badge.warn { background: color-mix(in srgb, var(--warn) 22%, transparent); color: var(--warn); }
    .badge.err { background: color-mix(in srgb, var(--err) 22%, transparent); color: var(--err); }
    .timecell { min-width: 160px; }
    .time-row { margin-bottom: 5px; }
    .time-label { display: inline-block; min-width: 44px; color: var(--muted); font-size: 11px; }
    .time-value { font-family: var(--mono); font-size: 12px; white-space: nowrap; }
    .age { color: var(--muted); font-family: var(--mono); font-size: 11px; }
    .validity-timer {
      display: inline-block; margin-top: 6px; padding: 3px 8px; border-radius: 7px;
      border: 1px solid currentColor; font-family: var(--mono); font-size: 11px;
    }
    .actions { display: flex; gap: 6px; flex-wrap: wrap; }
    .actions button { padding: 4px 10px; font-size: 12px; }
    .notice { font-size: 13px; min-height: 18px; }
    .notice.ok { color: var(--ok); } .notice.err { color: var(--err); } .notice.muted { color: var(--muted); }
    .empty { padding: 30px; text-align: center; color: var(--muted); }
    /* logs */
    .logs { font-family: var(--mono); font-size: 12.5px; max-height: 70vh; overflow: auto; padding: 8px 0; }
    .logline { display: flex; gap: 10px; padding: 3px 16px; border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent); }
    .logline:hover { background: var(--panel-2); }
    .logline .ts { color: var(--muted); white-space: nowrap; }
    .logline .lvl { width: 56px; font-weight: 600; }
    .logline.INFO .lvl { color: var(--accent); }
    .logline.WARNING .lvl { color: var(--warn); }
    .logline.ERROR .lvl, .logline.CRITICAL .lvl { color: var(--err); }
    .logline.DEBUG .lvl { color: var(--muted); }
    .logline .body { flex: 1; word-break: break-word; }
    .logline .ev { font-weight: 600; }
    .logline .kv { color: var(--muted); }
    .logline .kv b { color: var(--text); font-weight: 500; }
    .logline .exc { white-space: pre-wrap; color: var(--err); display: block; margin-top: 4px; }
    /* modal */
    .overlay { position: fixed; inset: 0; background: rgba(0,0,0,.55); display: none; align-items: center; justify-content: center; z-index: 20; }
    .overlay.open { display: flex; }
    .modal { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); width: min(720px, 92vw); max-height: 85vh; overflow: auto; }
    .modal-head { display: flex; align-items: center; gap: 10px; padding: 14px 18px; border-bottom: 1px solid var(--border); position: sticky; top: 0; background: var(--panel); }
    .modal-head h3 { margin: 0; font-size: 15px; }
    .kvtable { width: 100%; }
    .kvtable td { font-size: 12.5px; }
    .kvtable td.k { color: var(--muted); width: 240px; font-family: var(--mono); }
    .kvtable td.v { font-family: var(--mono); word-break: break-word; }
    label.inline { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; }
  </style>
</head>
<body>
  <header>
    <h1><span class="dot"></span> Jira Bot · Debug</h1>
    <span class="spacer"></span>
    <label class="inline"><input type="checkbox" id="autoRefresh"> авто-обновление</label>
    <select id="interval">
      <option value="5000">5s</option>
      <option value="10000" selected>10s</option>
      <option value="30000">30s</option>
    </select>
    <button class="primary" onclick="refreshActive()">Обновить</button>
  </header>

  <div class="main">
    <div class="cards" id="cards"></div>

    <div class="tabs">
      <div class="tab active" data-tab="alerts" onclick="switchTab('alerts')">Алерты</div>
      <div class="tab" data-tab="logs" onclick="switchTab('logs')">Логи</div>
    </div>

    <!-- ALERTS -->
    <section class="panel" id="tab-alerts">
      <div class="create-bar">
        <input id="createLink" placeholder="Ссылка на алерт или post id — создать задачу">
        <button class="primary" onclick="createFromLink()">Создать задачу</button>
        <span id="createNotice" class="notice muted"></span>
      </div>
      <div class="panel-head">
        <input id="search" placeholder="Поиск: post id, ключ, текст…" oninput="renderAlerts()">
        <input id="status" placeholder="статус" list="statuses" style="width:150px">
        <datalist id="statuses">
          <option value="pending_jira"><option value="jira_created"><option value="failed_jira">
          <option value="confirmed"><option value="confirming"><option value="pending_confirmation"><option value="failed_confirmation">
        </datalist>
        <button class="ghost" id="validityFilterButton" onclick="applyValidity('empty')">Пустая валидность</button>
        <label class="inline">лимит <input id="limit" type="number" min="1" max="200" value="50" style="width:72px"></label>
        <button onclick="loadAlerts()">Применить</button>
        <span class="grow"></span>
        <span id="alertsNotice" class="notice muted"></span>
      </div>
      <table>
        <thead><tr><th>Post</th><th>Jira</th><th>Создано</th><th>Статус</th><th>Сообщение</th><th></th></tr></thead>
        <tbody id="alerts"></tbody>
      </table>
      <div class="empty" id="alertsEmpty" style="display:none">Нет записей</div>
    </section>

    <!-- LOGS -->
    <section class="panel" id="tab-logs" style="display:none">
      <div class="panel-head">
        <select id="logLevel" onchange="loadLogs()">
          <option value="">все уровни</option>
          <option value="INFO">INFO+</option>
          <option value="WARNING">WARNING+</option>
          <option value="ERROR">ERROR+</option>
        </select>
        <input id="logSearch" placeholder="фильтр по тексту…" oninput="renderLogs()" class="grow">
        <label class="inline"><input type="checkbox" id="logTail" checked> к концу</label>
        <label class="inline">строк <input id="logLimit" type="number" min="50" max="2000" value="300" style="width:80px"></label>
        <button onclick="loadLogs()">Обновить</button>
        <span id="logsNotice" class="notice muted"></span>
      </div>
      <div class="logs" id="logs"></div>
    </section>
  </div>

  <div class="overlay" id="overlay" onclick="if(event.target===this)closeModal()">
    <div class="modal">
      <div class="modal-head">
        <h3 id="modalTitle">Детали</h3>
        <span class="grow"></span>
        <button class="ghost" onclick="closeModal()">✕</button>
      </div>
      <div id="modalBody"></div>
    </div>
  </div>

  <script>
    let timer = null;
    let alertsCache = [];
    let logsCache = [];
    let validityFilter = "";

    async function getJson(url, options) {
      const response = await fetch(url, options);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.message || data.detail || response.statusText);
      return data;
    }
    function escapeHtml(value) {
      return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[c]));
    }
    function link(url, text) {
      return url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(text)}</a>` : "";
    }
    function statusBadge(value) {
      const v = String(value || "");
      let cls = "";
      if (["jira_created", "confirmed"].includes(v)) cls = "ok";
      else if (["failed_jira", "failed_confirmation"].includes(v)) cls = "err";
      else if (["pending_jira", "pending_confirmation", "confirming"].includes(v)) cls = "warn";
      return `<span class="badge ${cls}">${escapeHtml(v)}</span>`;
    }
    function parseDate(value) {
      if (!value) return null;
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? null : date;
    }
    function formatDate(value) {
      const date = parseDate(value);
      if (!date) return "—";
      return date.toLocaleString("ru-RU", {
        day: "2-digit", month: "2-digit", year: "numeric",
        hour: "2-digit", minute: "2-digit",
      });
    }
    function ageMsSince(value) {
      const date = parseDate(value);
      return date ? Math.max(0, Date.now() - date.getTime()) : null;
    }
    function formatAge(ms) {
      if (ms == null) return "—";
      const totalMinutes = Math.floor(ms / 60000);
      const days = Math.floor(totalMinutes / 1440);
      const hours = Math.floor((totalMinutes % 1440) / 60);
      const minutes = totalMinutes % 60;
      if (days > 0) return `${days}д ${hours}ч`;
      if (hours > 0) return `${hours}ч ${minutes}м`;
      return `${minutes}м`;
    }
    function emptyValidityAgeSource(it) {
      return it.mattermost_message_created_at || it.created_at;
    }
    function emptyValidityTimer(it) {
      if (!it.validity_is_empty) return it.validity_status
        ? `<span class="badge ok">${escapeHtml(it.validity_status)}</span>`
        : `<span class="badge">—</span>`;
      const ms = ageMsSince(emptyValidityAgeSource(it));
      const maxMs = 3 * 24 * 60 * 60 * 1000;
      const ratio = Math.min(1, (ms || 0) / maxMs);
      const hue = Math.round(42 - ratio * 38);
      const color = `hsl(${hue} 82% 56%)`;
      const bg = `hsla(${hue}, 82%, 56%, ${0.13 + ratio * 0.19})`;
      return `<span class="validity-timer" style="color:${color};background:${bg}" title="Максимальная краснота через 3 дня">пусто · ${formatAge(ms)}</span>`;
    }
    function renderCreatedCell(it) {
      const alertAge = formatAge(ageMsSince(it.mattermost_message_created_at));
      const taskAge = formatAge(ageMsSince(it.created_at));
      return `<div class="timecell">
        <div class="time-row"><span class="time-label">Алерт</span><span class="time-value">${formatDate(it.mattermost_message_created_at)}</span> <span class="age">${alertAge}</span></div>
        <div class="time-row"><span class="time-label">Задача</span><span class="time-value">${formatDate(it.created_at)}</span> <span class="age">${taskAge}</span></div>
      </div>`;
    }

    function switchTab(name) {
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
      document.getElementById("tab-alerts").style.display = name === "alerts" ? "" : "none";
      document.getElementById("tab-logs").style.display = name === "logs" ? "" : "none";
      window._tab = name;
      refreshActive();
    }
    function refreshActive() {
      loadSummary();
      if (window._tab === "logs") loadLogs(); else loadAlerts();
    }

    async function loadSummary() {
      try {
        const s = await getJson("/debug/admin/api/summary");
        const active = document.getElementById("status").value.trim();
        const cards = [
          ["Всего", s.total, "", ""],
          ["Без Jira", s.pending_jira, "pending_jira", ""],
          ["Ошибки", s.failed, "failed_jira", ""],
          ["Подтверждено", s.confirmed, "confirmed", ""],
          ["Пустая Валидность", s.empty_validity, "", "empty"],
        ];
        document.getElementById("cards").innerHTML = cards.map(([label, value, filter, validity]) => `
          <div class="card ${(filter && filter === active) || (validity && validity === validityFilter) ? "active" : ""}" onclick="${validity ? `applyValidity('${validity}')` : `applyStatus('${filter}')`}">
            <div class="label">${label}</div><div class="value">${value}</div>
          </div>`).join("");
      } catch (e) { /* summary is best-effort */ }
    }
    function applyStatus(filter) {
      document.getElementById("status").value = filter || "";
      validityFilter = "";
      document.getElementById("validityFilterButton").classList.remove("primary");
      loadAlerts();
    }
    function applyValidity(filter) {
      validityFilter = validityFilter === filter ? "" : filter;
      document.getElementById("status").value = "";
      document.getElementById("validityFilterButton").classList.toggle("primary", Boolean(validityFilter));
      loadAlerts();
    }

    async function loadAlerts() {
      const notice = document.getElementById("alertsNotice");
      notice.className = "notice muted"; notice.textContent = "Загрузка…";
      try {
        const params = new URLSearchParams();
        params.set("limit", document.getElementById("limit").value || "50");
        const status = document.getElementById("status").value.trim();
        if (status) params.set("status", status);
        if (validityFilter) params.set("validity", validityFilter);
        const rows = await getJson(`/debug/admin/api/alerts?${params}`);
        alertsCache = rows.alerts;
        notice.textContent = `${rows.alerts.length} записей`;
        renderAlerts();
        loadSummary();
      } catch (e) {
        notice.className = "notice err"; notice.textContent = e.message;
      }
    }
    function renderAlerts() {
      const q = document.getElementById("search").value.trim().toLowerCase();
      const items = alertsCache.filter((it) => !q ||
        [it.mattermost_post_id, it.jira_issue_key, it.mattermost_message_preview, it.mattermost_channel_name]
          .some((f) => String(f || "").toLowerCase().includes(q)));
      document.getElementById("alertsEmpty").style.display = items.length ? "none" : "";
      document.getElementById("alerts").innerHTML = items.map((it) => `
        <tr>
          <td>${link(it.mattermost_message_url, it.mattermost_post_id.slice(0, 8))}
              <div class="mono" style="color:var(--muted)">${escapeHtml(it.mattermost_channel_name || "")}</div></td>
          <td>${it.jira_issue_url ? link(it.jira_issue_url, it.jira_issue_key) : "<span class='badge'>—</span>"}</td>
          <td>${renderCreatedCell(it)}</td>
          <td>${statusBadge(it.creation_status)}<br>${statusBadge(it.confirmation_status)}
              <br>${emptyValidityTimer(it)}</td>
          <td class="message"><span class="preview">${escapeHtml(it.mattermost_message_preview)}</span>
              ${it.last_error ? `<span class="err-text">${escapeHtml(it.last_error)}</span>` : ""}</td>
          <td><div class="actions">
            <button onclick="recreate('${it.mattermost_post_id}', false)">Retry</button>
            <button onclick="recreate('${it.mattermost_post_id}', true)">Force</button>
            <button class="ghost" onclick="showDetail('${it.mattermost_post_id}')">Детали</button>
          </div></td>
        </tr>`).join("");
    }

    async function createFromLink() {
      const input = document.getElementById("createLink");
      const notice = document.getElementById("createNotice");
      const link = input.value.trim();
      if (!link) { input.focus(); return; }
      notice.className = "notice muted"; notice.textContent = "Создаю…";
      try {
        const r = await getJson("/debug/admin/api/alerts/create-from-link",
          { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ link }) });
        notice.className = "notice " + (r.ok ? "ok" : "err");
        notice.textContent = (r.jira_issue_key ? r.jira_issue_key + " · " : "") + r.message;
        if (r.ok) input.value = "";
        await loadAlerts();
      } catch (e) {
        notice.className = "notice err"; notice.textContent = e.message;
      }
    }

    async function recreate(postId, force) {
      const notice = document.getElementById("alertsNotice");
      notice.className = "notice muted"; notice.textContent = force ? "Force recreate…" : "Retry…";
      try {
        const r = await getJson(`/debug/admin/api/alerts/${postId}/jira/recreate?force=${force}`, { method: "POST" });
        notice.className = "notice ok"; notice.textContent = `${r.status}: ${r.jira_issue_key || r.message}`;
        await loadAlerts();
      } catch (e) {
        notice.className = "notice err"; notice.textContent = e.message;
      }
    }

    async function showDetail(postId) {
      const overlay = document.getElementById("overlay");
      document.getElementById("modalTitle").textContent = postId;
      document.getElementById("modalBody").innerHTML = "<div class='empty'>Загрузка…</div>";
      overlay.classList.add("open");
      try {
        const d = await getJson(`/debug/admin/api/alerts/${postId}`);
        const rows = Object.entries(d).map(([k, v]) => {
          let val = v;
          if (k.endsWith("_url") && v) val = link(v, v);
          else val = escapeHtml(v == null ? "—" : v);
          return `<tr><td class="k">${escapeHtml(k)}</td><td class="v">${val}</td></tr>`;
        }).join("");
        document.getElementById("modalBody").innerHTML = `<table class="kvtable"><tbody>${rows}</tbody></table>`;
      } catch (e) {
        document.getElementById("modalBody").innerHTML = `<div class="empty err-text">${escapeHtml(e.message)}</div>`;
      }
    }
    function closeModal() { document.getElementById("overlay").classList.remove("open"); }

    async function loadLogs() {
      const notice = document.getElementById("logsNotice");
      notice.className = "notice muted"; notice.textContent = "Загрузка…";
      try {
        const params = new URLSearchParams();
        params.set("limit", document.getElementById("logLimit").value || "300");
        const level = document.getElementById("logLevel").value;
        if (level) params.set("level", level);
        const r = await getJson(`/debug/admin/api/logs?${params}`);
        logsCache = r.logs;
        notice.textContent = r.available ? `${r.logs.length} строк` : "буфер логов недоступен";
        renderLogs();
      } catch (e) {
        notice.className = "notice err"; notice.textContent = e.message;
      }
    }
    function renderLogs() {
      const q = document.getElementById("logSearch").value.trim().toLowerCase();
      const box = document.getElementById("logs");
      const items = logsCache.filter((l) => !q ||
        (l.message + " " + l.logger + " " + JSON.stringify(l.fields)).toLowerCase().includes(q));
      box.innerHTML = items.map((l) => {
        const fields = Object.entries(l.fields || {})
          .map(([k, v]) => `<span class="kv"><b>${escapeHtml(k)}</b>=${escapeHtml(v)}</span>`).join(" ");
        const exc = l.exception ? `<span class="exc">${escapeHtml(l.exception)}</span>` : "";
        const ts = (l.timestamp || "").replace("T", " ").slice(0, 19);
        return `<div class="logline ${escapeHtml(l.level)}">
          <span class="ts">${escapeHtml(ts)}</span>
          <span class="lvl">${escapeHtml(l.level)}</span>
          <span class="body"><span class="ev">${escapeHtml(l.message)}</span> ${fields}${exc}</span>
        </div>`;
      }).join("");
      if (document.getElementById("logTail").checked) box.scrollTop = box.scrollHeight;
    }

    function setupTimer() {
      if (timer) clearInterval(timer);
      if (document.getElementById("autoRefresh").checked) {
        timer = setInterval(refreshActive, parseInt(document.getElementById("interval").value, 10));
      }
    }
    document.getElementById("autoRefresh").addEventListener("change", setupTimer);
    document.getElementById("interval").addEventListener("change", setupTimer);
    document.getElementById("createLink").addEventListener("keydown", (e) => { if (e.key === "Enter") createFromLink(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
    setInterval(() => { if (window._tab === "alerts") renderAlerts(); }, 60000);

    window._tab = "alerts";
    refreshActive();
  </script>
</body>
</html>
"""


def register_debug_admin(app: FastAPI, service: IncidentBotService) -> None:
    @app.get("/debug/admin", response_class=HTMLResponse)
    async def debug_admin() -> HTMLResponse:
        return HTMLResponse(DEBUG_ADMIN_HTML)

    @app.get("/debug/admin/api/summary")
    async def debug_admin_summary() -> dict:
        return service.repository.debug_summary()

    @app.get("/debug/admin/api/alerts")
    async def debug_admin_alerts(
        limit: int = 50,
        status: str | None = None,
        validity: str | None = None,
    ) -> dict:
        tickets = service.repository.list_alerts(
            limit=limit,
            status=status,
            validity=validity,
        )
        return {
            "alerts": [_ticket_to_debug_dict(ticket) for ticket in tickets],
            "limit": min(max(limit, 1), 200),
            "status": status,
            "validity": validity,
        }

    @app.get("/debug/admin/api/logs")
    async def debug_admin_logs(limit: int = 300, level: str | None = None) -> dict:
        buffer = get_log_buffer()
        if buffer is None:
            return {"logs": [], "available": False}
        min_levelno = LEVEL_NAME_TO_NUMBER.get((level or "").upper(), 0)
        limit = min(max(limit, 1), 2000)
        return {
            "logs": buffer.records(limit=limit, min_levelno=min_levelno),
            "available": True,
        }

    @app.post("/debug/admin/api/alerts/create-from-link")
    async def debug_admin_create_from_link(
        link: str = Body(..., embed=True),
    ) -> JSONResponse:
        result = await service.debug_create_from_link(link)
        return JSONResponse(result.__dict__)

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
