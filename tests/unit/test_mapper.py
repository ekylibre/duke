from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from duke.domain.entities import ResolvedInput, ResolvedTarget
from duke.domain.intervention import InterventionDraft
from duke.integration.ekylibre.mappers import MapperError, intervention_draft_to_payload

PARIS = ZoneInfo("Europe/Paris")


def _full_draft() -> InterventionDraft:
    return InterventionDraft(
        procedure_name="vine_spraying",
        started_at=datetime(2026, 5, 7, 8, 0, tzinfo=PARIS),
        stopped_at=datetime(2026, 5, 7, 10, 0, tzinfo=PARIS),
        working_duration=timedelta(hours=2),
        targets=[
            ResolvedTarget(
                kind="land_parcel",
                raw_name="Bel Air",
                resolved_id=42,
                resolved_name="Bel Air",
                confidence=0.95,
            )
        ],
        inputs=[
            ResolvedInput(
                raw_name="Karaté Zeon",
                resolved_product_id=1234,
                resolved_product_name="Karaté Zeon",
                quantity_value=2.0,
                quantity_unit="liter",
                confidence=0.9,
            )
        ],
        confidence=0.92,
    )


def test_payload_uses_flat_top_level_fields() -> None:
    """Ekylibre's `Api::V2::InterventionsController#create_params` calls
    `super.permit(common_params_to_permit)` which expects intervention fields
    at the top level. Nesting them under `intervention:` strips them all and
    triggers a 400 'Parameters are missings' from the interactor."""
    payload = intervention_draft_to_payload(_full_draft())
    assert payload["procedure_name"] == "vine_spraying"
    # Top-level started_at/stopped_at aren't in the controller's permit list;
    # the dates ride along inside working_periods_attributes instead.
    assert "started_at" not in payload
    assert "stopped_at" not in payload
    # The legacy nesting is gone — assert it doesn't sneak back in.
    assert "intervention" not in payload
    # `nature` is set server-side via `intervention_options`; we don't ship it.
    assert "nature" not in payload


def test_payload_uses_procedure_spec_slot_names() -> None:
    """When a `ProcedureSpec` is supplied, `reference_name` follows Procedo's
    actual parameter slots (e.g. `cultivation` for `generic_tillage`) instead
    of the conservative `land_parcel` fallback. Without this, Ekylibre
    rejects the payload with `Cannot find parameter`/`Parameters are missings`."""
    from duke.integration.ekylibre.api_client import ProcedureSpec

    spec = ProcedureSpec(
        name="generic_tillage",
        parameters=[
            {"name": "cultivation", "type": "target", "required": True},
            {"name": "operator", "type": "doer"},
            {"name": "tractor", "type": "tool"},
        ],
    )
    payload = intervention_draft_to_payload(_full_draft(), procedure_spec=spec)
    assert payload["targets_attributes"][0]["reference_name"] == "cultivation"


def test_payload_falls_back_to_default_slot_names_without_spec() -> None:
    """No spec → use the conservative defaults that work for the common
    procedures Duke already shipped (spraying, fertilizing, …)."""
    payload = intervention_draft_to_payload(_full_draft())
    assert payload["targets_attributes"][0]["reference_name"] == "land_parcel"
    assert payload["inputs_attributes"][0]["reference_name"] == "plant_medicine"


def test_payload_includes_description_from_raw_text() -> None:
    """The user's original phrase rides along as `description` so an admin
    can see where the intervention came from. `description` is in
    `common_params_to_permit`, so Ekylibre keeps it on the record."""
    draft = _full_draft().model_copy(
        update={"raw_text": "labour ce matin sur Aux Verriers - Moulin"}
    )
    payload = intervention_draft_to_payload(draft)
    assert payload["description"] == "labour ce matin sur Aux Verriers - Moulin"


def test_payload_omits_description_when_raw_text_missing() -> None:
    draft = _full_draft().model_copy(update={"raw_text": None})
    payload = intervention_draft_to_payload(draft)
    assert "description" not in payload


def test_payload_includes_provider_envelope() -> None:
    """Ekylibre's Api::V2::BaseController#create_params requires `provider`
    with `vendor` and `name`. Without it the API responds 403 with
    'Provider param is invalid', and the intervention is rejected."""
    payload = intervention_draft_to_payload(_full_draft())
    assert payload["provider"] == {"vendor": "duke", "name": "duke-chatbot"}


def test_collections_use_array_form() -> None:
    """`*_attributes` go out as JSON arrays. `Computation::ComputedParameters`
    calls `.each_with_index` on each (which fails on a Hash/Parameters), then
    converts the Array → Hash for downstream steps. Sending an array is the
    only shape that survives both stages."""
    payload = intervention_draft_to_payload(_full_draft())
    assert payload["targets_attributes"] == [
        {"reference_name": "land_parcel", "product_id": 42}
    ]
    assert payload["inputs_attributes"] == [
        {
            "reference_name": "plant_medicine",
            "product_id": 1234,
            "quantity_value": 2.0,
            "quantity_unit_name": "liter",
        }
    ]


def test_empty_collections_are_omitted() -> None:
    """Empty `inputs_attributes`/`tools_attributes`/`doers_attributes` are
    dropped from the payload. ComputedParameters skips blank collections so
    they don't get Array → Hash converted, then UpdateEngineIntervention
    crashes on `[].keys`. Omitting empty arrays sidesteps both."""
    draft = _full_draft().model_copy(update={"inputs": []})
    payload = intervention_draft_to_payload(draft)
    assert "inputs_attributes" not in payload
    assert "tools_attributes" not in payload
    assert "doers_attributes" not in payload


def test_working_periods_when_stopped_at_set() -> None:
    payload = intervention_draft_to_payload(_full_draft())
    periods = payload["working_periods_attributes"]
    assert len(periods) == 1
    assert periods[0]["started_at"].startswith("2026-05-07T08:00:00")


def test_no_working_periods_without_stopped_at() -> None:
    draft = _full_draft()
    draft = draft.model_copy(update={"stopped_at": None})
    payload = intervention_draft_to_payload(draft)
    assert "working_periods_attributes" not in payload
    assert "stopped_at" not in payload


def test_missing_procedure_raises() -> None:
    draft = _full_draft().model_copy(update={"procedure_name": None})
    with pytest.raises(MapperError):
        intervention_draft_to_payload(draft)


def test_missing_started_at_raises() -> None:
    draft = _full_draft().model_copy(update={"started_at": None})
    with pytest.raises(MapperError):
        intervention_draft_to_payload(draft)


def test_target_without_resolved_id_raises() -> None:
    draft = _full_draft()
    draft.targets[0] = draft.targets[0].model_copy(update={"resolved_id": None})
    with pytest.raises(MapperError):
        intervention_draft_to_payload(draft)


def test_input_without_resolved_id_raises() -> None:
    draft = _full_draft()
    draft.inputs[0] = draft.inputs[0].model_copy(update={"resolved_product_id": None})
    with pytest.raises(MapperError):
        intervention_draft_to_payload(draft)
