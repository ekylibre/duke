"""Opt-in smoke test against a real Ollama server.

Skipped unless RUN_OLLAMA_SMOKE=1. Requires an Ollama server reachable at
OLLAMA_BASE_URL (default http://localhost:11434) with OLLAMA_MODEL pulled
(default mistral-nemo). Bring it up with:

    docker compose -f docker/docker-compose.yml --profile local-llm up -d
"""

from __future__ import annotations

import os

import pytest

from duke.nlu.llm.ollama import OllamaProvider

pytestmark = [
    pytest.mark.ollama_smoke,
    pytest.mark.skipif(
        os.environ.get("RUN_OLLAMA_SMOKE") != "1",
        reason="set RUN_OLLAMA_SMOKE=1 (needs a running Ollama with the model pulled)",
    ),
]


@pytest.mark.asyncio
async def test_extract_intervention_against_real_ollama():
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "mistral-nemo")
    provider = OllamaProvider.from_config(base_url, model=model)

    assert await provider.health() is True

    raw = await provider.extract_intervention(
        "J'ai labouré la parcelle Bernessard hier avec le 533",
        {
            "intent": "record_intervention",
            "candidate_procedures": [{"raw_name": "labouré", "resolved_name": "Labour"}],
            "candidate_parcels": [{"raw_name": "Bernessard"}],
            "candidate_tools": [{"raw_name": "533"}],
        },
    )
    # Structured output must satisfy the schema's required keys.
    assert "procedure_name" in raw
    assert "targets" in raw
