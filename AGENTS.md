# Repository Guidelines

Conventions for AI agents and contributors. This file is **only** style/testing/PR
conventions and the gate. Architecture, domains, and operations live in
[`docs/`](docs/); user-facing setup is in [`README.md`](README.md) (Russian); the
"what" (file tree, signatures, routes, MRO) is the generated
[`docs/reference/service-map.md`](docs/reference/service-map.md). Start from
[`CLAUDE.md`](CLAUDE.md) — it routes a task to the right document.

A `Mattermost alert → Jira incident` bridge bot. Python 3.11+, FastAPI + async
`httpx`, SQLAlchemy 2.0.

## Build, test, and development commands

- `python -m venv .venv && source .venv/bin/activate` — create and enter a venv.
- `pip install -e ".[test]"` — editable install with pytest, ruff, Pyright.
- `python -m mm_jira_bot` — run locally on `0.0.0.0:8080`, reads `.env`.
- `curl http://localhost:8080/healthz` — local health check.
- `docker compose up --build` — run with Postgres.

Test commands and layout are in [`docs/testing.md`](docs/testing.md); env vars in
[`docs/config.md`](docs/config.md).

## The gate (run before every commit)

Run all of these from the local venv and don't stop at the first failure:

```
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pyright
.venv/bin/pytest -q
.venv/bin/python scripts/gen_service_map.py --check
```

- ruff rules `E,F,I,UP,B,SIM`, line length 100; config in `pyproject.toml`.
  `debug_admin.py`, `jira_payload.py`, `postmortem.py`, `summary.py` ignore `E501`
  (long embedded CSS/HTML/JS and Russian PM templates).
- pyright is `basic` mode over `src/mm_jira_bot` + `tests`. There is **one** known
  pre-existing error (`tests/test_postmortem.py` — `ActionResult.status`,
  `reportAttributeAccessIssue`); ignore exactly that one, any other is new.
- The last step fails if `docs/reference/service-map.md` is stale. Regenerate with
  `.venv/bin/python scripts/gen_service_map.py && git add docs/reference/service-map.md`.

The `/gate` skill runs this inline; the `service-verifier` agent adds an adversarial
review for move-only refactors.

## Coding style & naming

Match nearby style: four-space indentation, type hints,
`from __future__ import annotations`, frozen dataclasses for value objects, small
modules with explicit responsibilities. `snake_case` for functions/variables/modules,
`PascalCase` for classes. Keep async boundaries clear for Mattermost, Jira, and
service methods. Formatting/linting via `ruff`, types via Pyright; run them before
committing and keep diffs focused.

When adding or changing a `service/` mixin, follow the typing convention in
[`docs/architecture.md`](docs/architecture.md) (state attrs as `__init__` declares
them, cross-domain calls as `if TYPE_CHECKING:` stubs, `_shared.py` stays the
import-graph leaf).

## Testing

pytest + pytest-asyncio (`asyncio_mode = "auto"` — no `@pytest.mark.asyncio`). Name
files `test_*.py`, functions `test_<behavior>`. The service suite is split by domain
to mirror `service/`; shared fakes/fixtures live in `tests/support.py` and
`tests/conftest.py`. Use the fakes + temp SQLite DB instead of live deps. Add or
extend tests for any behavior change — especially idempotency, retry/recovery, slash
commands, and Jira payload/option formatting. Full layout: [`docs/testing.md`](docs/testing.md).

## Commit & pull request guidelines

Concise, imperative commit subjects (e.g. `Add debug admin for alert ticket
operations`). Keep commits small and focused. PRs describe the behavior change, list
verification commands and results, note configuration/migration impact, and link
related issues. Include screenshots or request/response examples only when changing
user-visible Mattermost messages or HTTP behavior.

### Doc-sync (required)

Per [`CLAUDE.md`](CLAUDE.md), when a change alters behavior or architecture, bring the
docs in line **before** committing:

- domain/architecture change → the relevant [`docs/`](docs/) file;
- user-facing behavior or config → [`README.md`](README.md) / [`docs/config.md`](docs/config.md);
- every change → a `[Unreleased]` entry in [`CHANGELOG.md`](CHANGELOG.md).

The generated `docs/reference/service-map.md` is **not** hand-synced — the gate's
`--check` step enforces it.

## Security & configuration

All config comes from env vars (`config.py`, loaded from `.env`); see
[`docs/config.md`](docs/config.md) and `.env.example`. Copy `.env.example` to `.env`
for local dev and never commit real tokens; treat Jira/Mattermost tokens, channel
ids, and database URLs as secrets. Notable defaults: `ENABLE_BACKFILL_ON_STARTUP=false`
(process only new WS events, not history), `ENABLE_WEBSOCKET=true`. When changing
schema behavior, keep `migrations/`, the SQLAlchemy model, and startup init aligned
(see [`docs/persistence.md`](docs/persistence.md)).
