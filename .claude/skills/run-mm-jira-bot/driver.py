#!/usr/bin/env python3
"""Smoke driver for the mm_jira_bot FastAPI service.

Launches `python -m mm_jira_bot` with a self-contained config (dummy
Mattermost/Jira endpoints, a throwaway SQLite DB, no WebSocket, debug-admin
on), waits for /healthz, drives the public HTTP surface, prints PASS/FAIL per
check, then shuts the server down. Stdlib only — no curl, no extra deps.

Usage (from the repo root):
    python .claude/skills/run-mm-jira-bot/driver.py            # full smoke
    python .claude/skills/run-mm-jira-bot/driver.py --serve    # just run+wait, no checks

Port is fixed at 8080 because `python -m mm_jira_bot` hard-codes it; leave 8080
free or stop whatever is using it.

The point of this driver: the bot's two background loops (Mattermost WS,
pending-work) and its startup preflight are all non-fatal, so the HTTP server
comes up even though Mattermost/Jira here are bogus. That lets a future agent
exercise the endpoints offline.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Port is hard-coded in mm_jira_bot/__main__.py (uvicorn.run(... port=8080)),
# and the driver launches that module, so this must stay 8080.
REPO_ROOT = Path(__file__).resolve().parents[3]
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"


def smoke_env(db_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            # Dummy externals — never reached because WS is off and Jira create
            # is stubbed; only their presence is required by config.from_env().
            "MATTERMOST_URL": "https://mattermost.invalid",
            "MATTERMOST_TOKEN": "dummy-token",
            "MATTERMOST_ALERT_CHANNEL_ID": "alerts-channel",
            "MATTERMOST_INCIDENT_CHANNEL_ID": "incidents-channel",
            "MATTERMOST_BOT_USER_ID": "bot-user",
            "JIRA_BASE_URL": "https://jira.invalid",
            "JIRA_API_TOKEN": "dummy-token",
            "JIRA_PROJECT_KEY": "OPS",
            "JIRA_ISSUE_TYPE": "Incident",
            "JIRA_VALID_INCIDENT_FIELD": "Валидность",
            "JIRA_SOURCE_FIELD": "Источник",
            "JIRA_IS_CRIT_ALERT_FIELD": "Был ли крит алерт?",
            "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
            # Self-contained run: no live calls, no noisy reconnect loops.
            "JIRA_CREATE_ENABLED": "false",
            "ENABLE_WEBSOCKET": "false",
            "DEBUG_ADMIN_ENABLED": "true",
            "LOG_FORMAT": "text",
            "API_RETRY_ATTEMPTS": "1",
            # Neutralize anything a working-copy .env (loaded by config.from_env)
            # might set that would change these endpoints' behavior. Empty ==
            # unset for this config loader.
            "MATTERMOST_SLASH_TOKEN": "",
            "SERVICE_PUBLIC_URL": "",
            "LLM_API_TOKEN": "",
        }
    )
    return env


def wait_for_health(proc: subprocess.Popen, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False  # server died during startup
        try:
            with urllib.request.urlopen(f"{BASE}/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.4)
    return False


def http(method: str, path: str, *, body: object | None = None) -> tuple[int, str]:
    data = None
    headers = {}
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = str(body).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def run_checks() -> int:
    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"  [{mark}] {name}{(' | ' + detail) if detail else ''}")

    print("Driving HTTP surface:")

    status, text = http("GET", "/healthz")
    check("GET /healthz -> {ok:true}", status == 200 and json.loads(text).get("ok") is True, f"{status} {text}")

    status, text = http("GET", "/debug/admin")
    check("GET /debug/admin serves HTML", status == 200 and "<" in text, f"status {status}")

    status, text = http("GET", "/debug/admin/api/summary")
    ok = status == 200 and "total" in json.loads(text)
    check("GET /debug/admin/api/summary -> counters", ok, f"{status} {text[:120]}")

    status, text = http("GET", "/debug/admin/api/alerts")
    ok = status == 200 and isinstance(json.loads(text), (list, dict))
    check("GET /debug/admin/api/alerts -> list", ok, f"{status} {text[:120]}")

    # Invalid permalink path is handled entirely offline (no Mattermost call).
    status, text = http(
        "POST", "/mattermost/slash/incident", body="text=not-a-link&user_id=u1"
    )
    ok = status == 200 and "Invalid link" in json.loads(text).get("text", "")
    check("POST /mattermost/slash/incident (bad link) -> error text", ok, f"{status} {text[:120]}")

    # Unknown action returns an ephemeral message without external I/O.
    status, text = http(
        "POST",
        "/mattermost/actions/alert",
        body={"context": {"action": "nope", "alert_post_id": "p1"}, "user_id": "u1"},
    )
    ok = status == 200 and "ephemeral_text" in json.loads(text)
    check("POST /mattermost/actions/alert (unknown) -> ephemeral_text", ok, f"{status} {text[:120]}")

    return failures


def main() -> int:
    serve_only = "--serve" in sys.argv
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "smoke.db"
        env = smoke_env(db_path)
        print(f"Launching `python -m mm_jira_bot` on {BASE} (cwd={REPO_ROOT})")
        proc = subprocess.Popen(
            [sys.executable, "-m", "mm_jira_bot"],
            cwd=str(REPO_ROOT),
            env=env,
        )
        try:
            if not wait_for_health(proc, timeout=40):
                print("FAILED: server did not become healthy (see logs above)")
                return 1
            print(f"Server healthy at {BASE}\n")
            if serve_only:
                print("--serve: leaving server up; press Ctrl-C to stop.")
                proc.wait()
                return 0
            failures = run_checks()
            print()
            if failures:
                print(f"RESULT: {failures} check(s) FAILED")
                return 1
            print("RESULT: all checks PASSED")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
