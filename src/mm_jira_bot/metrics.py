"""Prometheus metrics: definitions, HTTP instrumentation, ticket gauges.

Single-process uvicorn, so the default global ``REGISTRY`` is fine (no
multiprocess mode). Counters/histograms are defined once at import; the
``/metrics`` endpoint (``web.py``) renders them on scrape. Ticket gauges are
collected lazily on each scrape via :class:`TicketStatsCollector`.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from typing import TYPE_CHECKING

from prometheus_client import REGISTRY, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

if TYPE_CHECKING:
    from mm_jira_bot.repository import AlertTicketRepository

http_requests_total = Counter(
    "bot_http_requests_total",
    "External HTTP requests made by the bot, by client and outcome.",
    ["client", "method", "status"],
)
http_request_duration_seconds = Histogram(
    "bot_http_request_duration_seconds",
    "External HTTP request duration in seconds, by client and method.",
    ["client", "method"],
)
errors_total = Counter(
    "bot_errors_total",
    "Error-level log events emitted by the bot, by event name.",
    ["event"],
)
ops_alerts_dropped_total = Counter(
    "bot_ops_alerts_dropped_total",
    "Ops-channel alerts dropped because the delivery queue was full.",
)


def observe_http(client: str, method: str, status: str, duration_seconds: float) -> None:
    """Record one external HTTP attempt (called per try from ``AsyncApiClient``)."""
    http_requests_total.labels(client=client, method=method, status=status).inc()
    http_request_duration_seconds.labels(client=client, method=method).observe(duration_seconds)


class TicketStatsCollector(Collector):
    """Expose alert-ticket counts as gauges, sampled at scrape time."""

    def __init__(self, repository: AlertTicketRepository) -> None:
        self._repository = repository

    def collect(self) -> Iterable[GaugeMetricFamily]:
        try:
            summary = self._repository.debug_summary()
        except Exception:
            # A DB hiccup must not blank the entire /metrics output.
            return
        for name, key, doc in (
            ("bot_tickets_total", "total", "Total alert tickets."),
            ("bot_tickets_pending_jira", "pending_jira", "Tickets without a Jira issue yet."),
            ("bot_tickets_failed", "failed", "Tickets in a failed creation/confirmation state."),
            ("bot_tickets_confirmed", "confirmed", "Tickets confirmed as valid incidents."),
        ):
            yield GaugeMetricFamily(name, doc, value=float(summary.get(key, 0) or 0))
        creation = GaugeMetricFamily(
            "bot_tickets_by_creation_status",
            "Alert tickets grouped by creation_status.",
            labels=["status"],
        )
        for status, count in (summary.get("creation_statuses") or {}).items():
            creation.add_metric([str(status)], float(count or 0))
        yield creation
        confirmation = GaugeMetricFamily(
            "bot_tickets_by_confirmation_status",
            "Alert tickets grouped by confirmation_status.",
            labels=["status"],
        )
        for status, count in (summary.get("confirmation_statuses") or {}).items():
            confirmation.add_metric([str(status)], float(count or 0))
        yield confirmation


_ticket_collector: TicketStatsCollector | None = None


def register_ticket_collector(repository: AlertTicketRepository) -> None:
    """Register the ticket-stats collector, replacing any previous one.

    Idempotent so repeated ``create_app`` calls (tests) don't duplicate series.
    """
    global _ticket_collector
    if _ticket_collector is not None:
        with suppress(KeyError):
            REGISTRY.unregister(_ticket_collector)
    _ticket_collector = TicketStatsCollector(repository)
    REGISTRY.register(_ticket_collector)
