from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REDACTED = "[redacted]"
SENSITIVE_KEYS = frozenset({"token", "email", "text", "draft", "answer", "delta"})


def _redact_processor(
    _logger: object,
    _method: str,
    event_dict: dict[str, object],
) -> dict[str, object]:
    for key in list(event_dict):
        if key in SENSITIVE_KEYS:
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(level: str = "INFO", verbose_payloads: bool = False) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(message)s", handlers=[logging.StreamHandler()])

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if not verbose_payloads:
        processors.append(_redact_processor)
    processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        context_class=dict,
        cache_logger_on_first_use=True,
    )

    structlog.contextvars.bind_contextvars(service="duke")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a per-request id to structlog contextvars."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = request_id
            return response
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
            del token
