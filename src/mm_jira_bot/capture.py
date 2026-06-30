from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mm_jira_bot.logging import get_logger

if TYPE_CHECKING:
    import httpx

    from mm_jira_bot.config import Settings

log = get_logger("mm_jira_bot.capture")

# Inbound WS frames worth keeping: messages + reactions (and their edits/removals;
# system posts arrive as `posted` with a system_ type). Noise — typing, presence,
# channel_viewed, … — is skipped.
_WS_EVENTS = frozenset(
    {"posted", "reaction_added", "reaction_removed", "post_edited", "post_deleted"}
)

# Per-bucket hard cap so a long-running prod capture can never fill the disk.
_MAX_PER_BUCKET = 200

# Cap on the raw-text fallback for an unparsable response body.
_MAX_TEXT = 20_000

_cache: dict[str, Capture] = {}


def get_capture(settings: Settings) -> Capture | None:
    """Shared recorder bound to ``CAPTURE_DIR`` when ``CAPTURE_FIXTURES`` is on, else
    ``None`` so the hot paths add a single bool check and nothing else. Cached per
    export dir, so the WS loop and every REST call share one recorder."""
    if not settings.capture_fixtures:
        return None
    inst = _cache.get(settings.capture_dir)
    if inst is None:
        inst = Capture(settings.capture_dir)
        _cache[settings.capture_dir] = inst
        log.info("capture.enabled", export_dir=settings.capture_dir)
    return inst


class Capture:
    """Best-effort recorder of real Mattermost/Jira traffic into an export folder,
    bucketed by kind (``ws/<event>``, ``http/<client>``). Write-only side effect: it
    touches nothing but local files and never raises into the running bot."""

    def __init__(self, export_dir: str) -> None:
        self._dir = Path(export_dir)
        self._counts: dict[str, int] = {}

    def record_ws(self, event: dict) -> None:
        """Persist one raw inbound websocket frame, exactly as received."""
        kind = str(event.get("event") or "")
        if kind in _WS_EVENTS:
            self._write(f"ws/{kind}", event)

    def record_http(
        self,
        client: str,
        method: str,
        path: str,
        *,
        request_json: Any = None,
        params: dict[str, Any] | None = None,
        response: httpx.Response,
    ) -> None:
        """Persist one REST exchange — outgoing request + incoming response."""
        self._write(
            f"http/{client}",
            {
                "request": {
                    "method": method,
                    "path": path,
                    "params": params,
                    "json": request_json,
                },
                "response": {
                    "status": response.status_code,
                    "body": _response_body(response),
                },
            },
            hint=method,
        )

    def _write(self, bucket: str, payload: dict, *, hint: str = "") -> None:
        count = self._counts.get(bucket, 0)
        if count >= _MAX_PER_BUCKET:
            return
        try:
            folder = self._dir / bucket
            folder.mkdir(parents=True, exist_ok=True)
            seq = count + 1
            name = f"{seq:04d}-{hint}.json" if hint else f"{seq:04d}.json"
            (folder / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._counts[bucket] = seq
            if seq == _MAX_PER_BUCKET:
                log.info("capture.bucket_full", bucket=bucket, count=seq)
        except Exception:
            # Capturing fixtures must never disturb the running bot.
            log.warning("capture.write_failed", bucket=bucket, exc_info=True)


def _response_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        text = response.text or ""
        return text[:_MAX_TEXT] + "…[truncated]" if len(text) > _MAX_TEXT else text
