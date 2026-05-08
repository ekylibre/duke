"""spaCy pipeline factory for Duke.

Loads `fr_core_news_lg` (or any spaCy model name from settings); falls back to a
blank `fr` pipeline if the model is not installed (useful for tests). An
EntityRuler is appended with patterns built from the lexicon so the pipeline
can recognize products / procedures / parcels by name.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import spacy
import structlog
from spacy.language import Language
from spacy.tokens import Doc

from duke.domain.intent import IntentResult
from duke.integration.ekylibre.lexicon_repo import LexiconRepository, Match
from duke.nlu.entity_ruler import (
    LABEL_PARCEL,
    LABEL_PRODUCT,
    LABEL_QUANTITY,
    patterns_from_lexicon,
)
from duke.nlu.intent_classifier import classify_intent
from duke.nlu.temporal import TemporalExtraction, parse_french_temporal

log = structlog.get_logger(__name__)


@dataclass
class CandidateMatch:
    raw_name: str
    resolved_id: int | None = None
    resolved_name: str | None = None
    score: float = 0.0


@dataclass
class NluResult:
    text: str
    intent: IntentResult
    candidate_products: list[CandidateMatch]
    candidate_procedures: list[CandidateMatch]
    candidate_parcels: list[CandidateMatch]
    raw_quantities: list[str]
    temporal: TemporalExtraction


def load_nlp(model_name_or_path: str) -> Language:
    """Load a spaCy `Language` from a model name or an on-disk model directory.

    `spacy.load` accepts either, so callers can pass `Settings.duke_ner_model_path`
    (a trained Duke NER) or `Settings.spacy_model` (the base model name).
    """
    try:
        return spacy.load(model_name_or_path, disable=["parser"])
    except OSError:
        log.warning(
            "spacy.model_not_found",
            model=model_name_or_path,
            fallback="blank-fr",
        )
        return spacy.blank("fr")


class NlpPipeline:
    def __init__(
        self,
        nlp: Language,
        lexicon_repo: LexiconRepository,
        parcel_names_provider=None,
    ) -> None:
        self._nlp = nlp
        self._lexicon_repo = lexicon_repo
        self._parcel_names_provider = parcel_names_provider
        self._ruler_installed = False

    @classmethod
    def build(
        cls,
        model_name: str,
        lexicon_repo: LexiconRepository,
        parcel_names_provider=None,
        ner_model_path: str | None = None,
    ) -> NlpPipeline:
        """Build the pipeline.

        When `ner_model_path` is provided and points to an existing directory,
        the trained Duke NER is loaded; otherwise we fall back to `model_name`
        (typically `fr_core_news_lg`).
        """
        target = model_name
        if ner_model_path:
            from pathlib import Path

            if Path(ner_model_path).exists():
                log.info("nlu.loading_trained_ner", path=ner_model_path)
                target = ner_model_path
            else:
                log.warning(
                    "nlu.ner_model_path_missing",
                    path=ner_model_path,
                    fallback=model_name,
                )
        return cls(load_nlp(target), lexicon_repo, parcel_names_provider)

    @property
    def lexicon_repo(self) -> LexiconRepository:
        return self._lexicon_repo

    def install_entity_ruler(self, parcel_names: Iterable[str] = ()) -> None:
        patterns = patterns_from_lexicon(self._lexicon_repo.lexicon, parcel_names=parcel_names)
        if "duke_entity_ruler" in self._nlp.pipe_names:
            self._nlp.remove_pipe("duke_entity_ruler")
        ruler = self._nlp.add_pipe(
            "entity_ruler",
            name="duke_entity_ruler",
            before="ner" if "ner" in self._nlp.pipe_names else None,
            config={"overwrite_ents": True},
        )
        ruler.add_patterns(patterns)  # type: ignore[attr-defined]
        self._ruler_installed = True

    def analyze(self, text: str, parcel_names: Iterable[str] = ()) -> NluResult:
        if not self._ruler_installed:
            self.install_entity_ruler(parcel_names=parcel_names)

        doc: Doc = self._nlp(text)

        candidate_products: list[CandidateMatch] = []
        candidate_parcels: list[CandidateMatch] = []
        raw_quantities: list[str] = []

        for ent in doc.ents:
            if ent.label_ == LABEL_PRODUCT:
                ent_id = ent.ent_id_ or ""
                resolved_id = int(ent_id) if ent_id.isdigit() else None
                candidate_products.append(
                    CandidateMatch(
                        raw_name=ent.text,
                        resolved_id=resolved_id,
                        resolved_name=ent.text,
                        score=1.0,
                    )
                )
            elif ent.label_ == LABEL_PARCEL:
                candidate_parcels.append(
                    CandidateMatch(raw_name=ent.text, resolved_name=ent.text, score=1.0)
                )
            elif ent.label_ == LABEL_QUANTITY:
                raw_quantities.append(ent.text)

        candidate_procedures = self._infer_procedure_candidates(text)

        if not candidate_products:
            for token in _content_tokens(doc):
                hits = self._lexicon_repo.find_product(token, limit=1)
                if hits:
                    candidate_products.append(_match_to_candidate(hits[0]))

        intent = classify_intent(text)
        temporal = parse_french_temporal(text)

        return NluResult(
            text=text,
            intent=intent,
            candidate_products=candidate_products,
            candidate_procedures=candidate_procedures,
            candidate_parcels=candidate_parcels,
            raw_quantities=raw_quantities,
            temporal=temporal,
        )

    def _infer_procedure_candidates(self, text: str) -> list[CandidateMatch]:
        hits = self._lexicon_repo.find_procedure(text, limit=3)
        return [_match_to_candidate(h) for h in hits]


def _content_tokens(doc: Doc) -> list[str]:
    out: list[str] = []
    for token in doc:
        if token.is_alpha and len(token.text) >= 4 and not token.is_stop:
            out.append(token.text)
    return out


def _match_to_candidate(match: Match) -> CandidateMatch:
    payload = match.payload
    resolved_id = getattr(payload, "id", None)
    resolved_name = getattr(payload, "label", None) or getattr(payload, "name", None)
    return CandidateMatch(
        raw_name=match.raw,
        resolved_id=resolved_id,
        resolved_name=resolved_name,
        score=match.score / 100.0,
    )
