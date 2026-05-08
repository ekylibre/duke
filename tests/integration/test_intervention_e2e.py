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
from duke.application.orchestrator import ConversationOrchestrator
from duke.application.query_answerer import QueryAnswerer
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
    def __init__(self, lexicon_repo=None) -> None:
        # The recorder's procedure-canonicalization step looks up free-form
        # names against this repo. Tests that don't care can omit it.
        self.lexicon_repo = lexicon_repo

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
    lexicon = InMemoryLexiconRepository(
        Lexicon(products=[], procedures=list(DEFAULT_PROCEDURES), units=list(DEFAULT_UNITS))
    )

    class _NullReadDb:
        from contextlib import asynccontextmanager as _acm

        @_acm
        async def with_tenant(self, schema: str):
            class _R:
                async def stock_for_variant(self, _vid: int):
                    return None

                async def interventions_in_range(self, _start, _end, limit=200):
                    return []

            yield _R()

    qa = QueryAnswerer(
        pipeline=_FakePipeline(),
        lexicon_repo=lexicon,
        read_db=_NullReadDb(),  # type: ignore[arg-type]
        llm=_FakeLLMRouter(),  # type: ignore[arg-type]
    )
    orchestrator = ConversationOrchestrator(recorder=recorder, query_answerer=qa)

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
            ws.send_json(
                {
                    "type": "auth",
                    "email": "user@farm.example",
                    "token": "tok-xyz",
                    "tenant": "farm_a",
                    "locale": "fr",
                }
            )
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
        # Targets/inputs go by id, not by name — verify the structured fields.
        assert b'"product_id":42' in sent_payload
        assert b'"product_id":1234' in sent_payload
        # Ekylibre requires the `provider` envelope to accept external creates.
        assert b'"vendor":"duke"' in sent_payload
        # The user's original phrase rides along as `description` so an admin
        # browsing interventions can see the natural-language input that
        # produced the record. Server-side state (ctx.drafts) carries
        # raw_text — the client doesn't echo it on confirm.
        assert b'"description":"j\'ai pulv' in sent_payload


class _AmbiguousThenResolvingLLM:
    """Fake LLM that returns an ambiguous draft on first call (parcel unresolved),
    then a fully-resolved draft once the user's clarification is folded into the
    text via Duke's `_handle_clarify`. Mirrors the real LLM contract closely
    enough for the orchestrator + ws_server to drive the round-trip.
    """

    primary_name = "fake-claude"

    async def extract_intervention(
        self, text: str, hints: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        clarified = "Précision" in text  # Duke appends "Précision : <answer>" on clarify
        target = {
            "raw_name": "VR du Verrier",
            "resolved_id": 77 if clarified else None,
            "resolved_name": "VR du Verrier 2" if clarified else None,
            "kind": "land_parcel",
        }
        return (
            {
                "procedure_name": "ploughing",
                "started_at": "2026-05-08T08:00:00+02:00",
                "stopped_at": "2026-05-08T11:00:00+02:00",
                "targets": [target],
                "inputs": [],
                "doers": [],
                "tools": [],
                "ambiguities": [],
                "confidence": 0.7,
            },
            "fake-claude",
        )


def test_clarify_resolves_ambiguity_and_reemits_draft(
    app: FastAPI,
    http_client: httpx.AsyncClient,
) -> None:
    recorder = InterventionRecorder(
        pipeline=_FakePipeline(),
        llm=_AmbiguousThenResolvingLLM(),  # type: ignore[arg-type]
    )
    lexicon = InMemoryLexiconRepository(
        Lexicon(products=[], procedures=list(DEFAULT_PROCEDURES), units=list(DEFAULT_UNITS))
    )

    class _NullReadDb:
        from contextlib import asynccontextmanager as _acm

        @_acm
        async def with_tenant(self, schema: str):
            class _R:
                async def stock_for_variant(self, _vid: int):
                    return None

                async def interventions_in_range(self, _start, _end, limit=200):
                    return []

            yield _R()

    qa = QueryAnswerer(
        pipeline=_FakePipeline(),
        lexicon_repo=lexicon,
        read_db=_NullReadDb(),  # type: ignore[arg-type]
        llm=_AmbiguousThenResolvingLLM(),  # type: ignore[arg-type]
    )
    orchestrator = ConversationOrchestrator(recorder=recorder, query_answerer=qa)

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

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/users/me").respond(
            200, json={"id": 7, "email": "user@farm.example", "full_name": "Jean Vigneron"}
        )

        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.send_json(
                {
                    "type": "auth",
                    "email": "user@farm.example",
                    "token": "tok",
                    "tenant": "farm_a",
                }
            )
            assert ws.receive_json()["type"] == "auth_ok"

            ws.send_json(
                {
                    "type": "user_message",
                    "id": "draft-1",
                    "text": "labour ce matin sur VR du Verrier",
                }
            )
            assert ws.receive_json()["type"] == "thinking"

            first = ws.receive_json()
            assert first["type"] == "intervention_draft"
            # parcel unresolved → ambiguity injected by the recorder
            assert first["fields"]["targets"][0]["resolved_id"] is None
            assert any("Verrier" in a["question"] for a in first["ambiguities"])

            ws.send_json(
                {"type": "clarify", "id": "draft-1", "answer": "il s'agit de Verrier 2"}
            )
            assert ws.receive_json()["type"] == "thinking"

            second = ws.receive_json()
            assert second["type"] == "intervention_draft"
            assert second["id"] == "draft-1"  # same id → widget replaces card
            assert second["fields"]["targets"][0]["resolved_id"] == 77
            assert second["ambiguities"] == []


class _FakeParcelReadDb:
    """Tenant-scoped read DB that returns a fixed list of land parcels.

    Used to drive the option-pick fast path: the clarify handler fuzzy-matches
    the unresolved target name against this list, surfaces the top hits as
    `Ambiguity.options`, and resolves the chosen option to a parcel id without
    invoking the LLM again.
    """

    def __init__(self, parcels: list[dict[str, Any]]):
        self._parcels = parcels

    from contextlib import asynccontextmanager as _acm

    @_acm
    async def with_tenant(self, schema: str):
        parcels = self._parcels

        class _R:
            async def list_land_parcels(self, limit: int = 500):
                return list(parcels[:limit])

            async def stock_for_variant(self, _vid: int):
                return None

            async def interventions_in_range(self, _start, _end, limit=200):
                return []

        yield _R()


def test_parcel_options_are_picked_directly_without_llm(
    app: FastAPI,
    http_client: httpx.AsyncClient,
) -> None:
    """End-to-end: draft has a parcel ambiguity, the widget receives options
    fetched from the tenant DB, and clicking an option patches the draft via
    the option-pick fast path (no second LLM call)."""

    llm_calls: list[str] = []

    class _LLMOnceAmbiguous:
        primary_name = "fake-claude"

        async def extract_intervention(
            self, text: str, hints: dict[str, Any]
        ) -> tuple[dict[str, Any], str]:
            llm_calls.append(text)
            return (
                {
                    "procedure_name": "ploughing",
                    "started_at": "2026-05-08T08:00:00+02:00",
                    "stopped_at": "2026-05-08T11:00:00+02:00",
                    "targets": [
                        {
                            "raw_name": "VR du Verrier",
                            "resolved_id": None,
                            "resolved_name": None,
                            "kind": "land_parcel",
                        }
                    ],
                    "inputs": [],
                    "doers": [],
                    "tools": [],
                    "ambiguities": [],
                    "confidence": 0.6,
                },
                "fake-claude",
            )

    recorder = InterventionRecorder(
        pipeline=_FakePipeline(),
        llm=_LLMOnceAmbiguous(),  # type: ignore[arg-type]
    )
    lexicon = InMemoryLexiconRepository(
        Lexicon(products=[], procedures=list(DEFAULT_PROCEDURES), units=list(DEFAULT_UNITS))
    )
    read_db = _FakeParcelReadDb(
        [
            {"id": 100, "name": "Verrier 1"},
            {"id": 101, "name": "Verrier 2"},
            {"id": 102, "name": "Bel Air"},
        ]
    )

    qa = QueryAnswerer(
        pipeline=_FakePipeline(),
        lexicon_repo=lexicon,
        read_db=read_db,  # type: ignore[arg-type]
        llm=_LLMOnceAmbiguous(),  # type: ignore[arg-type]
    )
    orchestrator = ConversationOrchestrator(recorder=recorder, query_answerer=qa)

    app.state.http_client = http_client
    app.state.intervention_recorder = recorder
    app.state.orchestrator = orchestrator
    app.state.read_db = read_db
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

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/users/me").respond(
            200, json={"id": 7, "email": "u@e.x", "full_name": "U"}
        )

        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.send_json(
                {"type": "auth", "email": "u@e.x", "token": "t", "tenant": "farm_a"}
            )
            assert ws.receive_json()["type"] == "auth_ok"

            ws.send_json(
                {
                    "type": "user_message",
                    "id": "draft-1",
                    "text": "labour ce matin sur VR du Verrier",
                }
            )
            assert ws.receive_json()["type"] == "thinking"

            first = ws.receive_json()
            assert first["type"] == "intervention_draft"
            target_amb = next(a for a in first["ambiguities"] if a["field"] == "targets")
            # Top-N fuzzy-matched parcel names exposed as click targets.
            assert "Verrier 1" in target_amb["options"]
            assert "Verrier 2" in target_amb["options"]

            llm_calls_before = len(llm_calls)
            ws.send_json({"type": "clarify", "id": "draft-1", "answer": "Verrier 2"})

            second = ws.receive_json()
            assert second["type"] == "intervention_draft"
            assert second["id"] == "draft-1"
            assert second["fields"]["targets"][0]["resolved_id"] == 101
            assert second["fields"]["targets"][0]["resolved_name"] == "Verrier 2"
            assert second["ambiguities"] == []
            # Fast path: no extra LLM extraction was triggered for the clarify.
            assert len(llm_calls) == llm_calls_before


async def test_recorder_canonicalizes_procedure_name_via_lexicon() -> None:
    """LLMs frequently emit the French label ("Labour") rather than the Procedo
    snake_case ("ploughing"). The recorder must look it up via the lexicon
    aliases and rewrite procedure_name before the payload is built — otherwise
    Ekylibre rejects the create with `Cannot find procedure: "labour"` (HTTP
    400)."""

    class _LLMReturnsFrenchLabel:
        primary_name = "fake-claude"

        async def extract_intervention(
            self, text: str, hints: dict[str, Any]
        ) -> tuple[dict[str, Any], str]:
            return (
                {
                    "procedure_name": "labour",  # the French label, not the Procedo name
                    "started_at": "2026-05-08T08:00:00+02:00",
                    "stopped_at": "2026-05-08T11:00:00+02:00",
                    "targets": [
                        {
                            "raw_name": "Bel Air",
                            "resolved_id": 42,
                            "resolved_name": "Bel Air",
                            "kind": "land_parcel",
                        }
                    ],
                    "inputs": [],
                    "doers": [],
                    "tools": [],
                    "ambiguities": [],
                    "confidence": 0.6,
                },
                "fake-claude",
            )

    lexicon = InMemoryLexiconRepository(
        Lexicon(products=[], procedures=list(DEFAULT_PROCEDURES), units=list(DEFAULT_UNITS))
    )
    recorder = InterventionRecorder(
        pipeline=_FakePipeline(lexicon_repo=lexicon),
        llm=_LLMReturnsFrenchLabel(),  # type: ignore[arg-type]
    )

    draft = await recorder.draft_from_text("labour ce matin sur Bel Air")
    assert draft.procedure_name == "generic_tillage"


def test_clarify_without_existing_draft_returns_error(
    app: FastAPI,
    http_client: httpx.AsyncClient,
    recorder: InterventionRecorder,
) -> None:
    lexicon = InMemoryLexiconRepository(
        Lexicon(products=[], procedures=list(DEFAULT_PROCEDURES), units=list(DEFAULT_UNITS))
    )

    class _NullReadDb:
        from contextlib import asynccontextmanager as _acm

        @_acm
        async def with_tenant(self, schema: str):
            class _R:
                async def stock_for_variant(self, _vid: int):
                    return None

                async def interventions_in_range(self, _start, _end, limit=200):
                    return []

            yield _R()

    qa = QueryAnswerer(
        pipeline=_FakePipeline(),
        lexicon_repo=lexicon,
        read_db=_NullReadDb(),  # type: ignore[arg-type]
        llm=_FakeLLMRouter(),  # type: ignore[arg-type]
    )
    orchestrator = ConversationOrchestrator(recorder=recorder, query_answerer=qa)

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

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/users/me").respond(
            200, json={"id": 7, "email": "u@e.x", "full_name": "U"}
        )

        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            ws.send_json(
                {"type": "auth", "email": "u@e.x", "token": "t", "tenant": "farm_a"}
            )
            assert ws.receive_json()["type"] == "auth_ok"

            # No prior draft for this id — server must error rather than crash.
            ws.send_json({"type": "clarify", "id": "ghost", "answer": "réponse"})
            err = ws.receive_json()
            assert err["type"] == "error"
            assert err["id"] == "ghost"
