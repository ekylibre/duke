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


def test_full_payload_has_intervention_root() -> None:
    payload = intervention_draft_to_payload(_full_draft())
    assert "intervention" in payload
    intervention = payload["intervention"]
    assert intervention["procedure_name"] == "vine_spraying"
    assert intervention["nature"] == "record"
    assert intervention["started_at"].startswith("2026-05-07T08:00:00")
    assert intervention["stopped_at"].startswith("2026-05-07T10:00:00")


def test_targets_attributes_format() -> None:
    payload = intervention_draft_to_payload(_full_draft())
    targets = payload["intervention"]["targets_attributes"]
    assert targets == [{"reference_name": "land_parcel", "product_id": 42}]


def test_inputs_attributes_format() -> None:
    payload = intervention_draft_to_payload(_full_draft())
    inputs = payload["intervention"]["inputs_attributes"]
    assert inputs == [
        {
            "reference_name": "plant_medicine",
            "product_id": 1234,
            "quantity_value": 2.0,
            "quantity_unit_name": "liter",
        }
    ]


def test_working_periods_when_stopped_at_set() -> None:
    payload = intervention_draft_to_payload(_full_draft())
    periods = payload["intervention"]["working_periods_attributes"]
    assert len(periods) == 1
    assert periods[0]["started_at"].startswith("2026-05-07T08:00:00")


def test_no_working_periods_without_stopped_at() -> None:
    draft = _full_draft()
    draft = draft.model_copy(update={"stopped_at": None})
    payload = intervention_draft_to_payload(draft)
    assert "working_periods_attributes" not in payload["intervention"]
    assert "stopped_at" not in payload["intervention"]


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
