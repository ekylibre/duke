"""End-to-end test for the Q&A streaming flow.

Drives the WebSocket through Starlette's TestClient with a real
ConversationOrchestrator wired to a real QueryAnswerer; the spaCy pipeline,
the LLM, and the Postgres reader are faked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from duke.application.intervention_recorder import InterventionRecorder
from duke.application.orchestrator import ConversationOrchestrator
from duke.application.query_answerer import QueryAnswerer
from duke.domain.intent import Intent, IntentResult
from duke.integration.ekylibre.lexicon_repo import (
    InMemoryLexiconRepository,
    Lexicon,
    ProductEntry,
)
from duke.main import create_app
from duke.nlu.pipeline import NluResult
from duke.nlu.temporal import TemporalExtraction

EKYLIBRE_BASE = "http://ekylibre.test"


@asynccontextmanager
async def _noop_lifespan(_app: FastAPI):
    yield


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
    async def stock_for_variant(self, variant_id: int) -> dict[str, Any]:
        return {"variant_id": variant_id, "total": 18.5, "last_update": datetime(2026, 5, 6)}

    async def interventions_in_range(self, start, end, limit=200) -> list[dict[str, Any]]:
        return []


class _FakeReadDb:
    @asynccontextmanager
    async def with_tenant(self, schema: str):
        yield _FakeReader()


class _StreamingLLM:
    name = "fake-claude-stream"

    async def answer_query(
        self, question: str, evidence: dict[str, Any], provider: str | None = None
    ) -> AsyncIterator[str]:
        for chunk in ["Il te reste ", "18,5 L ", "de Karaté Zeon ", "(maj 2026-05-06)."]:
            yield chunk

    async def extract_intervention(
        self, text: str, hints: dict[str, Any], provider: str | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError


def _build_components(intent: Intent) -> tuple[InterventionRecorder, ConversationOrchestrator]:
    pipeline = _FakePipeline(intent)
    lexicon = InMemoryLexiconRepository(
        Lexicon(products=[ProductEntry(id=1234, name="Karaté Zeon", aliases=("Karate Zeon",))])
    )
    qa = QueryAnswerer(
        pipeline=pipeline,
        lexicon_repo=lexicon,
        read_db=_FakeReadDb(),  # type: ignore[arg-type]
        llm=_StreamingLLM(),  # type: ignore[arg-type]
    )
    recorder = InterventionRecorder(pipeline=pipeline, llm=_StreamingLLM())  # type: ignore[arg-type]
    orchestrator = ConversationOrchestrator(recorder=recorder, query_answerer=qa)
    return recorder, orchestrator


@pytest.fixture
def app() -> FastAPI:
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    return app


@pytest.fixture
def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=EKYLIBRE_BASE)


def _bind_app(app: FastAPI, http_client: httpx.AsyncClient, intent: Intent) -> None:
    recorder, orchestrator = _build_components(intent)
    app.state.http_client = http_client
    app.state.intervention_recorder = recorder
    app.state.orchestrator = orchestrator
    app.state.settings = type(
        "S",
        (),
        {
            "allowed_ws_origins": [],
            "ekylibre_api_base_url": EKYLIBRE_BASE,
            "rate_limit_per_min": 30,
            "hash_secret": "test-secret",
            "llm_default_provider": "fake",
        },
    )()


def test_qa_stock_streams_and_finalizes(app: FastAPI, http_client: httpx.AsyncClient) -> None:
    _bind_app(app, http_client, Intent.QA_STOCK)

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/users/me").respond(
            200,
            json={"id": 7, "email": "user@farm.example", "full_name": "Jean Vigneron"},
        )

        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.send_json(
                {
                    "type": "auth",
                    "email": "user@farm.example",
                    "token": "tok-xyz",
                    "tenant": "farm_a",
                }
            )
            assert ws.receive_json()["type"] == "auth_ok"

            ws.send_json(
                {
                    "type": "user_message",
                    "id": "msg-1",
                    "text": "combien de Karaté Zeon me reste-t-il ?",
                }
            )

            assert ws.receive_json()["type"] == "thinking"

            tokens: list[str] = []
            while True:
                msg = ws.receive_json()
                if msg["type"] == "assistant_token":
                    tokens.append(msg["delta"])
                    continue
                assert msg["type"] == "assistant_message"
                assert msg["final"] is True
                assert msg["text"] == "".join(tokens)
                assert "18,5 L" in msg["text"]
                assert "Karaté Zeon" in msg["text"]
                break

            assert len(tokens) >= 2  # streaming actually happened


def test_out_of_scope_emits_dedicated_message(app: FastAPI, http_client: httpx.AsyncClient) -> None:
    _bind_app(app, http_client, Intent.OUT_OF_SCOPE)

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/users/me").respond(
            200, json={"id": 1, "email": "u@e.x", "full_name": "U"}
        )

        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.send_json(
                {
                    "type": "auth",
                    "email": "u@e.x",
                    "token": "tok",
                    "tenant": "farm_a",
                }
            )
            assert ws.receive_json()["type"] == "auth_ok"

            ws.send_json({"type": "user_message", "id": "m1", "text": "imprime le grand livre"})
            assert ws.receive_json()["type"] == "thinking"
            out = ws.receive_json()
            assert out["type"] == "out_of_scope"
            assert "n'est pas encore disponible" in out["reason"]
