"""Tests for `ProcedureRegistry` — the in-memory cache of Ekylibre's
Procedo registry that re-seeds the lexicon and powers procedure-aware
payload mapping."""

from __future__ import annotations

import httpx
import pytest
import respx

from duke.integration.ekylibre.api_client import (
    EkylibreApiClient,
    EkylibreCredentials,
    ProcedureSpec,
)
from duke.integration.ekylibre.lexicon_repo import (
    DEFAULT_PROCEDURES,
    InMemoryLexiconRepository,
    Lexicon,
)
from duke.integration.ekylibre.procedure_registry import (
    ProcedureRegistry,
    parameter_name_for_role,
)

EKYLIBRE_BASE = "http://ekylibre.test"


def _api() -> EkylibreApiClient:
    return EkylibreApiClient(
        EkylibreCredentials(
            email="u@e.x", token="t", tenant="farm_a", base_url=EKYLIBRE_BASE
        ),
        httpx.AsyncClient(base_url=EKYLIBRE_BASE),
    )


def _lexicon() -> InMemoryLexiconRepository:
    return InMemoryLexiconRepository(
        Lexicon(products=[], procedures=list(DEFAULT_PROCEDURES), units=[])
    )


@pytest.mark.asyncio
async def test_hydrate_caches_specs_and_reseeds_lexicon() -> None:
    payload = [
        {
            "name": "generic_tillage",
            "human_name": "Travail du sol",
            "deprecated": False,
            "hidden": False,
            "parameters": [
                {"name": "cultivation", "type": "target", "required": True}
            ],
        },
        {
            "name": "spraying",
            "human_name": "Pulvérisation",
            "deprecated": False,
            "hidden": False,
            "parameters": [
                {"name": "intervened_zone", "type": "target", "required": True},
                {"name": "plant_medicine", "type": "input", "required": True},
            ],
        },
    ]

    api = _api()
    lexicon = _lexicon()
    registry = ProcedureRegistry()

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/procedures").respond(200, json=payload)
        count = await registry.hydrate(api, lexicon)

    assert count == 2
    assert registry.hydrated is True
    # Specs are queryable by name
    spec = registry.get("generic_tillage")
    assert spec is not None
    assert spec.parameters[0].name == "cultivation"
    # Lexicon is re-seeded with the live names. The label comes from human_name.
    procedures = lexicon.lexicon.procedures
    by_name = {p.name: p for p in procedures}
    assert "generic_tillage" in by_name
    assert by_name["generic_tillage"].label == "Travail du sol"
    assert "spraying" in by_name
    # Curated French aliases for known procedures survive the re-seed,
    # so spaCy/LLM hints can still match "labour" → generic_tillage.
    default_aliases = {p.name: p.aliases for p in DEFAULT_PROCEDURES}
    assert by_name["generic_tillage"].aliases == default_aliases["generic_tillage"]


@pytest.mark.asyncio
async def test_hydrate_is_idempotent() -> None:
    api = _api()
    lexicon = _lexicon()
    registry = ProcedureRegistry()

    with respx.mock(assert_all_called=True) as router:
        route = router.get(f"{EKYLIBRE_BASE}/api/v2/procedures").respond(
            200,
            json=[
                {"name": "spraying", "human_name": "Pulvé", "parameters": []}
            ],
        )
        first = await registry.hydrate(api, lexicon)
        second = await registry.hydrate(api, lexicon)

    assert first == 1
    assert second == 0  # cache hit, no second HTTP call
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_hydrate_failure_keeps_defaults() -> None:
    """A network error or 5xx during hydrate must not corrupt the lexicon —
    Duke continues with the static defaults rather than serving an empty list."""
    api = _api()
    lexicon = _lexicon()
    registry = ProcedureRegistry()

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/procedures").respond(503)
        count = await registry.hydrate(api, lexicon)

    assert count == 0
    assert registry.hydrated is False
    # Default lexicon untouched
    assert any(p.name == "spraying" for p in lexicon.lexicon.procedures)


@pytest.mark.asyncio
async def test_get_or_fetch_falls_back_to_show_endpoint() -> None:
    """Before the bulk hydration runs (or for a procedure added since), a
    targeted `GET /api/v2/procedures/:id` populates the cache on demand."""
    api = _api()
    registry = ProcedureRegistry()

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/procedures/generic_tillage").respond(
            200,
            json={
                "name": "generic_tillage",
                "human_name": "Travail du sol",
                "parameters": [{"name": "cultivation", "type": "target"}],
            },
        )
        spec = await registry.get_or_fetch("generic_tillage", api)

    assert spec is not None
    assert spec.name == "generic_tillage"
    # Cached for the next call
    assert registry.get("generic_tillage") is spec


@pytest.mark.asyncio
async def test_get_or_fetch_returns_none_for_unknown_procedure() -> None:
    api = _api()
    registry = ProcedureRegistry()

    with respx.mock(assert_all_called=True) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/procedures/no_such").respond(
            404, json={"errors": ["not found"]}
        )
        result = await registry.get_or_fetch("no_such", api)

    assert result is None
    assert registry.get("no_such") is None


def test_parameter_name_for_role_picks_first_match() -> None:
    spec = ProcedureSpec(
        name="generic_tillage",
        parameters=[
            {"name": "cultivation", "type": "target"},
            {"name": "tractor", "type": "tool"},
            {"name": "operator", "type": "doer"},
        ],
    )
    assert parameter_name_for_role(spec, "target") == "cultivation"
    assert parameter_name_for_role(spec, "tool") == "tractor"
    assert parameter_name_for_role(spec, "doer") == "operator"
    assert parameter_name_for_role(spec, "input") is None


def test_parameter_name_for_role_handles_missing_spec() -> None:
    assert parameter_name_for_role(None, "target") is None
    assert parameter_name_for_role(ProcedureSpec(name="empty"), "target") is None
