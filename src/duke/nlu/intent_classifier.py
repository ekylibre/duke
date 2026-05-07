from __future__ import annotations

import re

from duke.domain.intent import Intent, IntentResult

_RECORD_PATTERNS = [
    re.compile(
        r"\b(j'?ai|je\s+viens\s+de|j'?aimerais|saisi[sr]?|enregistre[rz]?|cree[rz]?|ajoute[rz]?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(pulverise[rz]?|seme[rz]?|recolte[rz]?|traite[rz]?|epande[rz]?|laboure[rz]?|fauche[rz]?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(intervention|saisie)\b", re.IGNORECASE),
]

_QA_STOCK_PATTERNS = [
    re.compile(r"\b(combien|quantit[eé]|stock|reste|restant|disponible)\b", re.IGNORECASE),
    re.compile(r"\bme\s+reste[\s\-]+t[\s\-]?il\b", re.IGNORECASE),
]

_QA_HISTORY_PATTERNS = [
    re.compile(
        r"\b(quelles?|liste|historique|derni[eè]res?|aper[cç]u|montre[\s\-]?moi)\b", re.IGNORECASE
    ),
    re.compile(r"\b(qu'?est[\s\-]ce\s+que\s+j'?ai|qu'?ai[\s\-]?je)\b", re.IGNORECASE),
]

_OUT_OF_SCOPE_PATTERNS = [
    re.compile(
        r"\b(imprime[rz]?|impression|grand[\s\-]+livre|edite[rz]?|edition|export[a-z]*)\b",
        re.IGNORECASE,
    ),
]


def classify_intent(text: str) -> IntentResult:
    """Rule-based French intent classifier.

    Returns the strongest matching intent with a heuristic confidence score.
    The orchestrator may override with the LLM-detected intent if confidence is low.
    """
    if any(p.search(text) for p in _OUT_OF_SCOPE_PATTERNS):
        return IntentResult(intent=Intent.OUT_OF_SCOPE, confidence=0.85)

    record_hits = sum(1 for p in _RECORD_PATTERNS if p.search(text))
    qa_stock_hits = sum(1 for p in _QA_STOCK_PATTERNS if p.search(text))
    qa_history_hits = sum(1 for p in _QA_HISTORY_PATTERNS if p.search(text))

    scores = {
        Intent.RECORD_INTERVENTION: record_hits,
        Intent.QA_STOCK: qa_stock_hits,
        Intent.QA_HISTORY: qa_history_hits,
    }
    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] == 0:
        return IntentResult(intent=Intent.UNKNOWN, confidence=0.0)

    confidence = min(0.5 + 0.2 * best[1], 0.95)
    return IntentResult(intent=best[0], confidence=confidence)
