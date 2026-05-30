"""Unit tests for LLMRouter provider selection + fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from duke.nlu.llm.base import LLMUnavailableError
from duke.nlu.llm.router import LLMRouter


class _Provider:
    def __init__(self, name: str, *, down: bool = False) -> None:
        self.name = name
        self._down = down
        self.calls = 0

    async def extract_intervention(self, text: str, hints: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self._down:
            raise LLMUnavailableError(f"{self.name} down")
        return {"by": self.name}

    async def answer_query(
        self, question: str, evidence: dict[str, Any]
    ) -> AsyncIterator[str]:
        if self._down:
            raise LLMUnavailableError(f"{self.name} down")
        yield self.name

    async def health(self) -> bool:
        return not self._down


def _router(providers, order):
    return LLMRouter(providers={p.name: p for p in providers}, default_order=order)


@pytest.mark.asyncio
async def test_uses_requested_provider():
    claude, mistral, ollama = _Provider("claude"), _Provider("mistral"), _Provider("ollama")
    router = _router([claude, mistral, ollama], ["claude", "mistral", "ollama"])
    payload, used = await router.extract_intervention("x", {}, provider="ollama")
    assert used == "ollama"
    assert payload == {"by": "ollama"}


@pytest.mark.asyncio
async def test_falls_back_to_default_chain_when_selected_down():
    ollama = _Provider("ollama", down=True)
    claude = _Provider("claude")
    router = _router([claude, ollama], ["claude", "ollama"])
    _, used = await router.extract_intervention("x", {}, provider="ollama")
    assert used == "claude"  # fell through to the default head
    assert ollama.calls == 1


@pytest.mark.asyncio
async def test_unknown_provider_uses_default():
    claude = _Provider("claude")
    router = _router([claude], ["claude"])
    _, used = await router.extract_intervention("x", {}, provider="does-not-exist")
    assert used == "claude"


@pytest.mark.asyncio
async def test_available_lists_default_first():
    router = _router([_Provider("mistral"), _Provider("claude")], ["claude", "mistral"])
    assert router.available()[0] == "claude"
    assert set(router.available()) == {"claude", "mistral"}
    assert router.default_provider == "claude"


@pytest.mark.asyncio
async def test_all_down_raises_unavailable():
    router = _router([_Provider("claude", down=True)], ["claude"])
    with pytest.raises(LLMUnavailableError):
        await router.extract_intervention("x", {})


@pytest.mark.asyncio
async def test_answer_query_fallback_before_first_token():
    router = _router(
        [_Provider("ollama", down=True), _Provider("claude")], ["claude", "ollama"]
    )
    tokens = [t async for t in router.answer_query("q", {}, provider="ollama")]
    assert tokens == ["claude"]
