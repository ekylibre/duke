from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import structlog
from mistralai import Mistral
from mistralai.models import SDKError

from duke.nlu.llm.base import LLMSchemaError, LLMUnavailableError
from duke.nlu.llm.prompts import (
    ANSWER_QUERY_SYSTEM,
    EXTRACT_INTERVENTION_SYSTEM,
    build_answer_user_prompt,
    build_extraction_user_prompt,
)
from duke.nlu.llm.tools import (
    EXTRACT_INTERVENTION_DESCRIPTION,
    EXTRACT_INTERVENTION_SCHEMA,
    EXTRACT_INTERVENTION_TOOL_NAME,
)

log = structlog.get_logger(__name__)


class MistralProvider:
    name = "mistral"

    def __init__(self, client: Mistral, model: str, max_tokens: int = 1024) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    @classmethod
    def from_api_key(cls, api_key: str, model: str, max_tokens: int = 1024) -> MistralProvider:
        return cls(Mistral(api_key=api_key), model=model, max_tokens=max_tokens)

    async def health(self) -> bool:
        return self._client is not None

    async def extract_intervention(self, text: str, hints: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.chat.complete_async(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": EXTRACT_INTERVENTION_SYSTEM},
                    {"role": "user", "content": build_extraction_user_prompt(text, hints)},
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": EXTRACT_INTERVENTION_TOOL_NAME,
                            "description": EXTRACT_INTERVENTION_DESCRIPTION,
                            "parameters": EXTRACT_INTERVENTION_SCHEMA,
                        },
                    }
                ],
                tool_choice={
                    "type": "function",
                    "function": {"name": EXTRACT_INTERVENTION_TOOL_NAME},
                },
            )
        except SDKError as exc:
            raise LLMUnavailableError(f"mistral sdk error: {exc}") from exc

        if not response.choices:
            raise LLMSchemaError("Mistral returned no choices")

        message = response.choices[0].message
        for tool_call in message.tool_calls or []:
            if tool_call.function.name != EXTRACT_INTERVENTION_TOOL_NAME:
                continue
            try:
                payload = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise LLMSchemaError(f"Mistral returned non-JSON arguments: {exc}") from exc
            if not isinstance(payload, dict):
                raise LLMSchemaError("Mistral tool arguments are not a JSON object")
            return payload

        raise LLMSchemaError("Mistral did not call extract_intervention")

    async def answer_query(
        self,
        question: str,
        evidence: dict[str, Any],
    ) -> AsyncIterator[str]:
        try:
            response = await self._client.chat.complete_async(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": ANSWER_QUERY_SYSTEM},
                    {"role": "user", "content": build_answer_user_prompt(question, evidence)},
                ],
            )
        except SDKError as exc:
            raise LLMUnavailableError(f"mistral sdk error: {exc}") from exc

        if not response.choices:
            raise LLMSchemaError("Mistral returned no choices")
        text = response.choices[0].message.content or ""
        if isinstance(text, list):
            text = "".join(c.text for c in text if hasattr(c, "text"))
        yield text
