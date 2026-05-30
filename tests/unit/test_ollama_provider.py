"""Unit tests for OllamaProvider (httpx mocked, no real Ollama server)."""

from __future__ import annotations

import json

import httpx
import pytest

from duke.nlu.llm.base import LLMSchemaError, LLMUnavailableError
from duke.nlu.llm.ollama import OllamaProvider


def _provider(handler) -> OllamaProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://ollama:11434")
    return OllamaProvider(client, model="mistral-nemo")


@pytest.mark.asyncio
async def test_extract_intervention_parses_structured_json():
    payload = {
        "procedure_name": "generic_tillage",
        "targets": [{"raw_name": "Bernessard"}],
        "inputs": [],
        "ambiguities": [],
        "confidence": 0.8,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        assert body["stream"] is False
        assert body["format"]["type"] == "object"  # structured output schema
        return httpx.Response(200, json={"message": {"content": json.dumps(payload)}})

    result = await _provider(handler).extract_intervention("j'ai labouré", {})
    assert result["procedure_name"] == "generic_tillage"


@pytest.mark.asyncio
async def test_extract_intervention_invalid_json_raises_schema_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "not-json {"}})

    with pytest.raises(LLMSchemaError):
        await _provider(handler).extract_intervention("x", {})


@pytest.mark.asyncio
async def test_extract_intervention_http_error_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(LLMUnavailableError):
        await _provider(handler).extract_intervention("x", {})


@pytest.mark.asyncio
async def test_answer_query_streams_ndjson_deltas():
    lines = [
        json.dumps({"message": {"content": "Il te reste "}, "done": False}),
        json.dumps({"message": {"content": "18 L."}, "done": True}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="\n".join(lines))

    tokens = [t async for t in _provider(handler).answer_query("combien ?", {})]
    assert "".join(tokens) == "Il te reste 18 L."


@pytest.mark.asyncio
async def test_health_true_on_200_tags():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": []})

    assert await _provider(handler).health() is True
