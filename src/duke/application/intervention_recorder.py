"""Use case: record an intervention from a free-text French sentence."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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
from duke.integration.ekylibre.api_client import (
    CreatedIntervention,
    EkylibreApiClient,
    ProcedureSpec,
)
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

        draft = _build_draft(raw, nlu, text)
        return self._canonicalize_procedure(draft)

    def _canonicalize_procedure(self, draft: InterventionDraft) -> InterventionDraft:
        """Normalize the LLM-extracted procedure name to the Procedo canonical
        snake_case (e.g. "Labour" / "labour" / "labourer" → "ploughing").

        Ekylibre's interactor rejects requests with unknown procedure names
        ("Cannot find procedure: 'labour'") even though the lexicon has the
        French label as an alias. We resolve via the lexicon and replace the
        free-form value with the canonical name before the payload is built.
        """
        if not draft.procedure_name:
            return draft
        # Defensive: legacy test fakes may not expose lexicon_repo. Treat
        # absence as "no lexicon to canonicalize against" and pass through.
        repo = getattr(self._pipeline, "lexicon_repo", None)
        if repo is None:
            return draft
        matches = repo.find_procedure(draft.procedure_name, limit=1)
        if not matches:
            return draft
        canonical = getattr(matches[0].payload, "name", None)
        if not canonical or canonical == draft.procedure_name:
            return draft
        log.info(
            "intervention.procedure_canonicalized",
            from_=draft.procedure_name,
            to=canonical,
        )
        return draft.model_copy(update={"procedure_name": canonical})

    async def confirm(
        self,
        api: EkylibreApiClient,
        draft: InterventionDraft,
        procedure_spec: ProcedureSpec | None = None,
    ) -> CreatedIntervention:
        if not draft.is_ready_for_post():
            raise ValueError(
                "draft is not ready for POST (missing fields or unresolved ambiguities)"
            )
        payload = intervention_draft_to_payload(draft, procedure_spec=procedure_spec)
        return await api.create_intervention(payload)


def _hints_from_nlu(nlu: NluResult) -> dict[str, Any]:
    return {
        "intent": nlu.intent.intent.value,
        "intent_confidence": nlu.intent.confidence,
        "candidate_products": [asdict(c) for c in nlu.candidate_products],
        "candidate_procedures": [asdict(c) for c in nlu.candidate_procedures],
        "candidate_parcels": [asdict(c) for c in nlu.candidate_parcels],
        "candidate_tools": [asdict(c) for c in nlu.candidate_tools],
        "candidate_doers": [asdict(c) for c in nlu.candidate_workers],
        "raw_quantities": nlu.raw_quantities,
        "temporal": nlu.temporal.model_dump(mode="json"),
    }


def _build_draft(raw: dict[str, Any], nlu: NluResult, text: str) -> InterventionDraft:
    started_at = _coerce_dt(raw.get("started_at")) or nlu.temporal.started_at
    stopped_at = _coerce_dt(raw.get("stopped_at")) or nlu.temporal.stopped_at

    # Default the start date to "now" when the user didn't specify one.
    # Mirrors typical ERP form behaviour (date pre-filled to today). Users
    # who meant a different day will mention it in the phrase ("hier", "la
    # semaine dernière") and the temporal parser will pick it up; or they
    # can adjust in Ekylibre after the draft is recorded.
    # Use Europe/Paris to match the timezone the temporal parser uses when
    # it does fill `started_at` itself, so the draft never mixes tz-aware
    # and tz-naive datetimes. Done early so the LLM-ambiguities filter
    # below sees the resolved value.
    if started_at is None:
        started_at = datetime.now(tz=ZoneInfo("Europe/Paris"))

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

    # We auto-inject a target ambiguity for every unresolved target below; any
    # LLM-provided ambiguity that points at the same raw value would duplicate
    # the prompt. Track the unresolved names so we can filter the LLM batch
    # before merging.
    unresolved_target_names = {
        t.raw_name for t in targets if t.resolved_id is None and t.raw_name
    }
    # Drop LLM ambiguities that target a field we've already filled in code.
    # The LLM is sometimes overzealous (e.g. asks "à quelle heure ?" even
    # when the temporal parser produced a default time); without this filter
    # the user stays blocked behind a question we don't actually need.
    filled_fields: set[str] = set()
    if started_at is not None:
        filled_fields.add("started_at")
    if stopped_at is not None:
        filled_fields.add("stopped_at")
    ambiguities = [
        _ambiguity_from_dict(a)
        for a in (raw.get("ambiguities") or [])
        if (a.get("raw_value") or "") not in unresolved_target_names
        and a.get("field") not in filled_fields
    ]
    # One consolidated multi-select ambiguity covers both cases:
    #   - no targets at all  → user picks the parcels from scratch
    #   - some raw targets unresolved → user picks all parcels in one shot,
    #     including any already-resolved ones they want to keep. A single
    #     prompt avoids the per-target ping-pong loop where the user has to
    #     pick once for each unresolved name.
    if not targets or any(t.resolved_id is None for t in targets):
        ambiguities.append(
            Ambiguity(
                field="targets",
                raw_value=text,
                question="Sur quelle(s) parcelle(s) as-tu réalisé l'intervention ?",
                multi=True,
            )
        )

    # Per-input single-select for any input the LLM/NLU couldn't tie to a
    # tenant product. Each prompt keeps the input's raw_name and quantity;
    # the option pick fills `resolved_product_id`/`resolved_product_name`.
    # Without this the mapper would crash at confirm time on the unresolved
    # `product_id`.
    for inp in inputs_:
        if inp.resolved_product_id is None and inp.raw_name:
            qty = ""
            if inp.quantity_value is not None:
                qty = f"{inp.quantity_value} {inp.quantity_unit or ''}".strip()
                qty = f" ({qty})" if qty else ""
            ambiguities.append(
                Ambiguity(
                    field="inputs",
                    raw_value=inp.raw_name,
                    question=f"Quel produit correspond à « {inp.raw_name} »{qty} ?",
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
    # `or` (rather than `dict.get` defaults) is deliberate: the LLM
    # sometimes returns explicit `null` for these fields, which `get`
    # would surface as `None` and Pydantic would reject for str fields.
    return Ambiguity(
        field=a.get("field") or "unknown",
        raw_value=a.get("raw_value") or "",
        options=list(a.get("options") or []),
        question=a.get("question") or "Peux-tu préciser ?",
        multi=bool(a.get("multi") or False),
        selected=list(a.get("selected") or []),
    )
