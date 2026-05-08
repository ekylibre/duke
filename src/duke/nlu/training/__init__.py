"""NER training utilities: corpus loading, span resolution, synthesis."""

from duke.nlu.training.converter import (
    AnnotatedExample,
    EntitySpan,
    examples_to_spacy,
    load_annotated_corpus,
    resolve_spans,
)
from duke.nlu.training.synth import SynthConfig, synthesize_corpus

__all__ = [
    "AnnotatedExample",
    "EntitySpan",
    "SynthConfig",
    "examples_to_spacy",
    "load_annotated_corpus",
    "resolve_spans",
    "synthesize_corpus",
]
