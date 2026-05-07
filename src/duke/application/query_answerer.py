"""Use case: answer a French question by reading Ekylibre data, no writes.

The QueryAnswerer:
- resolves the relevant entity (product, parcel) via the LexiconRepository,
- runs a parameterized read-only SQL via EkylibreReadDb under the tenant schema,
- streams an LLM-grounded French answer to the caller.

It deliberately does not let the LLM choose the SQL — only the framing of the
answer. Evidence is built deterministically from the user's intent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from duke.domain.intent import Intent
from duke.integration.ekylibre.lexicon_repo import LexiconRepository, ProductEntry
from duke.integration.ekylibre.read_db import EkylibreReadDb
from duke.nlu.llm.router import LLMRouter
from duke.nlu.pipeline import NlpPipeline
from duke.nlu.temporal import parse_french_temporal

log = structlog.get_logger(__name__)


EMPTY_EVIDENCE_MESSAGE = "Je n'ai pas trouvé cette information dans tes données."


class QueryAnswerer:
    def __init__(
        self,
        pipeline: NlpPipeline,
        lexicon_repo: LexiconRepository,
        read_db: EkylibreReadDb,
        llm: LLMRouter,
    ) -> None:
        self._pipeline = pipeline
        self._lexicon_repo = lexicon_repo
        self._read_db = read_db
        self._llm = llm

    async def answer_stream(
        self,
        question: str,
        tenant_schema: str,
    ) -> AsyncIterator[str]:
        nlu = self._pipeline.analyze(question)
        intent = nlu.intent.intent

        if intent == Intent.QA_STOCK:
            evidence = await self._evidence_stock(question, tenant_schema)
        elif intent == Intent.QA_HISTORY:
            evidence = await self._evidence_history(question, tenant_schema)
        else:
            evidence = {"intent": intent.value, "note": "intent not supported by QueryAnswerer"}

        if not evidence.get("matches") and not evidence.get("rows"):
            yield EMPTY_EVIDENCE_MESSAGE
            return

        async for token in self._llm.answer_query(question, evidence):
            yield token

    async def _evidence_stock(self, question: str, tenant_schema: str) -> dict[str, Any]:
        product_match = self._best_product_match(question)
        if product_match is None:
            return {"kind": "stock", "matches": [], "reason": "no_product_match"}

        async with self._read_db.with_tenant(tenant_schema) as reader:
            stock = await reader.stock_for_variant(product_match.id)

        if stock is None:
            return {"kind": "stock", "matches": [], "reason": "no_stock_row"}

        return {
            "kind": "stock",
            "product": {"id": product_match.id, "name": product_match.name},
            "matches": [stock],
        }

    async def _evidence_history(self, question: str, tenant_schema: str) -> dict[str, Any]:
        start, end = self._range_for_history(question)

        async with self._read_db.with_tenant(tenant_schema) as reader:
            rows = await reader.interventions_in_range(start, end, limit=50)

        return {
            "kind": "history",
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "rows": rows,
        }

    def _best_product_match(self, question: str) -> ProductEntry | None:
        for token in _content_words(question):
            hits = self._lexicon_repo.find_product(token, limit=1)
            if hits:
                payload = hits[0].payload
                if isinstance(payload, ProductEntry):
                    return payload
        # Try the whole sentence as a fallback (rapidfuzz tolerates extra context).
        hits = self._lexicon_repo.find_product(question, limit=1)
        if hits and isinstance(hits[0].payload, ProductEntry):
            return hits[0].payload
        return None

    def _range_for_history(self, question: str) -> tuple[datetime, datetime]:
        now = datetime.now(tz=UTC)
        temporal = parse_french_temporal(question, now=now)

        if temporal.started_at is not None:
            start = temporal.started_at
            end = temporal.stopped_at or (start + timedelta(days=1))
            return start, end

        question_lower = question.lower()
        if "semaine" in question_lower:
            start = now - timedelta(days=7)
            return start, now
        if "mois" in question_lower:
            start = now - timedelta(days=30)
            return start, now
        if "année" in question_lower or "annee" in question_lower or "an" in question_lower.split():
            start = now - timedelta(days=365)
            return start, now

        # Default: last 7 days.
        return now - timedelta(days=7), now


def _content_words(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.split():
        token = raw.strip(" ,.;:!?'\"()[]")
        if len(token) >= 4 and token.lower() not in _STOPWORDS:
            out.append(token)
    return out


_STOPWORDS = {
    "combien",
    "quelle",
    "quelles",
    "quel",
    "quels",
    "stock",
    "reste",
    "restant",
    "disponible",
    "depuis",
    "pour",
    "dans",
    "cette",
    "semaine",
    "mois",
    "annee",
    "année",
    "liste",
    "historique",
    "interventions",
    "intervention",
}
