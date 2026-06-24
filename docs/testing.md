# Testing

pytest + pytest-asyncio, configured in `pyproject.toml`: `asyncio_mode = "auto"`
(no `@pytest.mark.asyncio` needed), `pythonpath = ["src"]`, `testpaths = ["tests"]`.

## Run

```bash
.venv/bin/pytest -q                                          # full suite
.venv/bin/pytest tests/test_alerts.py::test_<name>           # a single test
.venv/bin/pytest --cov=mm_jira_bot --cov-report=term-missing # with coverage
```

Coverage is measured via the `--cov` command above; re-measure rather than trusting a
stale figure. The full pre-commit gate (ruff, format, pyright, service-map `--check`)
is in [`../CLAUDE.md`](../CLAUDE.md).

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
| `tests/test_service_infra.py` | cross-cutting: config validation, DB, auth allowlist, slash-token auth, app/lifespan, `_redact_database_url`, coordinator routing, ops/metrics |
| `tests/test_logging.py` | `logging.py` formatters / ring buffer |

Reliability / contract seams (not tied to one mixin):

| Test file | Covers |
|---|---|
| `tests/test_retry.py` | `retry.py` — `is_retryable_status` boundaries, `retry_async` backoff/exhaustion/short-circuit |
| `tests/test_http.py` | `http.py` — `_raise_for_status`, transport-error wrapping, and the real client over `httpx.MockTransport` (503→200 recover, persistent-503 exhaust) |
| `tests/test_repository.py` | `repository.py` — idempotency, `uq_active_root` partial index, timezone round-trip, legacy `init_db` backfill |
| `tests/test_migrations.py` | `migrations/*.sql` vs `Base.metadata` schema round-trip |
| `tests/test_websocket_loop.py` | `web.py`/`mattermost.py` — websocket reconnect, handler isolation, pending-work loop, event parsers |
| `tests/test_parsers_properties.py` | property-based (Hypothesis) over `markdown_to_jira_wiki`, `alert_signature`, `is_resolved_alert`, post-id parsing |

## Harness

- **`tests/conftest.py`** — the `settings` and `service` fixtures (auto-injected). The
  `settings` fixture uses a `tmp_path` SQLite DB; `service` wires the fakes.
- **`tests/support.py`** — in-memory `FakeMattermostClient` / `FakeJiraClient` /
  `FakeLlmClient`, data builders (`make_alert`, `_manual_post`), service builders
  (`_build_service`, `_incident_service`), and cross-domain flow/assertion helpers.
  Imported as `from support import …`.

Use the fakes + temp SQLite DB to avoid live Mattermost, Jira, or Postgres
dependencies. For the wire-level seam the fakes bypass, drive the real
client over `httpx.MockTransport` (see `tests/test_http.py`); for parser/formatter
invariants use property-based tests with **Hypothesis** (`tests/test_parsers_properties.py`,
in the `[test]` extras).

## Expectations

Add or extend tests for any behavior change — especially idempotency, retry/recovery,
slash commands, and Jira payload/option formatting. Name files `test_*.py` and
functions `test_<behavior>`. Conventions are in [`../CLAUDE.md`](../CLAUDE.md).
