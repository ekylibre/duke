"""Intent accuracy gate against the golden NLU corpus.

CI fails if the rule-based classifier scores below MIN_ACCURACY on the
hand-curated phrases in tests/fixtures/golden_phrases.yaml. Extend the
corpus with real user feedback rather than tuning the classifier to fit
the existing samples.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

from duke.domain.intent import Intent
from duke.nlu.intent_classifier import classify_intent

MIN_ACCURACY = 0.85
GOLDEN_PATH = Path(__file__).parent.parent / "fixtures" / "golden_phrases.yaml"


def _load_corpus() -> list[dict[str, str]]:
    raw = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("golden_phrases.yaml must be a top-level list")
    return raw


def test_golden_corpus_intent_accuracy() -> None:
    corpus = _load_corpus()
    if len(corpus) < 10:
        pytest.skip("corpus too small")

    confusions: Counter[tuple[str, str]] = Counter()
    correct = 0

    for entry in corpus:
        expected = Intent(entry["intent"])
        predicted = classify_intent(entry["text"]).intent
        if predicted == expected:
            correct += 1
        else:
            confusions[(expected.value, predicted.value)] += 1

    accuracy = correct / len(corpus)

    assert accuracy >= MIN_ACCURACY, (
        f"intent accuracy={accuracy:.2%} (target>={MIN_ACCURACY:.0%}). "
        f"Confusions: {dict(confusions)}"
    )


@pytest.mark.parametrize("entry", _load_corpus())
def test_golden_phrase_classified(entry: dict[str, str]) -> None:
    """One test per phrase so CI surfaces individual misclassifications.

    Marked xfail-tolerant via the corpus-level accuracy gate above; this
    parametric test fails loudly so reviewers see exactly which sentence
    needs attention.
    """
    expected = Intent(entry["intent"])
    predicted = classify_intent(entry["text"]).intent
    assert predicted == expected, (
        f"text={entry['text']!r} expected={expected.value} got={predicted.value}"
    )
