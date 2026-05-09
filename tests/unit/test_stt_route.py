"""Unit tests for the `POST /api/v1/stt/transcribe` route.

The Whisper backend is stubbed via `app.state.whisper_service` so these tests
don't load any model. Ekylibre auth is mocked via `respx`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from fastapi import FastAPI

from duke.config import Settings
from duke.main import create_app
from duke.stt import STTBackendError, STTUnavailableError, WhisperService

EKYLIBRE_BASE = "http://ekylibre.test"


@asynccontextmanager
async def _noop_lifespan(_app: FastAPI):
    yield


@pytest.fixture
def app_without_lifespan() -> FastAPI:
    app = create_app()
    app.router.lifespan_context = _noop_lifespan  # type: ignore[assignment]
    return app


class _StubBackend:
    def __init__(self, response: str = "voilà le texte") -> None:
        self.response = response

    def transcribe(self, audio: bytes, *, language: str) -> str:
        return self.response


def _wire_app(app, *, enable_stt: bool = True, backend=None, http_client=None):
    """Plug minimal app.state needed by the STT route into a lifespan-less app."""
    settings = Settings(enable_server_stt=enable_stt, stt_max_audio_bytes=1024)  # type: ignore[call-arg]
    app.state.settings = settings
    app.state.http_client = http_client or httpx.AsyncClient(base_url=EKYLIBRE_BASE)
    app.state.whisper_service = WhisperService(
        model_name="stub",
        device="cpu",
        compute_type="int8",
        language="fr",
        backend_factory=lambda: backend or _StubBackend(),
    )
    return app


async def _post(app, *, headers=None, files=None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/v1/stt/transcribe",
            headers=headers or {},
            files=files,
        )


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": "simple-token user@farm.example tok123",
        "X-Tenant": "closeriedesterres",
    }


def _audio_files(payload: bytes = b"\x00\x01\x02\x03") -> dict:
    return {"audio": ("clip.webm", payload, "audio/webm")}


def _mock_users_me_ok(router):
    router.get(f"{EKYLIBRE_BASE}/api/v2/users/me").respond(
        200,
        json={
            "id": 7,
            "email": "user@farm.example",
            "full_name": "Jean Vigneron",
            "locale": "fr",
        },
    )


@pytest.mark.asyncio
async def test_returns_503_when_disabled(app_without_lifespan) -> None:
    _wire_app(app_without_lifespan, enable_stt=False)

    resp = await _post(
        app_without_lifespan,
        headers=_auth_headers(),
        files=_audio_files(),
    )

    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_returns_401_when_authorization_missing(app_without_lifespan) -> None:
    _wire_app(app_without_lifespan)

    resp = await _post(
        app_without_lifespan,
        headers={"X-Tenant": "closeriedesterres"},
        files=_audio_files(),
    )

    assert resp.status_code == 401
    assert "Authorization" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_returns_401_when_authorization_malformed(app_without_lifespan) -> None:
    _wire_app(app_without_lifespan)

    resp = await _post(
        app_without_lifespan,
        headers={
            "Authorization": "Bearer abc",
            "X-Tenant": "closeriedesterres",
        },
        files=_audio_files(),
    )

    assert resp.status_code == 401
    assert "simple-token" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_returns_401_when_tenant_missing(app_without_lifespan) -> None:
    _wire_app(app_without_lifespan)

    resp = await _post(
        app_without_lifespan,
        headers={"Authorization": "simple-token user@farm.example tok"},
        files=_audio_files(),
    )

    assert resp.status_code == 401
    assert "X-Tenant" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_returns_401_when_token_invalid(app_without_lifespan) -> None:
    _wire_app(app_without_lifespan)

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/users/me").respond(
            401, json={"error": "invalid"}
        )
        resp = await _post(
            app_without_lifespan,
            headers=_auth_headers(),
            files=_audio_files(),
        )

    assert resp.status_code == 401
    assert "invalid token" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_returns_502_when_ekylibre_unavailable(app_without_lifespan) -> None:
    _wire_app(app_without_lifespan)

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/users/me").respond(503)
        resp = await _post(
            app_without_lifespan,
            headers=_auth_headers(),
            files=_audio_files(),
        )

    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_returns_400_on_empty_audio(app_without_lifespan) -> None:
    _wire_app(app_without_lifespan)

    with respx.mock(assert_all_called=False) as router:
        _mock_users_me_ok(router)
        resp = await _post(
            app_without_lifespan,
            headers=_auth_headers(),
            files=_audio_files(payload=b""),
        )

    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_returns_413_on_too_large(app_without_lifespan) -> None:
    _wire_app(app_without_lifespan)
    huge = b"\x00" * 2048  # > 1024 bytes (stt_max_audio_bytes wired in _wire_app)

    with respx.mock(assert_all_called=False) as router:
        _mock_users_me_ok(router)
        resp = await _post(
            app_without_lifespan,
            headers=_auth_headers(),
            files=_audio_files(payload=huge),
        )

    assert resp.status_code == 413
    assert "exceeds" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_happy_path_returns_transcript(app_without_lifespan) -> None:
    backend = _StubBackend(response="ce matin j'ai pulvérisé")
    _wire_app(app_without_lifespan, backend=backend)

    with respx.mock(assert_all_called=False) as router:
        _mock_users_me_ok(router)
        resp = await _post(
            app_without_lifespan,
            headers=_auth_headers(),
            files=_audio_files(),
        )

    assert resp.status_code == 200
    assert resp.json() == {"text": "ce matin j'ai pulvérisé"}


@pytest.mark.asyncio
async def test_returns_422_when_backend_errors(app_without_lifespan) -> None:
    class _BoomBackend:
        def transcribe(self, audio: bytes, *, language: str) -> str:
            raise STTBackendError("invalid audio")

    _wire_app(app_without_lifespan, backend=_BoomBackend())

    with respx.mock(assert_all_called=False) as router:
        _mock_users_me_ok(router)
        resp = await _post(
            app_without_lifespan,
            headers=_auth_headers(),
            files=_audio_files(),
        )

    assert resp.status_code == 422
    assert "transcription failed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_returns_503_when_backend_unavailable(app_without_lifespan) -> None:
    """If faster-whisper isn't installed, we surface 503 (not 500)."""
    settings = Settings(enable_server_stt=True, stt_max_audio_bytes=1024)  # type: ignore[call-arg]
    app_without_lifespan.state.settings = settings
    app_without_lifespan.state.http_client = httpx.AsyncClient(base_url=EKYLIBRE_BASE)

    def _factory():
        raise STTUnavailableError("faster-whisper missing")

    app_without_lifespan.state.whisper_service = WhisperService(
        model_name="stub",
        device="cpu",
        compute_type="int8",
        language="fr",
        backend_factory=_factory,
    )

    with respx.mock(assert_all_called=False) as router:
        _mock_users_me_ok(router)
        resp = await _post(
            app_without_lifespan,
            headers=_auth_headers(),
            files=_audio_files(),
        )

    assert resp.status_code == 503
    assert "STT backend unavailable" in resp.json()["detail"]
