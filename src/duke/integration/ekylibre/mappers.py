"""Map Duke domain types to Ekylibre API request payloads."""

from __future__ import annotations

from typing import Any

from duke.domain.intervention import InterventionDraft
from duke.integration.ekylibre.api_client import ProcedureSpec
from duke.integration.ekylibre.procedure_registry import parameter_name_for_role

# Ekylibre's `Api::V2::BaseController#create_params` requires every external
# create request to carry a `provider` envelope ({vendor, name}) so the resource
# can be traced back to its origin. We tag everything Duke writes so an admin
# can audit Duke-authored interventions in Ekylibre's provider tracking.
PROVIDER_VENDOR = "duke"
PROVIDER_NAME = "duke-chatbot"

# Fallback `reference_name` values when no live procedure spec is available.
# These match the slot names of the most common procedures (e.g. spraying,
# fertilizing) but break for procedures with non-standard slots like
# `generic_tillage` (target slot is `cultivation`, not `land_parcel`). The
# live procedure_spec path replaces these for any procedure we know about.
_DEFAULT_TARGET_REFERENCE = "land_parcel"
_DEFAULT_INPUT_REFERENCE = "plant_medicine"
_DEFAULT_TOOL_REFERENCE = "tool"
_DEFAULT_DOER_REFERENCE = "doer"


class MapperError(Exception):
    """Raised when a draft cannot be mapped to a valid Ekylibre payload."""


def intervention_draft_to_payload(
    draft: InterventionDraft,
    procedure_spec: ProcedureSpec | None = None,
) -> dict[str, Any]:
    """Build the API payload for `POST /api/v2/interventions`.

    When `procedure_spec` is provided (the registry is hydrated), the slot
    names (`reference_name`) come from Procedo's actual parameter list — for
    example `generic_tillage` expects `cultivation` for its target, not the
    generic `land_parcel`. Without a spec we fall back to conservative
    defaults that work for the most common procedures.
    """
    if draft.procedure_name is None:
        raise MapperError("procedure_name is required")
    if draft.started_at is None:
        raise MapperError("started_at is required")
    if not draft.targets:
        raise MapperError("at least one target is required")

    target_slot = (
        parameter_name_for_role(procedure_spec, "target") or _DEFAULT_TARGET_REFERENCE
    )
    input_slot = (
        parameter_name_for_role(procedure_spec, "input") or _DEFAULT_INPUT_REFERENCE
    )
    tool_slot = parameter_name_for_role(procedure_spec, "tool") or _DEFAULT_TOOL_REFERENCE
    doer_slot = parameter_name_for_role(procedure_spec, "doer") or _DEFAULT_DOER_REFERENCE

    # The intervention pipeline has two consecutive steps with conflicting
    # expectations on `*_attributes` collections:
    #   - `Computation::ComputedParameters` calls `.each_with_index` (needs Array)
    #   - `Computation::UpdateEngineIntervention` calls `.keys` (needs Hash)
    # ComputedParameters runs first and converts Array → Hash, so we send
    # Array form. The catch: ComputedParameters skips empty collections,
    # leaving an Array that then crashes UpdateEngineIntervention's `.keys`.
    # The fix is to omit any empty `*_attributes` from the payload entirely.
    targets_attrs: list[dict[str, Any]] = []
    for target in draft.targets:
        if target.resolved_id is None:
            raise MapperError(f"target {target.raw_name!r} has no resolved id")
        targets_attrs.append(
            {"reference_name": target_slot, "product_id": target.resolved_id}
        )

    inputs_attrs: list[dict[str, Any]] = []
    for inp in draft.inputs:
        if inp.resolved_product_id is None:
            raise MapperError(f"input {inp.raw_name!r} has no resolved product id")
        attrs: dict[str, Any] = {
            "reference_name": input_slot,
            "product_id": inp.resolved_product_id,
        }
        if inp.quantity_value is not None:
            attrs["quantity_value"] = inp.quantity_value
        if inp.quantity_unit:
            attrs["quantity_unit_name"] = inp.quantity_unit
        inputs_attrs.append(attrs)

    doers_attrs: list[dict[str, Any]] = [
        {"reference_name": doer_slot, "product_id": d.resolved_id}
        for d in draft.doers
        if d.resolved_id is not None
    ]
    tools_attrs: list[dict[str, Any]] = [
        {"reference_name": tool_slot, "product_id": t.resolved_id}
        for t in draft.tools
        if t.resolved_id is not None
    ]

    working_periods: list[dict[str, Any]] = []
    if draft.stopped_at is not None:
        working_periods.append(
            {
                "started_at": draft.started_at.isoformat(),
                "stopped_at": draft.stopped_at.isoformat(),
            }
        )

    # `Api::V2::InterventionsController#create_params` calls
    # `super.permit(common_params_to_permit)` and the permit list expects the
    # intervention fields at the top level (procedure_name, targets_attributes,
    # …) — not nested under an `intervention` key. Anything outside this list
    # gets stripped, so a nested envelope leaves params empty and the interactor
    # raises "Parameters are missings" (HTTP 400). `nature`, `started_at` and
    # `stopped_at` are not in the permit list either — the dates ride along
    # via `working_periods_attributes` and the interactor recomputes them
    # when `auto_calculate_working_periods` is on.
    payload: dict[str, Any] = {
        "provider": {"vendor": PROVIDER_VENDOR, "name": PROVIDER_NAME},
        "procedure_name": draft.procedure_name,
        "targets_attributes": targets_attrs,
    }
    # Carry the original user phrase into Ekylibre's `description` field so
    # an admin browsing interventions can see the natural-language input that
    # produced the record. `description` is in the controller's permit list.
    if draft.raw_text:
        payload["description"] = draft.raw_text
    if inputs_attrs:
        payload["inputs_attributes"] = inputs_attrs
    if doers_attrs:
        payload["doers_attributes"] = doers_attrs
    if tools_attrs:
        payload["tools_attributes"] = tools_attrs
    if working_periods:
        payload["working_periods_attributes"] = working_periods

    return payload
