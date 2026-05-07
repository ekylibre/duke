"""ConversationOrchestrator: route a user message based on detected intent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import structlog

from duke.application.intervention_recorder import InterventionRecorder
from duke.application.query_answerer import QueryAnswerer
from duke.domain.intent import Intent
from duke.domain.intervention import InterventionDraft
from duke.nlu.intent_classifier import classify_intent

log = structlog.get_logger(__name__)


@dataclass
class DraftReady:
    draft: InterventionDraft


@dataclass
class QaStream:
    stream: AsyncIterator[str]


@dataclass
class OutOfScope:
    reason: str
    suggestion: str | None = None


@dataclass
class UnknownIntent:
    message: str


OrchestratorResult = DraftReady | QaStream | OutOfScope | UnknownIntent


class ConversationOrchestrator:
    def __init__(
        self,
        recorder: InterventionRecorder,
        query_answerer: QueryAnswerer,
    ) -> None:
        self._recorder = recorder
        self._query_answerer = query_answerer

    async def handle(
        self,
        text: str,
        tenant_schema: str,
        parcel_names: list[str] | None = None,
    ) -> OrchestratorResult:
        intent = classify_intent(text).intent
        log.info("orchestrator.routed", intent=intent.value)

        if intent == Intent.RECORD_INTERVENTION:
            draft = await self._recorder.draft_from_text(text, parcel_names)
            return DraftReady(draft=draft)

        if intent in (Intent.QA_STOCK, Intent.QA_HISTORY):
            stream = self._query_answerer.answer_stream(text, tenant_schema)
            return QaStream(stream=stream)

        if intent == Intent.OUT_OF_SCOPE:
            return OutOfScope(
                reason=(
                    "Cette fonction n'est pas encore disponible dans Duke. "
                    "Tu peux la lancer depuis l'interface Ekylibre."
                ),
                suggestion="Ouvre Ekylibre dans un autre onglet.",
            )

        return UnknownIntent(
            message=(
                "Je n'ai pas compris la demande. Peux-tu reformuler en précisant "
                "s'il s'agit d'une saisie ou d'une question ?"
            )
        )
