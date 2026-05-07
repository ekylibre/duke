"""Use case: record an intervention from a free-text French sentence."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

import structlog
from pydantic import ValidationError

from duke.domain.entities import (
    Ambiguity,
    ResolvedDoer,
    ResolvedInput,
    ResolvedTarget,
    ResolvedTool,
)
from duke.domain.intervention import InterventionDraft
from duke.integration.ekylibre.api_client import CreatedIntervention, EkylibreApiClient
from duke.integration.ekylibre.mappers import intervention_draft_to_payload
from duke.nlu.llm.base import LLMSchemaError
from duke.nlu.llm.router import LLMRouter
from duke.nlu.pipeline import NlpPipeline, NluResult

log = structlog.get_logger(__name__)


class InterventionRecorder:
    def __init__(self, pipeline: NlpPipeline, llm: LLMRouter) -> None:
        self._pipeline = pipeline
        self._llm = llm

    async def draft_from_text(
        self, text: str, parcel_names: list[str] | None = None
    ) -> InterventionDraft:
        nlu = self._pipeline.analyze(text, parcel_names=parcel_names or [])
        hints = _hints_from_nlu(nlu)

        try:
            raw, provider = await self._llm.extract_intervention(text, hints)
            log.info("intervention.extracted", provider=provider)
        except LLMSchemaError as exc:
            log.warning("intervention.llm_schema_error", error=str(exc))
            return _draft_from_nlu_only(nlu, text)

        return _build_draft(raw, nlu, text)

    async def confirm(
        self,
        api: EkylibreApiClient,
        draft: InterventionDraft,
    ) -> CreatedIntervention:
        if not draft.is_ready_for_post():
            raise ValueError(
                "draft is not ready for POST (missing fields or unresolved ambiguities)"
            )
        payload = intervention_draft_to_payload(draft)
        return await api.create_intervention(payload)


def _hints_from_nlu(nlu: NluResult) -> dict[str, Any]:
    return {
        "intent": nlu.intent.intent.value,
        "intent_confidence": nlu.intent.confidence,
        "candidate_products": [asdict(c) for c in nlu.candidate_products],
        "candidate_procedures": [asdict(c) for c in nlu.candidate_procedures],
        "candidate_parcels": [asdict(c) for c in nlu.candidate_parcels],
        "raw_quantities": nlu.raw_quantities,
        "temporal": nlu.temporal.model_dump(mode="json"),
    }


def _build_draft(raw: dict[str, Any], nlu: NluResult, text: str) -> InterventionDraft:
    started_at = _coerce_dt(raw.get("started_at")) or nlu.temporal.started_at
    stopped_at = _coerce_dt(raw.get("stopped_at")) or nlu.temporal.stopped_at

    duration: timedelta | None = nlu.temporal.working_duration
    secs = raw.get("working_duration_seconds")
    if isinstance(secs, int) and secs > 0:
        duration = timedelta(seconds=secs)

    targets = [_target_from_dict(t) for t in (raw.get("targets") or [])]
    inputs_ = [_input_from_dict(i) for i in (raw.get("inputs") or [])]
    doers = [
        ResolvedDoer(raw_name=d.get("raw_name", ""))
        for d in (raw.get("doers") or [])
        if d.get("raw_name")
    ]
    tools = [
        ResolvedTool(raw_name=t.get("raw_name", ""))
        for t in (raw.get("tools") or [])
        if t.get("raw_name")
    ]

    ambiguities = [_ambiguity_from_dict(a) for a in (raw.get("ambiguities") or [])]

    if started_at is None:
        ambiguities.append(
            Ambiguity(
                field="started_at",
                raw_value=text,
                question="Quelle est la date de l'intervention ?",
            )
        )
    if not targets:
        ambiguities.append(
            Ambiguity(
                field="targets",
                raw_value=text,
                question="Sur quelle parcelle as-tu réalisé l'intervention ?",
            )
        )
    if any(t.resolved_id is None for t in targets):
        for t in targets:
            if t.resolved_id is None:
                ambiguities.append(
                    Ambiguity(
                        field="targets",
                        raw_value=t.raw_name,
                        question=f"Quelle parcelle correspond à « {t.raw_name} » ?",
                    )
                )

    confidence = float(raw.get("confidence", 0.0) or 0.0)

    try:
        return InterventionDraft(
            procedure_name=raw.get("procedure_name"),
            started_at=started_at,
            stopped_at=stopped_at,
            working_duration=duration,
            targets=targets,
            inputs=inputs_,
            doers=doers,
            tools=tools,
            ambiguities=ambiguities,
            confidence=confidence,
            raw_text=text,
        )
    except ValidationError as exc:
        log.warning("intervention.draft_invalid", error=str(exc))
        return _draft_from_nlu_only(nlu, text)


def _draft_from_nlu_only(nlu: NluResult, text: str) -> InterventionDraft:
    targets = [
        ResolvedTarget(
            kind="land_parcel",
            raw_name=c.raw_name,
            resolved_id=c.resolved_id,
            resolved_name=c.resolved_name,
            confidence=c.score,
        )
        for c in nlu.candidate_parcels
    ]
    return InterventionDraft(
        started_at=nlu.temporal.started_at,
        stopped_at=nlu.temporal.stopped_at,
        working_duration=nlu.temporal.working_duration,
        targets=targets,
        ambiguities=[
            Ambiguity(
                field="llm",
                raw_value=text,
                question="L'extraction a échoué, peux-tu reformuler la phrase ?",
            )
        ],
        confidence=0.0,
        raw_text=text,
    )


def _coerce_dt(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _target_from_dict(t: dict[str, Any]) -> ResolvedTarget:
    return ResolvedTarget(
        kind=t.get("kind") or "land_parcel",
        raw_name=t.get("raw_name", ""),
        resolved_id=t.get("resolved_id"),
        resolved_name=t.get("resolved_name"),
        confidence=float(t.get("confidence", 0.0) or 0.0),
    )


def _input_from_dict(i: dict[str, Any]) -> ResolvedInput:
    return ResolvedInput(
        raw_name=i.get("raw_name", ""),
        resolved_product_id=i.get("resolved_product_id"),
        resolved_product_name=i.get("resolved_product_name"),
        quantity_value=i.get("quantity_value"),
        quantity_unit=i.get("quantity_unit"),
        confidence=float(i.get("confidence", 0.0) or 0.0),
    )


def _ambiguity_from_dict(a: dict[str, Any]) -> Ambiguity:
    return Ambiguity(
        field=a.get("field", "unknown"),
        raw_value=a.get("raw_value", ""),
        options=list(a.get("options") or []),
        question=a.get("question", "Peux-tu préciser ?"),
    )
