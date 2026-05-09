from __future__ import annotations

import structlog
from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile

from duke.config import Settings
from duke.integration.ekylibre.api_client import (
    EkylibreApiClient,
    EkylibreAuthError,
    EkylibreCredentials,
    EkylibreTenantError,
    EkylibreUnavailableError,
)
from duke.observability.metrics import stt_requests_total
from duke.stt import STTBackendError, STTUnavailableError, WhisperService

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/stt", tags=["stt"])


def _parse_simple_token(authorization: str | None) -> tuple[str, str]:
    if not authorization:
        raise HTTPException(status_code=401, detail="missing Authorization header")
    parts = authorization.split()
    if len(parts) != 3 or parts[0].lower() != "simple-token":
        raise HTTPException(
            status_code=401,
            detail="expected `simple-token <email> <token>` Authorization",
        )
    return parts[1], parts[2]


@router.post("/transcribe")
async def transcribe(
    request: Request,
    audio: UploadFile = File(...),
    authorization: str | None = Header(default=None),
    x_tenant: str | None = Header(default=None, alias="X-Tenant"),
) -> dict[str, str]:
    """Transcribe an audio blob to French text via Whisper.

    Auth mirrors the WS path: `Authorization: simple-token <email> <token>`
    + `X-Tenant: <tenant>`. Validated against `GET /api/v2/users/me` so a
    revoked token can't reach the model. The transcript is returned to the
    widget which then sends it through the existing `user_message` WS flow —
    Duke's NLU pipeline never branches on STT origin.
    """
    settings: Settings = request.app.state.settings

    if not settings.enable_server_stt:
        stt_requests_total.labels(outcome="disabled").inc()
        raise HTTPException(
            status_code=503,
            detail="server STT disabled (set ENABLE_SERVER_STT=true)",
        )

    if not x_tenant:
        raise HTTPException(status_code=401, detail="missing X-Tenant header")
    email, token = _parse_simple_token(authorization)

    creds = EkylibreCredentials(
        email=email,
        token=token,
        tenant=x_tenant,
        base_url=settings.ekylibre_api_base_url,
    )
    api = EkylibreApiClient(creds, request.app.state.http_client)

    try:
        await api.validate_token()
    except EkylibreAuthError as exc:
        stt_requests_total.labels(outcome="auth_invalid").inc()
        raise HTTPException(status_code=401, detail="invalid token") from exc
    except EkylibreTenantError as exc:
        stt_requests_total.labels(outcome="auth_tenant").inc()
        raise HTTPException(status_code=401, detail="unknown tenant") from exc
    except EkylibreUnavailableError as exc:
        stt_requests_total.labels(outcome="upstream_unavailable").inc()
        raise HTTPException(status_code=502, detail="Ekylibre API unavailable") from exc

    payload = await audio.read()
    size = len(payload)
    if size == 0:
        stt_requests_total.labels(outcome="empty").inc()
        raise HTTPException(status_code=400, detail="empty audio file")
    if size > settings.stt_max_audio_bytes:
        stt_requests_total.labels(outcome="too_large").inc()
        raise HTTPException(
            status_code=413,
            detail=f"audio exceeds {settings.stt_max_audio_bytes} bytes",
        )

    service: WhisperService = request.app.state.whisper_service

    try:
        text = await service.transcribe(payload)
    except STTUnavailableError as exc:
        stt_requests_total.labels(outcome="backend_unavailable").inc()
        log.error("stt.backend_unavailable", error=str(exc))
        raise HTTPException(status_code=503, detail="STT backend unavailable") from exc
    except STTBackendError as exc:
        stt_requests_total.labels(outcome="backend_error").inc()
        log.warning("stt.backend_error", error=str(exc))
        raise HTTPException(
            status_code=422, detail=f"transcription failed: {exc}"
        ) from exc

    stt_requests_total.labels(outcome="ok").inc()
    log.info(
        "stt.transcribed",
        tenant=x_tenant,
        bytes=size,
        chars=len(text),
        content_type=audio.content_type,
    )
    return {"text": text}
