"""Tests for the NER training data synthesizer."""

from __future__ import annotations

from duke.nlu.training.synth import (
    DEFAULT_PARCELS,
    DEFAULT_PRODUCTS,
    SynthConfig,
    synthesize_corpus,
)


def test_synth_is_deterministic_given_seed() -> None:
    a = synthesize_corpus(SynthConfig(seed=123, n_examples=20))
    b = synthesize_corpus(SynthConfig(seed=123, n_examples=20))
    assert [(x.text, x.intent) for x in a] == [(x.text, x.intent) for x in b]


def test_synth_different_seeds_diverge() -> None:
    a = synthesize_corpus(SynthConfig(seed=1, n_examples=20))
    b = synthesize_corpus(SynthConfig(seed=2, n_examples=20))
    assert [x.text for x in a] != [x.text for x in b]


def test_synth_spans_match_text() -> None:
    """Every entity span must reference an actual substring of `text`."""
    corpus = synthesize_corpus(SynthConfig(seed=7, n_examples=200))
    for example in corpus:
        for entity in example.entities:
            chunk = example.text[entity.start : entity.end]
            assert chunk, f"empty span on {example.text!r}: {entity!r}"
            # Either the slot value remains a known pool entry or it's a
            # procedure form / quantity — assert structural sanity not value.
            assert 0 <= entity.start < entity.end <= len(example.text)


def test_synth_intent_is_set() -> None:
    corpus = synthesize_corpus(SynthConfig(seed=5, n_examples=40))
    seen = {ex.intent for ex in corpus}
    assert seen.issubset({"record_intervention", "qa_stock", "qa_history"})
    # With 40 samples we should hit the larger record_intervention pool.
    assert "record_intervention" in seen


def test_synth_uses_provided_pools() -> None:
    corpus = synthesize_corpus(
        SynthConfig(
            seed=0,
            n_examples=30,
            products=["UNIQUE_PRODUCT_X"],
            parcels=["UNIQUE_PARCEL_Y"],
        )
    )
    # Any record/qa example that picks a product or parcel must come from our
    # custom pool.
    for ex in corpus:
        for entity in ex.entities:
            chunk = ex.text[entity.start : entity.end]
            if entity.label == "DUKE_PRODUCT":
                assert chunk == "UNIQUE_PRODUCT_X"
            elif entity.label == "DUKE_PARCEL":
                assert chunk == "UNIQUE_PARCEL_Y"


def test_default_pools_are_non_empty() -> None:
    assert DEFAULT_PRODUCTS
    assert DEFAULT_PARCELS
