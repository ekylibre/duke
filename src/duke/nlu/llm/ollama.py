"""Local LLM provider backed by an Ollama server (`/api/chat`).

Mirrors `ClaudeProvider`/`MistralProvider` against the same prompts and schema
so the three providers are interchangeable behind `LLMRouter`. Two Ollama
features carry the load:

- **extraction** uses Ollama *structured outputs* (`format=<JSON schema>`),
  reusing `EXTRACT_INTERVENTION_SCHEMA`. The model is constrained to emit JSON
  matching the schema, returned as a string in `message.content` we parse.
- **Q&A** streams real deltas via `/api/chat` `stream:true` (NDJSON), unlike
  Mistral's single-shot yield.

No new dependency: we talk to Ollama over `httpx` (already a base dep). The
model must be pulled server-side (`ollama pull <model>`); see the docker
compose `local-llm` profile.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

from duke.nlu.llm.base import LLMSchemaError, LLMUnavailableError
from duke.nlu.llm.prompts import (
    ANSWER_QUERY_SYSTEM,
    EXTRACT_INTERVENTION_SYSTEM,
    build_answer_user_prompt,
    build_extraction_user_prompt,
)
from duke.nlu.llm.tools import EXTRACT_INTERVENTION_SCHEMA

log = structlog.get_logger(__name__)

# Local generation on a ~12B CPU model is slow; be generous so a single
# extraction doesn't trip the client timeout. Tune per hardware.
_DEFAULT_TIMEOUT_S = 120.0


class OllamaProvider:
    name = "ollama"

    def __init__(self, client: httpx.AsyncClient, model: str, max_tokens: int = 1024) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    @classmethod
    def from_config(
        cls, base_url: str, model: str, max_tokens: int = 1024
    ) -> OllamaProvider:
        client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=_DEFAULT_TIMEOUT_S
        )
        return cls(client, model=model, max_tokens=max_tokens)

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/api/tags", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def extract_intervention(self, text: str, hints: dict[str, Any]) -> dict[str, Any]:
        body = {
            "model": self._model,
            "stream": False,
            # Structured outputs: constrain generation to the intervention schema.
            "format": EXTRACT_INTERVENTION_SCHEMA,
            "options": {"num_predict": self._max_tokens, "temperature": 0},
            "messages": [
                {"role": "system", "content": EXTRACT_INTERVENTION_SYSTEM},
                {"role": "user", "content": build_extraction_user_prompt(text, hints)},
            ],
        }
        try:
            resp = await self._client.post("/api/chat", json=body)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"ollama http error: {exc}") from exc

        content = (data.get("message") or {}).get("content")
        if not content:
            raise LLMSchemaError("Ollama returned no message content")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMSchemaError(f"Ollama returned non-JSON content: {exc}") from exc
        if not isinstance(payload, dict):
            raise LLMSchemaError("Ollama content is not a JSON object")
        return payload

    async def answer_query(
        self,
        question: str,
        evidence: dict[str, Any],
    ) -> AsyncIterator[str]:
        body = {
            "model": self._model,
            "stream": True,
            "options": {"num_predict": self._max_tokens},
            "messages": [
                {"role": "system", "content": ANSWER_QUERY_SYSTEM},
                {"role": "user", "content": build_answer_user_prompt(question, evidence)},
            ],
        }
        try:
            async with self._client.stream("POST", "/api/chat", json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    token = (chunk.get("message") or {}).get("content")
                    if token:
                        yield token
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"ollama http error: {exc}") from exc
