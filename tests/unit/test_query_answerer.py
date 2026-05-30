from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import pytest

from duke.application.query_answerer import EMPTY_EVIDENCE_MESSAGE, QueryAnswerer
from duke.domain.intent import Intent, IntentResult
from duke.integration.ekylibre.lexicon_repo import (
    InMemoryLexiconRepository,
    Lexicon,
    ProductEntry,
)
from duke.nlu.pipeline import NluResult
from duke.nlu.temporal import TemporalExtraction


class _FakePipeline:
    def __init__(self, intent: Intent) -> None:
        self._intent = intent

    def analyze(self, text: str, parcel_names=()) -> NluResult:
        return NluResult(
            text=text,
            intent=IntentResult(intent=self._intent, confidence=0.9),
            candidate_products=[],
            candidate_procedures=[],
            candidate_parcels=[],
            raw_quantities=[],
            temporal=TemporalExtraction(),
        )


class _FakeReader:
    def __init__(self, stock: dict[str, Any] | None, interventions: list[dict[str, Any]]) -> None:
        self.stock = stock
        self.interventions = interventions

    async def stock_for_variant(self, variant_id: int) -> dict[str, Any] | None:
        if self.stock is None:
            return None
        return {**self.stock, "variant_id": variant_id}

    async def interventions_in_range(self, start, end, limit=200):
        return self.interventions


class _FakeReadDb:
    def __init__(self, reader: _FakeReader) -> None:
        self.reader = reader
        self.last_tenant_schema: str | None = None

    @asynccontextmanager
    async def with_tenant(self, schema: str):
        self.last_tenant_schema = schema
        yield self.reader


class _FakeLLM:
    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self.last_evidence: dict[str, Any] | None = None

    async def answer_query(
        self, question: str, evidence: dict[str, Any], provider: str | None = None
    ) -> AsyncIterator[str]:
        self.last_evidence = evidence
        for token in self._tokens:
            yield token


def _lexicon() -> InMemoryLexiconRepository:
    return InMemoryLexiconRepository(
        Lexicon(products=[ProductEntry(id=1234, name="Karaté Zeon", aliases=("Karate Zeon",))])
    )


@pytest.mark.asyncio
async def test_stock_question_grounds_on_evidence() -> None:
    lexicon = _lexicon()
    reader = _FakeReader(
        stock={"total": 18.5, "last_update": datetime(2026, 5, 4, 14, 0)},
        interventions=[],
    )
    read_db = _FakeReadDb(reader)
    llm = _FakeLLM(tokens=["Il te reste ", "18,5 L ", "de Karaté Zeon."])

    qa = QueryAnswerer(
        pipeline=_FakePipeline(Intent.QA_STOCK),
        lexicon_repo=lexicon,
        read_db=read_db,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
    )

    tokens: list[str] = []
    async for tok in qa.answer_stream("combien de Karaté Zeon me reste-t-il ?", "tenant_a"):
        tokens.append(tok)

    assert "".join(tokens) == "Il te reste 18,5 L de Karaté Zeon."
    assert read_db.last_tenant_schema == "tenant_a"
    assert llm.last_evidence is not None
    assert llm.last_evidence["kind"] == "stock"
    assert llm.last_evidence["matches"][0]["total"] == 18.5
    assert llm.last_evidence["product"]["id"] == 1234


@pytest.mark.asyncio
async def test_stock_question_without_match_returns_canned_message() -> None:
    lexicon = InMemoryLexiconRepository(Lexicon(products=[]))
    read_db = _FakeReadDb(_FakeReader(stock=None, interventions=[]))
    llm = _FakeLLM(tokens=["should not be called"])

    qa = QueryAnswerer(
        pipeline=_FakePipeline(Intent.QA_STOCK),
        lexicon_repo=lexicon,
        read_db=read_db,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
    )

    tokens = [t async for t in qa.answer_stream("stock de Karaté Zeon", "tenant_a")]
    assert tokens == [EMPTY_EVIDENCE_MESSAGE]
    assert llm.last_evidence is None


@pytest.mark.asyncio
async def test_history_question_passes_rows_as_evidence() -> None:
    lexicon = _lexicon()
    rows = [
        {"id": 1, "procedure_name": "spraying", "started_at": datetime(2026, 5, 6, 8, 0)},
        {"id": 2, "procedure_name": "sowing", "started_at": datetime(2026, 5, 5, 9, 0)},
    ]
    read_db = _FakeReadDb(_FakeReader(stock=None, interventions=rows))
    llm = _FakeLLM(tokens=["Tu as fait 2 interventions cette semaine."])

    qa = QueryAnswerer(
        pipeline=_FakePipeline(Intent.QA_HISTORY),
        lexicon_repo=lexicon,
        read_db=read_db,  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
    )

    tokens = [
        t async for t in qa.answer_stream("liste mes interventions cette semaine", "tenant_a")
    ]
    assert tokens == ["Tu as fait 2 interventions cette semaine."]
    assert llm.last_evidence is not None
    assert llm.last_evidence["kind"] == "history"
    assert len(llm.last_evidence["rows"]) == 2
