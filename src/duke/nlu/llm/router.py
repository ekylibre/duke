from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import structlog

from duke.nlu.llm.base import LLMProvider, LLMUnavailableError
from duke.observability.metrics import errors_total

log = structlog.get_logger(__name__)


class LLMRouter:
    """Registry of LLM providers with per-call selection and fallback.

    The caller may request a specific provider by name (the user's choice,
    threaded from the WS session); the router tries it first, then falls back
    to the configured default order on `LLMUnavailableError`. Other `LLMError`
    subtypes (e.g. schema errors) propagate without fallback — a malformed
    structured response is a bug to surface, not a transient outage.
    """

    def __init__(
        self,
        providers: dict[str, LLMProvider],
        default_order: list[str],
    ) -> None:
        self._providers = dict(providers)
        # Keep only known providers, preserve order, drop duplicates.
        seen: set[str] = set()
        self._default_order = [
            n for n in default_order if n in self._providers and not (n in seen or seen.add(n))
        ]

    @property
    def default_provider(self) -> str | None:
        return self._default_order[0] if self._default_order else None

    def available(self) -> list[str]:
        """Provider names configured on this router (default first)."""
        ordered = list(self._default_order)
        ordered += [n for n in self._providers if n not in ordered]
        return ordered

    def _chain(self, provider: str | None) -> list[str]:
        """Selected provider first (if valid), then the default order."""
        chain: list[str] = []
        if provider and provider in self._providers:
            chain.append(provider)
        for name in self._default_order:
            if name not in chain:
                chain.append(name)
        return chain

    async def extract_intervention(
        self,
        text: str,
        hints: dict[str, Any],
        provider: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        chain = self._chain(provider)
        if not chain:
            raise LLMUnavailableError("no LLM provider configured")
        last_exc: LLMUnavailableError | None = None
        for name in chain:
            try:
                payload = await self._providers[name].extract_intervention(text, hints)
                return payload, name
            except LLMUnavailableError as exc:
                last_exc = exc
                log.warning("llm.provider_unavailable", provider=name, error=str(exc))
                errors_total.labels(code="LLM_UNAVAILABLE").inc()
                continue
        raise last_exc or LLMUnavailableError("all LLM providers failed")

    async def answer_query(
        self,
        question: str,
        evidence: dict[str, Any],
        provider: str | None = None,
    ) -> AsyncIterator[str]:
        chain = self._chain(provider)
        if not chain:
            raise LLMUnavailableError("no LLM provider configured")
        last_exc: LLMUnavailableError | None = None
        for name in chain:
            yielded = False
            try:
                async for token in self._providers[name].answer_query(question, evidence):
                    yielded = True
                    yield token
                return
            except LLMUnavailableError as exc:
                last_exc = exc
                log.warning("llm.provider_unavailable", provider=name, error=str(exc))
                errors_total.labels(code="LLM_UNAVAILABLE").inc()
                # Can't recover mid-stream once tokens reached the client.
                if yielded:
                    raise
                continue
        raise last_exc or LLMUnavailableError("all LLM providers failed")
