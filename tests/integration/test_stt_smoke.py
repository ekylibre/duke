"""Opt-in real-Whisper transcription smoke test.

Pulls and runs the actual `faster-whisper` model. Disabled by default because
it downloads ~150-500 MB on first run. Enable with `RUN_STT_SMOKE=1`.

Generates a short PCM tone (no real speech) — we only assert the pipeline
roundtrips bytes → string without raising. A meaningful French-quality test
needs a recorded sample which we keep out of git for licensing reasons.
"""

from __future__ import annotations

import io
import math
import os
import struct
import wave

import pytest

from duke.stt import WhisperService

RUN = os.environ.get("RUN_STT_SMOKE") == "1"
pytestmark = [
    pytest.mark.skipif(not RUN, reason="set RUN_STT_SMOKE=1 to run"),
    pytest.mark.stt_smoke,
]


def _silence_wav(seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    """Generate a tiny mono PCM WAV with a low sine — enough for VAD to skip."""
    n_samples = int(seconds * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for i in range(n_samples):
            sample = int(2000 * math.sin(2 * math.pi * 220 * i / sample_rate))
            wav.writeframes(struct.pack("<h", sample))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_real_whisper_roundtrips_bytes_to_string() -> None:
    svc = WhisperService(
        model_name=os.environ.get("WHISPER_SMOKE_MODEL", "tiny"),
        device="cpu",
        compute_type="int8",
        language="fr",
    )

    text = await svc.transcribe(_silence_wav())
    assert isinstance(text, str)
