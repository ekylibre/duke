"""Unit tests for `EkylibreApiClient` Procedo procedure endpoints.

The HTTP layer is exercised via `respx` — no real Ekylibre needed. These
tests pin the wire contract: query-string filters, 404 handling for
`get_procedure`, and the JSON shape returned by `Api::V2::ProceduresController`
on the Ekylibre side (validated against `app/views/api/v2/procedures/*.jbuilder`).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from duke.integration.ekylibre.api_client import (
    EkylibreApiClient,
    EkylibreAuthError,
    EkylibreCredentials,
    EkylibreUnavailableError,
)

EKYLIBRE_BASE = "http://ekylibre.test"


def _creds() -> EkylibreCredentials:
    return EkylibreCredentials(
        email="user@farm.example",
        token="tok",
        tenant="closeriedesterres",
        base_url=EKYLIBRE_BASE,
    )


@pytest.fixture
def client() -> EkylibreApiClient:
    http = httpx.AsyncClient(base_url=EKYLIBRE_BASE)
    return EkylibreApiClient(_creds(), http)


@pytest.mark.asyncio
async def test_list_procedures_parses_array(client: EkylibreApiClient) -> None:
    body = [
        {
            "name": "spraying",
            "human_name": "Pulvérisation",
            "deprecated": False,
            "hidden": False,
            "categories": [{"name": "field_work", "human_name": "Travail au champ"}],
            "mandatory_actions": [],
            "optional_actions": [],
            "activity_families": ["plant_farming"],
            "varieties": [],
            "parameters": [
                {"name": "land_parcel", "type": "target", "required": True},
            ],
        },
        {
            "name": "generic_tillage",
            "human_name": "Travail du sol",
            "deprecated": False,
            "hidden": False,
            "parameters": [],
        },
    ]
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/procedures").respond(200, json=body)
        procedures = await client.list_procedures()

    assert [p.name for p in procedures] == ["spraying", "generic_tillage"]
    assert procedures[0].human_name == "Pulvérisation"
    assert procedures[0].categories[0].name == "field_work"
    assert procedures[0].parameters[0].name == "land_parcel"


@pytest.mark.asyncio
async def test_list_procedures_passes_filters(client: EkylibreApiClient) -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.get(f"{EKYLIBRE_BASE}/api/v2/procedures").respond(200, json=[])
        await client.list_procedures(
            category="field_work",
            procedure_action="spray",
            activity_family="plant_farming",
            include_deprecated=True,
            include_hidden=True,
        )

    sent = route.calls.last.request
    qs = dict(sent.url.params)
    # `procedure_action` (not `action`) avoids the Rails reserved-param clash:
    # `params[:action]` always equals the controller action name on the server,
    # so a filter using that key would silently match nothing.
    assert qs == {
        "category": "field_work",
        "procedure_action": "spray",
        "activity_family": "plant_farming",
        "include_deprecated": "true",
        "include_hidden": "true",
    }


@pytest.mark.asyncio
async def test_list_procedures_omits_default_filters(client: EkylibreApiClient) -> None:
    """Default call should send no query string — Ekylibre already excludes
    deprecated and hidden by default; sending the flag explicitly is noisier."""
    with respx.mock(assert_all_called=True) as router:
        route = router.get(f"{EKYLIBRE_BASE}/api/v2/procedures").respond(200, json=[])
        await client.list_procedures()

    sent = route.calls.last.request
    assert sent.url.query == b""


@pytest.mark.asyncio
async def test_get_procedure_returns_spec(client: EkylibreApiClient) -> None:
    body = {
        "name": "generic_tillage",
        "human_name": "Travail du sol",
        "deprecated": False,
        "hidden": False,
        "parameters": [
            {
                "name": "land_parcel",
                "human_name": "Parcelle",
                "type": "target",
                "cardinality": {"minimum": 1, "maximum": None},
                "required": True,
            }
        ],
    }
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/procedures/generic_tillage").respond(200, json=body)
        proc = await client.get_procedure("generic_tillage")

    assert proc is not None
    assert proc.name == "generic_tillage"
    assert proc.parameters[0].cardinality is not None
    assert proc.parameters[0].cardinality.minimum == 1
    assert proc.parameters[0].required is True


@pytest.mark.asyncio
async def test_get_procedure_returns_none_on_404(client: EkylibreApiClient) -> None:
    """404 means the procedure name is unknown — distinct from a tenant 404
    on validate_token. Returning None lets callers fall back to the lexicon
    without an exception."""
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/procedures/no_such_thing").respond(
            404, json={"errors": ["Procedure not found: no_such_thing"]}
        )
        result = await client.get_procedure("no_such_thing")

    assert result is None


@pytest.mark.asyncio
async def test_get_procedure_raises_auth_on_401(client: EkylibreApiClient) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/procedures/spraying").respond(401)
        with pytest.raises(EkylibreAuthError):
            await client.get_procedure("spraying")


@pytest.mark.asyncio
async def test_list_procedures_raises_on_5xx(client: EkylibreApiClient) -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{EKYLIBRE_BASE}/api/v2/procedures").respond(503)
        with pytest.raises(EkylibreUnavailableError):
            await client.list_procedures()
