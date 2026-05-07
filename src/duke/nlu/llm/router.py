from __future__ import annotations

from typing import Any

import structlog

from duke.nlu.llm.base import LLMError, LLMProvider, LLMUnavailableError
from duke.observability.metrics import errors_total

log = structlog.get_logger(__name__)


class LLMRouter:
    """Dispatches LLM calls to a primary provider, falling back on transient errors.

    No tenant/task-level overrides are wired in iteration 2; the structure is here
    so iteration 3 can plug them via additional methods.
    """

    def __init__(
        self,
        primary: LLMProvider,
        secondary: LLMProvider | None = None,
    ) -> None:
        self._primary = primary
        self._secondary = secondary

    @property
    def primary_name(self) -> str:
        return self._primary.name

    async def extract_intervention(
        self,
        text: str,
        hints: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        try:
            payload = await self._primary.extract_intervention(text, hints)
            return payload, self._primary.name
        except LLMUnavailableError as exc:
            log.warning("llm.primary_unavailable", provider=self._primary.name, error=str(exc))
            errors_total.labels(code="LLM_UNAVAILABLE").inc()
            if self._secondary is None:
                raise
            payload = await self._secondary.extract_intervention(text, hints)
            return payload, self._secondary.name
        except LLMError:
            raise
