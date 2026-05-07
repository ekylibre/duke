"""End-to-end test for US-1.

Drives the WebSocket through Starlette's TestClient with:
- a real FastAPI app (lifespan replaced by a no-op),
- a real InterventionRecorder built with a fake spaCy pipeline and a fake LLM router,
- httpx calls to Ekylibre intercepted by respx (validate_token + create_intervention).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from duke.application.intervention_recorder import InterventionRecorder
from duke.domain.intent import Intent, IntentResult
from duke.integration.ekylibre.lexicon_repo import (
    DEFAULT_PROCEDURES,
    DEFAULT_UNITS,
    InMemoryLexiconRepository,
    Lexicon,
)
from duke.main import create_app
from duke.nlu.pipeline import NluResult
from duke.nlu.temporal import TemporalExtraction

PARIS = ZoneInfo("Europe/Paris")
EKYLIBRE_BASE = "http://ekylibre.test"


@asynccontextmanager
async def _noop_lifespan(_app: FastAPI):
    yield


class _FakePipeline:
    def analyze(self, text: str, parcel_names=()) -> NluResult:
        return NluResult(
            text=text,
            intent=IntentResult(intent=Intent.RECORD_INTERVENTION, confidence=0.85),
            candidate_products=[],
            candidate_procedures=[],
            candidate_parcels=[],
            raw_quantities=["2L"],
            temporal=TemporalExtraction(
                started_at=datetime(2026, 5, 7, 8, 0, tzinfo=PARIS),
                stopped_at=datetime(2026, 5, 7, 10, 0, tzinfo=PARIS),
                working_duration=timedelta(hours=2),
            ),
        )


class _FakeLLMRouter:
    primary_name = "fake-claude"

    async def extract_intervention(
        self, text: str, hints: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        return (
            {
                "procedure_name": "vine_spraying",
                "started_at": "2026-05-07T08:00:00+02:00",
                "stopped_at": "2026-05-07T10:00:00+02:00",
                "working_duration_seconds": 7200,
                "targets": [
                    {
                        "raw_name": "Bel Air",
                        "resolved_id": 42,
                        "resolved_name": "Bel Air",
                        "kind": "land_parcel",
                    }
                ],
                "inputs": [
                    {
                        "raw_name": "Karaté Zeon",
                        "resolved_product_id": 1234,
                        "resolved_product_name": "Karaté Zeon",
                        "quantity_value": 2.0,
                        "quantity_unit": "liter",
                    }
                ],
                "doers": [],
                "tools": [],
                "ambiguities": [],
                "confidence": 0.92,
            },
            "fake-claude",
        )


@pytest.fixture
def app() -> FastAPI:
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    return app


@pytest.fixture
def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=EKYLIBRE_BASE)


@pytest.fixture
def recorder() -> InterventionRecorder:
    InMemoryLexiconRepository(
        Lexicon(products=[], procedures=list(DEFAULT_PROCEDURES), units=list(DEFAULT_UNITS))
    )
    return InterventionRecorder(pipeline=_FakePipeline(), llm=_FakeLLMRouter())  # type: ignore[arg-type]


def test_us1_full_flow(
    app: FastAPI,
    http_client: httpx.AsyncClient,
    recorder: InterventionRecorder,
) -> None:
    app.state.http_client = http_client
    app.state.intervention_recorder = recorder
    app.state.settings = type(
        "S",
        (),
        {
            "allowed_ws_origins": [],
            "ekylibre_api_base_url": EKYLIBRE_BASE,
        },
    )()

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/users/me").respond(
            200,
            json={"id": 7, "email": "user@farm.example", "full_name": "Jean Vigneron"},
        )
        post_route = router.post(f"{EKYLIBRE_BASE}/api/v2/interventions").respond(
            201,
            json={"id": 999, "url": f"{EKYLIBRE_BASE}/backend/interventions/999"},
        )

        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": "tok-xyz", "tenant": "farm_a", "locale": "fr"})
            auth_ok = ws.receive_json()
            assert auth_ok["type"] == "auth_ok"
            assert auth_ok["user"]["email"] == "user@farm.example"

            ws.send_json(
                {
                    "type": "user_message",
                    "id": "msg-1",
                    "text": (
                        "j'ai pulvérisé 2L de Karaté Zeon sur la parcelle "
                        "Bel Air ce matin pendant 2h"
                    ),
                }
            )

            thinking = ws.receive_json()
            assert thinking["type"] == "thinking"

            draft_msg = ws.receive_json()
            assert draft_msg["type"] == "intervention_draft"
            assert draft_msg["fields"]["procedure_name"] == "vine_spraying"
            assert draft_msg["fields"]["targets"][0]["resolved_id"] == 42
            assert draft_msg["fields"]["inputs"][0]["resolved_product_id"] == 1234
            assert draft_msg["ambiguities"] == []

            ws.send_json(
                {"type": "confirm_intervention", "id": "msg-1", "draft": draft_msg["fields"]}
            )

            created = ws.receive_json()
            assert created["type"] == "intervention_created"
            assert created["ekylibre_id"] == 999
            assert created["url"].endswith("/999")

        assert post_route.called
        sent_payload = post_route.calls.last.request.content
        assert b"vine_spraying" in sent_payload
        assert b"Bel Air" not in sent_payload  # we POST IDs, not names
