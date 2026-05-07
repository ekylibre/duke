from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, generate_latest

ws_sessions_active = Gauge(
    "duke_ws_sessions_active",
    "Number of active WebSocket sessions.",
)

user_messages_total = Counter(
    "duke_user_messages_total",
    "User messages received, by outcome.",
    labelnames=("outcome",),
)

errors_total = Counter(
    "duke_errors_total",
    "Errors emitted to clients, by code.",
    labelnames=("code",),
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
