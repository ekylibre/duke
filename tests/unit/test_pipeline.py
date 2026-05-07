from __future__ import annotations

import spacy

from duke.domain.intent import Intent
from duke.integration.ekylibre.lexicon_repo import (
    DEFAULT_PROCEDURES,
    DEFAULT_UNITS,
    InMemoryLexiconRepository,
    Lexicon,
    ProductEntry,
)
from duke.nlu.entity_ruler import LABEL_PARCEL, LABEL_PRODUCT, LABEL_QUANTITY
from duke.nlu.pipeline import NlpPipeline


def _repo() -> InMemoryLexiconRepository:
    return InMemoryLexiconRepository(
        Lexicon(
            products=[ProductEntry(id=1234, name="Karaté Zeon", aliases=("Karate Zeon",))],
            procedures=list(DEFAULT_PROCEDURES),
            units=list(DEFAULT_UNITS),
        )
    )


def test_blank_pipeline_extracts_entities() -> None:
    nlp = spacy.blank("fr")
    pipeline = NlpPipeline(nlp=nlp, lexicon_repo=_repo())
    pipeline.install_entity_ruler(parcel_names=["Bel Air"])

    doc = pipeline._nlp("j'ai pulvérisé 2L de Karaté Zeon sur la parcelle Bel Air")
    labels = {ent.label_: ent.text for ent in doc.ents}
    assert LABEL_PRODUCT in labels
    assert "Karaté Zeon" in labels[LABEL_PRODUCT]
    assert LABEL_PARCEL in labels
    assert LABEL_QUANTITY in labels


def test_pipeline_analyze_returns_record_intent() -> None:
    nlp = spacy.blank("fr")
    pipeline = NlpPipeline(nlp=nlp, lexicon_repo=_repo())
    result = pipeline.analyze(
        "j'ai pulvérisé 2L de Karaté Zeon sur la parcelle Bel Air ce matin",
        parcel_names=["Bel Air"],
    )
    assert result.intent.intent == Intent.RECORD_INTERVENTION
    assert any("Karaté" in c.raw_name for c in result.candidate_products)
    assert any("Bel Air" in c.raw_name for c in result.candidate_parcels)
    assert result.temporal.started_at is not None
