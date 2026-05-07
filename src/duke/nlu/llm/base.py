from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol


class LLMError(Exception):
    """Base for LLM provider errors."""


class LLMUnavailableError(LLMError):
    """Provider is unreachable, rate-limited, or returned a non-recoverable error."""


class LLMSchemaError(LLMError):
    """Provider returned a structured response that does not match the expected schema."""


class LLMProvider(Protocol):
    name: str

    async def extract_intervention(
        self,
        text: str,
        hints: dict[str, Any],
    ) -> dict[str, Any]: ...

    def answer_query(
        self,
        question: str,
        evidence: dict[str, Any],
    ) -> AsyncIterator[str]: ...

    async def health(self) -> bool: ...
