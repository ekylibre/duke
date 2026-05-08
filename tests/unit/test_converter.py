"""Tests for the YAML annotation → spaCy converter."""

from __future__ import annotations

from pathlib import Path

import pytest
import spacy
import yaml

from duke.nlu.training.converter import (
    AnnotatedExample,
    EntitySpan,
    examples_to_spacy,
    load_annotated_corpus,
    resolve_spans,
)

GOLDEN = Path(__file__).parent.parent / "fixtures" / "golden_phrases.yaml"


def test_resolve_spans_by_literal_substring() -> None:
    text = "j'ai pulvérisé 2L de Karaté Zeon sur la parcelle Bel Air"
    spans = resolve_spans(
        text,
        [
            {"label": "DUKE_PROCEDURE", "span": "pulvérisé"},
            {"label": "DUKE_QUANTITY", "span": "2L"},
            {"label": "DUKE_PRODUCT", "span": "Karaté Zeon"},
            {"label": "DUKE_PARCEL", "span": "Bel Air"},
        ],
    )

    by_label = {s.label: s for s in spans}
    assert text[by_label["DUKE_PROCEDURE"].start : by_label["DUKE_PROCEDURE"].end] == "pulvérisé"
    assert text[by_label["DUKE_QUANTITY"].start : by_label["DUKE_QUANTITY"].end] == "2L"
    assert text[by_label["DUKE_PRODUCT"].start : by_label["DUKE_PRODUCT"].end] == "Karaté Zeon"
    assert text[by_label["DUKE_PARCEL"].start : by_label["DUKE_PARCEL"].end] == "Bel Air"


def test_resolve_spans_explicit_offsets() -> None:
    text = "Karaté Zeon sur Bel Air"
    spans = resolve_spans(
        text,
        [
            {"label": "DUKE_PRODUCT", "start": 0, "end": 11},
            {"label": "DUKE_PARCEL", "start": 16, "end": 23},
        ],
    )
    assert spans == [
        EntitySpan(label="DUKE_PRODUCT", start=0, end=11),
        EntitySpan(label="DUKE_PARCEL", start=16, end=23),
    ]


def test_resolve_spans_nth_picks_correct_occurrence() -> None:
    text = "Bel Air et Bel Air"
    spans = resolve_spans(
        text,
        [
            {"label": "DUKE_PARCEL", "span": "Bel Air", "nth": 0},
            {"label": "DUKE_PARCEL", "span": "Bel Air", "nth": 1},
        ],
    )
    assert spans[0].start == 0
    assert spans[1].start == 11


def test_resolve_spans_missing_substring_raises() -> None:
    with pytest.raises(ValueError, match="not found"):
        resolve_spans("abc", [{"label": "X", "span": "zz"}])


def test_resolve_spans_overlap_rejected() -> None:
    text = "Bouillie bordelaise"
    with pytest.raises(ValueError, match="overlapping"):
        resolve_spans(
            text,
            [
                {"label": "DUKE_PRODUCT", "span": "Bouillie bordelaise"},
                {"label": "DUKE_PRODUCT", "span": "bordelaise"},
            ],
        )


def test_load_annotated_corpus_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "corpus.yaml"
    src.write_text(
        yaml.safe_dump(
            [
                {
                    "text": "j'ai semé du blé sur Bel Air",
                    "intent": "record_intervention",
                    "entities": [
                        {"label": "DUKE_PROCEDURE", "span": "semé"},
                        {"label": "DUKE_PRODUCT", "span": "blé"},
                        {"label": "DUKE_PARCEL", "span": "Bel Air"},
                    ],
                },
                {"text": "merci", "intent": "unknown"},
            ],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    loaded = load_annotated_corpus(src)
    assert len(loaded) == 2
    assert loaded[0].intent == "record_intervention"
    assert {e.label for e in loaded[0].entities} == {
        "DUKE_PROCEDURE",
        "DUKE_PRODUCT",
        "DUKE_PARCEL",
    }
    assert loaded[1].entities == []


def test_golden_corpus_loads_cleanly() -> None:
    """All hand-annotated phrases must parse — guards against typos in spans."""
    loaded = load_annotated_corpus(GOLDEN)
    assert len(loaded) >= 20
    # Every annotated entity's substring must match the resolved offsets.
    for ex in loaded:
        for entity in ex.entities:
            assert ex.text[entity.start : entity.end]


def test_examples_to_spacy_keeps_aligned_spans() -> None:
    nlp = spacy.blank("fr")
    annotated = [
        AnnotatedExample(
            text="j'ai semé du blé sur Bel Air",
            intent="record_intervention",
            entities=[
                EntitySpan(label="DUKE_PROCEDURE", start=5, end=9),  # "semé"
                EntitySpan(label="DUKE_PRODUCT", start=13, end=16),  # "blé"
                EntitySpan(label="DUKE_PARCEL", start=21, end=28),  # "Bel Air"
            ],
        )
    ]
    examples = examples_to_spacy(nlp, annotated)
    assert len(examples) == 1
    ents = examples[0].reference.ents
    labels = sorted(ent.label_ for ent in ents)
    assert labels == ["DUKE_PARCEL", "DUKE_PROCEDURE", "DUKE_PRODUCT"]
