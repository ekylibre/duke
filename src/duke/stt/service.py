from __future__ import annotations

import asyncio
from collections.abc import Callable
from io import BytesIO
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)


class STTUnavailableError(RuntimeError):
    """Backend can't be loaded (deps missing, model download failed)."""


class STTBackendError(RuntimeError):
    """Transcription failed at runtime (unsupported audio, decode error)."""


class WhisperBackend(Protocol):
    """Sync transcription backend. Implemented by faster-whisper or a test fake."""

    def transcribe(self, audio: bytes, *, language: str) -> str: ...


class WhisperService:
    """Async wrapper around a sync Whisper backend.

    The model is heavy (~500 MB) and ctranslate2's `transcribe` is blocking,
    so we lazy-load it on first call and run transcription in a worker thread.
    The lazy load is gated by an `asyncio.Lock` so concurrent first requests
    don't double-instantiate the model. Tests inject a `backend_factory`
    returning a fake backend to avoid pulling weights.
    """

    def __init__(
        self,
        *,
        model_name: str,
        device: str,
        compute_type: str,
        language: str,
        cache_dir: str | None = None,
        backend_factory: Callable[[], WhisperBackend] | None = None,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._cache_dir = cache_dir
        self._backend_factory = backend_factory or self._default_backend_factory
        self._backend: WhisperBackend | None = None
        self._lock = asyncio.Lock()

    def _default_backend_factory(self) -> WhisperBackend:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise STTUnavailableError(
                "faster-whisper is not installed — run `uv sync --extra stt`"
            ) from exc

        model = WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=self._compute_type,
            download_root=self._cache_dir,
        )
        return _FasterWhisperBackend(model)

    async def transcribe(self, audio: bytes) -> str:
        if not audio:
            raise STTBackendError("empty audio payload")
        backend = await self._ensure_backend()
        try:
            text = await asyncio.to_thread(
                backend.transcribe, audio, language=self._language
            )
        except (STTUnavailableError, STTBackendError):
            raise
        except Exception as exc:
            log.exception("stt.transcribe_failed", error=str(exc))
            raise STTBackendError(str(exc)) from exc
        return text.strip()

    async def _ensure_backend(self) -> WhisperBackend:
        if self._backend is not None:
            return self._backend
        async with self._lock:
            if self._backend is None:
                log.info(
                    "stt.loading_model",
                    model=self._model_name,
                    device=self._device,
                    compute_type=self._compute_type,
                )
                try:
                    self._backend = await asyncio.to_thread(self._backend_factory)
                except STTUnavailableError:
                    raise
                except Exception as exc:
                    log.exception("stt.model_load_failed", error=str(exc))
                    raise STTUnavailableError(str(exc)) from exc
                log.info("stt.model_loaded", model=self._model_name)
        return self._backend


class _FasterWhisperBackend:
    """Adapter around `faster_whisper.WhisperModel`. Pure sync."""

    def __init__(self, model: Any) -> None:
        self._model = model

    def transcribe(self, audio: bytes, *, language: str) -> str:
        segments, _info = self._model.transcribe(
            BytesIO(audio),
            language=language,
            vad_filter=True,
            beam_size=5,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
