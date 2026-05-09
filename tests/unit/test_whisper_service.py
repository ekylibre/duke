"""Unit tests for `WhisperService`.

The real backend is faster-whisper (heavy: ~500 MB model + ctranslate2). All
tests here inject a stub backend via `backend_factory` so they run in
milliseconds without pulling weights. The opt-in `tests/integration/test_stt_smoke.py`
covers the real backend.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from duke.stt import STTBackendError, STTUnavailableError, WhisperService


class _StubBackend:
    def __init__(self, response: str = "ce matin j'ai pulvérisé") -> None:
        self.response = response
        self.calls: list[tuple[bytes, str]] = []

    def transcribe(self, audio: bytes, *, language: str) -> str:
        self.calls.append((audio, language))
        return self.response


@pytest.mark.asyncio
async def test_transcribe_returns_text() -> None:
    stub = _StubBackend(response="bonjour Duke")
    svc = WhisperService(
        model_name="stub",
        device="cpu",
        compute_type="int8",
        language="fr",
        backend_factory=lambda: stub,
    )

    text = await svc.transcribe(b"\x00\x01\x02")

    assert text == "bonjour Duke"
    assert stub.calls == [(b"\x00\x01\x02", "fr")]


@pytest.mark.asyncio
async def test_empty_audio_raises_backend_error() -> None:
    svc = WhisperService(
        model_name="stub",
        device="cpu",
        compute_type="int8",
        language="fr",
        backend_factory=lambda: _StubBackend(),
    )

    with pytest.raises(STTBackendError, match="empty"):
        await svc.transcribe(b"")


@pytest.mark.asyncio
async def test_backend_exception_wrapped() -> None:
    class _BoomBackend:
        def transcribe(self, audio: bytes, *, language: str) -> str:
            raise RuntimeError("ffmpeg decode failed")

    svc = WhisperService(
        model_name="stub",
        device="cpu",
        compute_type="int8",
        language="fr",
        backend_factory=lambda: _BoomBackend(),
    )

    with pytest.raises(STTBackendError, match="ffmpeg"):
        await svc.transcribe(b"\x00")


@pytest.mark.asyncio
async def test_factory_unavailable_surfaces_unavailable_error() -> None:
    def _factory() -> _StubBackend:
        raise STTUnavailableError("faster-whisper missing")

    svc = WhisperService(
        model_name="stub",
        device="cpu",
        compute_type="int8",
        language="fr",
        backend_factory=_factory,
    )

    with pytest.raises(STTUnavailableError):
        await svc.transcribe(b"\x00")


@pytest.mark.asyncio
async def test_factory_arbitrary_exception_wraps_to_unavailable() -> None:
    def _factory() -> _StubBackend:
        raise OSError("disk full")

    svc = WhisperService(
        model_name="stub",
        device="cpu",
        compute_type="int8",
        language="fr",
        backend_factory=_factory,
    )

    with pytest.raises(STTUnavailableError, match="disk full"):
        await svc.transcribe(b"\x00")


@pytest.mark.asyncio
async def test_concurrent_first_calls_load_backend_once() -> None:
    """Two concurrent transcribes on a cold service must instantiate the
    backend exactly once — proves the lock around lazy load."""
    factory_calls = 0
    lock = threading.Lock()

    def _factory() -> _StubBackend:
        nonlocal factory_calls
        with lock:
            factory_calls += 1
        return _StubBackend(response="ok")

    svc = WhisperService(
        model_name="stub",
        device="cpu",
        compute_type="int8",
        language="fr",
        backend_factory=_factory,
    )

    results = await asyncio.gather(
        svc.transcribe(b"\x00"),
        svc.transcribe(b"\x01"),
        svc.transcribe(b"\x02"),
    )

    assert results == ["ok", "ok", "ok"]
    assert factory_calls == 1


@pytest.mark.asyncio
async def test_strips_trailing_whitespace_in_transcript() -> None:
    stub = _StubBackend(response="  hello world  ")
    svc = WhisperService(
        model_name="stub",
        device="cpu",
        compute_type="int8",
        language="fr",
        backend_factory=lambda: stub,
    )

    text = await svc.transcribe(b"\x00")

    assert text == "hello world"
