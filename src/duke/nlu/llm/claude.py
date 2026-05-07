from __future__ import annotations

from typing import Any

import structlog
from anthropic import APIError, APIStatusError, AsyncAnthropic

from duke.nlu.llm.base import LLMSchemaError, LLMUnavailableError
from duke.nlu.llm.prompts import EXTRACT_INTERVENTION_SYSTEM, build_extraction_user_prompt
from duke.nlu.llm.tools import (
    EXTRACT_INTERVENTION_DESCRIPTION,
    EXTRACT_INTERVENTION_SCHEMA,
    EXTRACT_INTERVENTION_TOOL_NAME,
)

log = structlog.get_logger(__name__)


class ClaudeProvider:
    name = "claude"

    def __init__(
        self,
        client: AsyncAnthropic,
        model: str,
        max_tokens: int = 1024,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    @classmethod
    def from_api_key(cls, api_key: str, model: str, max_tokens: int = 1024) -> ClaudeProvider:
        return cls(AsyncAnthropic(api_key=api_key), model=model, max_tokens=max_tokens)

    async def health(self) -> bool:
        return self._client is not None

    async def extract_intervention(self, text: str, hints: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": EXTRACT_INTERVENTION_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[
                    {
                        "name": EXTRACT_INTERVENTION_TOOL_NAME,
                        "description": EXTRACT_INTERVENTION_DESCRIPTION,
                        "input_schema": EXTRACT_INTERVENTION_SCHEMA,
                    }
                ],
                tool_choice={"type": "tool", "name": EXTRACT_INTERVENTION_TOOL_NAME},
                messages=[{"role": "user", "content": build_extraction_user_prompt(text, hints)}],
            )
        except APIStatusError as exc:
            raise LLMUnavailableError(f"claude status {exc.status_code}") from exc
        except APIError as exc:
            raise LLMUnavailableError(f"claude api error: {exc}") from exc

        for block in response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and block.name == EXTRACT_INTERVENTION_TOOL_NAME
            ):
                payload = block.input
                if not isinstance(payload, dict):
                    raise LLMSchemaError("tool_use input is not a dict")
                return payload

        raise LLMSchemaError("Claude did not return a tool_use block for extract_intervention")
