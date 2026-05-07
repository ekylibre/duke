from __future__ import annotations

import re
import unicodedata

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
    re.compile(
        r"\b(pulverisation|semis|recolte|labour|labourage|fauchage|"
        r"fertilisation|epandage|vendange|moisson|traitement)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(intervention|saisie)\b", re.IGNORECASE),
]

_QA_STOCK_PATTERNS = [
    re.compile(r"\b(combien|quantite|stock|reste|restant|disponible)\b", re.IGNORECASE),
    re.compile(r"\bme\s+reste[\s\-]+t[\s\-]?il\b", re.IGNORECASE),
]

_QA_HISTORY_PATTERNS = [
    re.compile(
        r"\b(quelles?|liste|historique|dernieres?|apercu|montre[\s\-]?moi)\b", re.IGNORECASE
    ),
    re.compile(r"\b(qu'?est[\s\-]ce\s+que\s+j'?ai|qu'?ai[\s\-]?je)\b", re.IGNORECASE),
]

_OUT_OF_SCOPE_PATTERNS = [
    re.compile(
        r"\b(imprime[rz]?|impression|grand[\s\-]+livre|edite[rz]?|edition|"
        r"export[a-z]*|genere[rz]?|generation)\b",
        re.IGNORECASE,
    ),
]


def _strip_accents(text: str) -> str:
    """Remove French accents to normalize regex matching."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _count_matches(patterns: list[re.Pattern[str]], text: str) -> int:
    """Sum of all match occurrences across all patterns."""
    return sum(len(p.findall(text)) for p in patterns)


def classify_intent(text: str) -> IntentResult:
    """Rule-based French intent classifier.

    Returns the strongest matching intent with a heuristic confidence score.
    The orchestrator may override with the LLM-detected intent if confidence is low.
    """
    normalized = _strip_accents(text)

    if any(p.search(normalized) for p in _OUT_OF_SCOPE_PATTERNS):
        return IntentResult(intent=Intent.OUT_OF_SCOPE, confidence=0.85)

    record_hits = _count_matches(_RECORD_PATTERNS, normalized)
    qa_stock_hits = _count_matches(_QA_STOCK_PATTERNS, normalized)
    qa_history_hits = _count_matches(_QA_HISTORY_PATTERNS, normalized)

    # Q&A intents take precedence over record on ties: questions are more specific.
    qa_max = max(qa_stock_hits, qa_history_hits)
    if qa_max > 0 and qa_max >= record_hits:
        if qa_stock_hits > qa_history_hits:
            return IntentResult(
                intent=Intent.QA_STOCK, confidence=min(0.5 + 0.2 * qa_stock_hits, 0.95)
            )
        return IntentResult(
            intent=Intent.QA_HISTORY, confidence=min(0.5 + 0.2 * qa_history_hits, 0.95)
        )
    if record_hits > 0:
        return IntentResult(
            intent=Intent.RECORD_INTERVENTION, confidence=min(0.5 + 0.2 * record_hits, 0.95)
        )

    return IntentResult(intent=Intent.UNKNOWN, confidence=0.0)
