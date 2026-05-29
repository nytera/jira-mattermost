# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.11+ service that bridges Mattermost alerts to Jira incidents. Source code lives in `src/mm_jira_bot/`, with the runnable entry point in `src/mm_jira_bot/__main__.py`. Core modules are split by concern: `mattermost.py` and `jira.py` for external clients, `service.py` for orchestration, `repository.py` for persistence, `web.py` for FastAPI routes, and `config.py` for environment-backed settings. Tests live in `tests/`; current coverage is concentrated in `tests/test_service.py`. Database schema reference SQL is in `migrations/`, and container setup is in `Dockerfile` plus `docker-compose.yml`.

## Build, Test, and Development Commands

- `python -m venv .venv && source .venv/bin/activate`: create and enter a local virtual environment.
- `pip install -e ".[test]"`: install the package in editable mode with pytest dependencies.
- `python -m mm_jira_bot`: run the bot locally using `.env` configuration.
- `curl http://localhost:8080/healthz`: check the local FastAPI health endpoint.
- `pytest`: run the test suite configured by `pyproject.toml`.
- `docker compose up --build`: build and run the bot with Postgres.

## Coding Style & Naming Conventions

Follow the existing style: four-space indentation, type hints, `from __future__ import annotations`, dataclasses for simple value objects, and small modules with explicit responsibilities. Use snake_case for functions, variables, and module names; use PascalCase for classes. Keep async boundaries clear for Mattermost, Jira, and service methods. No formatter or linter is configured, so keep diffs focused and consistent with nearby code.

## Testing Guidelines

Use pytest and pytest-asyncio; async tests are enabled with `asyncio_mode = "auto"`. Name test files `test_*.py` and test functions `test_<behavior>`. Prefer fake clients and temporary SQLite databases, as in `tests/test_service.py`, to avoid live Mattermost, Jira, or Postgres dependencies. Add or update tests for behavior changes, especially idempotency, retry/recovery paths, slash commands, and Jira payload formatting.

## Commit & Pull Request Guidelines

Git history currently uses concise, imperative commit subjects, for example `Initial Mattermost Jira incident bot`. Keep commits small and focused. Pull requests should describe the behavior change, list verification commands and results, note configuration or migration impact, and link related issues. Include screenshots or request/response examples only when changing user-visible Mattermost messages or HTTP behavior.

## Security & Configuration Tips

Copy `.env.example` to `.env` for local development and never commit real tokens. Treat Jira API tokens, Mattermost tokens, channel IDs, and database URLs as secrets. When changing schema behavior, keep `migrations/001_create_alert_tickets.sql`, SQLAlchemy models, and startup initialization expectations aligned.
