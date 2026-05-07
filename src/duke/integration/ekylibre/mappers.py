"""Map Duke domain types to Ekylibre API request payloads."""

from __future__ import annotations

from typing import Any

from duke.domain.intervention import InterventionDraft


class MapperError(Exception):
    """Raised when a draft cannot be mapped to a valid Ekylibre payload."""


def intervention_draft_to_payload(draft: InterventionDraft) -> dict[str, Any]:
    if draft.procedure_name is None:
        raise MapperError("procedure_name is required")
    if draft.started_at is None:
        raise MapperError("started_at is required")
    if not draft.targets:
        raise MapperError("at least one target is required")

    targets_attrs: list[dict[str, Any]] = []
    for target in draft.targets:
        if target.resolved_id is None:
            raise MapperError(f"target {target.raw_name!r} has no resolved id")
        targets_attrs.append(
            {
                "reference_name": target.kind,
                "product_id": target.resolved_id,
            }
        )

    inputs_attrs: list[dict[str, Any]] = []
    for inp in draft.inputs:
        if inp.resolved_product_id is None:
            raise MapperError(f"input {inp.raw_name!r} has no resolved product id")
        attrs: dict[str, Any] = {
            "reference_name": "plant_medicine",
            "product_id": inp.resolved_product_id,
        }
        if inp.quantity_value is not None:
            attrs["quantity_value"] = inp.quantity_value
        if inp.quantity_unit:
            attrs["quantity_unit_name"] = inp.quantity_unit
        inputs_attrs.append(attrs)

    working_periods: list[dict[str, Any]] = []
    if draft.stopped_at is not None:
        working_periods.append(
            {
                "started_at": draft.started_at.isoformat(),
                "stopped_at": draft.stopped_at.isoformat(),
            }
        )

    intervention: dict[str, Any] = {
        "procedure_name": draft.procedure_name,
        "nature": draft.nature,
        "started_at": draft.started_at.isoformat(),
        "targets_attributes": targets_attrs,
        "inputs_attributes": inputs_attrs,
        "doers_attributes": [
            {"reference_name": "doer", "product_id": d.resolved_id}
            for d in draft.doers
            if d.resolved_id is not None
        ],
        "tools_attributes": [
            {"reference_name": "tool", "product_id": t.resolved_id}
            for t in draft.tools
            if t.resolved_id is not None
        ],
    }
    if draft.stopped_at is not None:
        intervention["stopped_at"] = draft.stopped_at.isoformat()
    if working_periods:
        intervention["working_periods_attributes"] = working_periods

    return {"intervention": intervention}
