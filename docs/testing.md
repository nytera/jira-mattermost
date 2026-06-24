# Testing

pytest + pytest-asyncio, configured in `pyproject.toml`: `asyncio_mode = "auto"`
(no `@pytest.mark.asyncio` needed), `pythonpath = ["src"]`, `testpaths = ["tests"]`.

## Run

```bash
.venv/bin/pytest -q                                          # full suite
.venv/bin/pytest tests/test_alerts.py::test_<name>           # a single test
.venv/bin/pytest --cov=mm_jira_bot --cov-report=term-missing # with coverage
```

Current baseline: **202 tests, ~81% line coverage** (verified via the `--cov`
command above; treat the number as approximate and re-measure rather than trusting a
stale figure). The full pre-commit gate also runs ruff, ruff format `--check`,
pyright and the service-map `--check` — see [`../CLAUDE.md`](../CLAUDE.md).

## Layout (per-domain, mirrors `service/`)

The service suite is split by domain to match the mixins (see
[`reference/service-map.md`](reference/service-map.md) for the mixin map):

| Test file | Covers |
|---|---|
| `tests/test_alerts.py` | `AlertMixin` (`_alerts.py`) |
| `tests/test_incidents.py` | `IncidentMixin` (`_incidents.py`) |
| `tests/test_jira_sync.py` | `JiraSyncMixin` (`_jira_sync.py`) |
| `tests/test_postmortem.py` | `PostmortemMixin` + postmortem helpers |
| `tests/test_thread_summary.py` | `ThreadSummaryMixin` (`_thread_summary.py`) |
| `tests/test_debug.py` | `DebugMixin` (`_debug.py`) |
| `tests/test_service_infra.py` | cross-cutting: config, DB, auth allowlist, app/lifespan, coordinator routing, ops/metrics |
| `tests/test_logging.py` | `logging.py` formatters / ring buffer |

> Drift note: older README copy described a single pre-split test set. The current
> layout is per-domain as above.

## Harness

- **`tests/conftest.py`** — the `settings` and `service` fixtures (auto-injected). The
  `settings` fixture uses a `tmp_path` SQLite DB; `service` wires the fakes.
- **`tests/support.py`** — in-memory `FakeMattermostClient` / `FakeJiraClient` /
  `FakeLlmClient`, data builders (`make_alert`, `_manual_post`), service builders
  (`_build_service`, `_incident_service`), and cross-domain flow/assertion helpers.
  Imported as `from support import …`.

Use the fakes + temp SQLite DB to avoid live Mattermost, Jira, or Postgres
dependencies.

## Expectations

Add or extend tests for any behavior change — especially idempotency, retry/recovery,
slash commands, and Jira payload/option formatting. Name files `test_*.py` and
functions `test_<behavior>`. Conventions are in [`../CLAUDE.md`](../CLAUDE.md).
