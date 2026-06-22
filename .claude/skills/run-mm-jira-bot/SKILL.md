---
name: run-mm-jira-bot
description: Run, launch, start, or smoke-test the Mattermost→Jira incident bot (FastAPI service). Use to drive its HTTP surface (healthz, debug admin, slash/action endpoints) offline without a live Mattermost/Jira, or to verify a change works in the running app.
---

# Run mm_jira_bot

`mm_jira_bot` is a FastAPI service that bridges Mattermost alerts to Jira
incidents. It has **no GUI** — the surface is HTTP on `0.0.0.0:8080` plus two
background asyncio loops (Mattermost WebSocket, pending-work retry). Those loops
and the startup preflight are **non-fatal**, so the HTTP server comes up even
with bogus Mattermost/Jira config. That is what lets you drive it offline.

The agent path is a stdlib Python driver:
**`.claude/skills/run-mm-jira-bot/driver.py`**. It launches `python -m
mm_jira_bot` with a self-contained config (dummy externals, throwaway SQLite,
WebSocket off, debug-admin on), waits for `/healthz`, exercises six endpoints,
prints PASS/FAIL, and shuts the server down.

All paths below are relative to the repo root (the `<unit>`).

## Prerequisites

- Python 3.11+ (verified on 3.14). No OS packages, no browser, no xvfb.
- Runtime deps: `fastapi`, `uvicorn[standard]`, `httpx`, `SQLAlchemy`,
  `psycopg[binary]`, `websockets`. The driver itself needs only the stdlib.

If the deps are not importable yet, install them (editable, with test extras):

```bash
python -m pip install -e ".[test]"
```

The README's `python -m venv .venv` first is the clean-machine path; skip it if
you already have an interpreter with the deps (check with
`python -c "import fastapi, uvicorn, sqlalchemy"`).

## Run (agent path) — the driver

From the repo root:

```bash
python .claude/skills/run-mm-jira-bot/driver.py
```

Expected tail (exit code 0):

```
Driving HTTP surface:
  [PASS] GET /healthz -> {ok:true} | 200 {"ok":true}
  [PASS] GET /debug/admin serves HTML | status 200
  [PASS] GET /debug/admin/api/summary -> counters | 200 {"total":0,...
  [PASS] GET /debug/admin/api/alerts -> list | 200 {"alerts":[],...
  [PASS] POST /mattermost/slash/incident (bad link) -> error text | 200 ...
  [PASS] POST /mattermost/actions/alert (unknown) -> ephemeral_text | 200 ...

RESULT: all checks PASSED
```

On Windows, prefix with `PYTHONIOENCODING=utf-8` (Git Bash) or
`$env:PYTHONIOENCODING='utf-8';` (PowerShell) — endpoint bodies contain Russian
text and the console may otherwise mojibake the captured output (the checks
still pass; it's display-only).

The interleaved `preflight failed ... getaddrinfo failed` and
`mattermost.action.post_lookup_failed` lines are **expected** — Mattermost/Jira
are deliberately unreachable. They are warnings/errors from non-fatal loops, not
driver failures; trust the `[PASS]/[FAIL]` lines and the exit code.

### Keep it running to poke by hand

```bash
python .claude/skills/run-mm-jira-bot/driver.py --serve
```

Leaves the server up (same self-contained config) so you can `curl` it or open
`http://localhost:8080/debug/admin` in a browser. Ctrl-C to stop.

### What the driver covers

The driver drives the layers a typical change here touches — the HTTP endpoints
in `web.py` and `debug_admin.py`, dispatching into `service.py`. For a change to
an internal pure module (`jira_payload.py`, `formatting.py`, `actions.py`,
`summary.py`) the faster handle is a direct unit test — see **Test** below.

## Run (human path)

```bash
cp -n .env.example .env   # -n: never clobber an existing real .env
python -m mm_jira_bot      # serves 0.0.0.0:8080; Ctrl-C to stop
curl http://localhost:8080/healthz
```

Fill the freshly created `.env` with real `MATTERMOST_*`/`JIRA_*` values before
the run is useful.

Without real Mattermost/Jira credentials this connects to nothing useful; it's
the production entry point, not the way to exercise the app locally. Use the
driver instead.

## Test

```bash
python -m pytest -q
```

88 tests pass (`asyncio_mode=auto`, `pythonpath=src`); they use fake
Mattermost/Jira clients and a temp SQLite DB, so no live services are touched.
Single test: `python -m pytest tests/test_service.py::test_<name>`.

## Gotchas

- **A working-copy `.env` overrides the driver's intent.** `config.from_env()`
  loads `.env` from the cwd, but only for keys not already in the environment.
  The driver therefore sets the load-bearing vars *explicitly* (including
  blanking `MATTERMOST_SLASH_TOKEN`, `SERVICE_PUBLIC_URL`, `LLM_API_TOKEN`). The
  first version of the driver hit a real `403 Invalid slash command token`
  because the repo `.env` had a live slash token — hence the explicit blanks.
- **Port is hard-coded to 8080** in `__main__.py` (`uvicorn.run(... port=8080)`),
  and the driver launches that module, so the port is not overridable — just
  leave 8080 free (or stop whatever holds it). An early driver had a `PORT` env
  knob; it was a trap, because it moved only the *poller*, not the server, so
  the smoke "failed to become healthy" while the server was happily on 8080.
- **The unknown-action check still calls Mattermost** (`get_post`) and so logs a
  `post_lookup_failed` after the retry budget. The driver sets
  `API_RETRY_ATTEMPTS=1` to keep that to ~1s instead of the default 4 attempts
  with backoff.
- **`empty_validity` count** and the SPA at `/debug/admin` only exist when
  `DEBUG_ADMIN_ENABLED=true`; the driver sets it. With it off those routes 404.

## Troubleshooting

- `FAILED: server did not become healthy` — a missing dep or an import error
  killed the subprocess during startup. Re-run; the uvicorn/traceback prints
  above the driver's own output. Confirm deps with
  `python -c "import fastapi, uvicorn, sqlalchemy, httpx, websockets"`.
- `RuntimeError: Missing required environment variable: ...` — you ran
  `python -m mm_jira_bot` directly without a complete `.env`. Use the driver
  (it supplies a full dummy config) or fill `.env`.
- Mojibake (`�`) in the output on Windows — set `PYTHONIOENCODING=utf-8`; it's
  cosmetic, the checks are unaffected.
