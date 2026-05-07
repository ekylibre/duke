from __future__ import annotations

import pytest

from duke.domain.intent import Intent
from duke.nlu.intent_classifier import classify_intent


@pytest.mark.parametrize(
    "text,expected",
    [
        ("j'ai pulvérisé 2L de Karaté Zeon sur Bel Air ce matin", Intent.RECORD_INTERVENTION),
        ("j'ai semé du blé hier", Intent.RECORD_INTERVENTION),
        ("enregistre une intervention de labour", Intent.RECORD_INTERVENTION),
        ("combien de Karaté Zeon me reste-t-il ?", Intent.QA_STOCK),
        ("quel est mon stock de fioul ?", Intent.QA_STOCK),
        ("quelles parcelles ai-je traitées cette semaine ?", Intent.QA_HISTORY),
        ("liste mes interventions de mai", Intent.QA_HISTORY),
        ("imprime le grand livre", Intent.OUT_OF_SCOPE),
        ("édite l'export comptable", Intent.OUT_OF_SCOPE),
        ("bonjour", Intent.UNKNOWN),
    ],
)
def test_classify(text: str, expected: Intent) -> None:
    result = classify_intent(text)
    assert result.intent == expected, f"expected {expected} for {text!r}, got {result}"
