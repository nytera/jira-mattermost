# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Canonical docs

This repo keeps its detailed guidance in two files — read them before non-trivial work:

- **[AGENTS.md](AGENTS.md)** — architecture, module responsibilities, the two idempotent flows, Jira field/option resolution, persistence/timezone, style and PR conventions. This is the technical source of truth.
- **[README.md](README.md)** — user-facing setup, configuration (env vars), operational behavior, and the "Сводка: что на что влияет" behavior matrix (single source of truth for which reaction/button does what).

Most documentation lives there; the notes below are only the quick reference and the few things worth flagging up front.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"            # editable install + pytest/ruff (the [test] extra)
python -m mm_jira_bot                # run locally on 0.0.0.0:8080, reads .env
curl http://localhost:8080/healthz   # health check

pytest                                              # full suite (asyncio_mode=auto, pythonpath=src)
pytest tests/test_service.py::test_<name>            # single test
pytest --cov=mm_jira_bot --cov-report=term-missing   # coverage (baseline ~78%)

ruff check src tests                 # lint (rules E,F,I,UP,B,SIM)
ruff format src tests                # format (--check to verify without writing)

docker compose up --build            # run with Postgres
```

## Big picture

A single-process **Mattermost alert → Jira incident** bridge bot (Python 3.11+, FastAPI + async httpx, SQLAlchemy 2.0). `web.py:create_app()` builds the FastAPI app and, in its lifespan, runs three background asyncio tasks against one `IncidentBotService`: startup preflight, the Mattermost WebSocket loop, and a pending-work loop that retries failed Jira creates / pending confirmations (the durability backbone).

`service.py` is the orchestration layer and the only place that coordinates Mattermost + Jira + the repository — **read it first**. `create_app(service=...)` accepts an injected service so tests pass fakes and a temp SQLite DB instead of live clients.

See AGENTS.md for the full module table, the two idempotent flows (alert→issue, confirmation→incident), the test-mode (`JIRA_CREATE_ENABLED=false`) stubbing rules, and Jira field-name→id / createmeta option resolution.

## Things to keep aligned when editing

- **Behavior matrix:** the per-channel/per-control "что на что влияет" table in README.md is the single source of truth for reaction/button behavior — update it instead of duplicating the logic in prose elsewhere.
- **Schema changes:** keep `migrations/` (reference schema, hand-maintained), the SQLAlchemy model in `repository.py`, and `init_db()` startup `create_all`/`ALTER TABLE` expectations in sync. No separate migrator runs locally.
- **Timezone:** all persisted/displayed times go through `domain.backend_now()` / `backend_datetime()` using `INCIDENT_TIMEZONE` (default `Europe/Moscow`). Don't use naive `datetime.now()`.
- **`E501` per-file ignores** (`debug_admin.py`, `jira_payload.py`, `postmortem.py`) exist for long unbreakable literals (embedded SPA CSS/HTML/JS, Russian PM templates) — don't reflow those.
- Add/extend tests for any behavior change, especially idempotency, retry/recovery, slash commands, and Jira payload/option formatting.
```
