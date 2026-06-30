# Admin UI (frontend)

The admin UI is a single-page app in `web_ui/` that drives the bot through the
JSON API at `/admin/api/*`. Backend and auth model live in
[admin.md](domains/admin.md); this file covers the SPA.

## Stack

Vite + React 18 + TypeScript, styled with Tailwind. Routing via
`react-router-dom`, icons via `lucide-react`. Fonts are self-hosted, not from a
CDN: `@fontsource/hanken-grotesk` and `@fontsource/ibm-plex-mono` are imported in
`src/main.tsx`.

## Pages

The shell renders a login screen until a token is present, then a `Layout` with
five routes:

- **Dashboard** (`/`) — instrument readouts (open incidents, MTTA, MTTR, pending
  Jira, errors), a daily alert-volume signal, an Episode Stream grouped by
  `alert_signature` (root + repeats), and validity / Jira-creation distributions
  plus top channels.
- **Incidents** (`/incidents`) — filterable table (all / open / no-validity); a
  row opens a slide-over with full detail and lifecycle actions:
  confirm, end, set validity, generate postmortem, generate summary.
- **Alerts** (`/alerts`) — raw alert tickets. Create a Jira issue from a
  Mattermost link or post id, and create / recreate the Jira issue per row.
- **Settings** (`/settings`) — override the LLM prompts (thread summary,
  postmortem) live. Each shows its source (db / env / default) with save and
  reset-to-default.
- **Logs** (`/logs`) — the in-memory ring buffer of process log records, with
  level and text filters.

## Auth flow

Bearer-token only — no usernames. The login form stashes the token in
`localStorage` (key `mmjira.admin.token`), then probes `GET /admin/api/summary`
to validate it; on success the app is unlocked, on failure the token is cleared
and an error shown. Every request sends `Authorization: Bearer <token>`. Any
`401` clears the token and bounces back to the login screen.

## Build & serving

`npm run build` (`tsc --noEmit && vite build`) emits the bundle to `web_ui/dist/`.
There is no local step that copies it into the app — that happens only in the
Docker build. Both `web_ui/dist/` and `src/mm_jira_bot/admin_static/` are
gitignored.

FastAPI serves the built bundle via `admin_api.mount_admin_ui`, which reads
`src/mm_jira_bot/admin_static/` and mounts it under `/admin` (assets at
`/admin/assets`, SPA catch-all at `/admin/{path}`). When the build is absent it
no-ops with a warning, so `pip install -e` and the test suite run without Node.

- **Dev:** `npm run dev` serves the SPA on `:5173` and proxies `/admin/api` to the
  bot on `:8080` (see `vite.config.ts`).
- **Prod:** the multi-stage `Dockerfile` has a `node:22` stage that runs
  `npm run build` and copies `web_ui/dist` into `src/mm_jira_bot/admin_static`,
  so the Python image ships the bundle.

## Security

There is one shared `ADMIN_UI_TOKEN` and no per-user identity, so the UI gives no
audit trail of who did what. The SPA bundle itself is served unauthenticated by
design — the browser must load it before it has a token; the `/admin/api/*`
routes behind it enforce the Bearer check. Put the service behind a reverse proxy
or firewall; do not expose it directly.
