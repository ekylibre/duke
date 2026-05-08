from __future__ import annotations

import re
import unicodedata

from duke.domain.intent import Intent, IntentResult

_RECORD_PATTERNS = [
    # Recording-intent verbs ("j'ai pulvérisé…", "enregistre…", "saisis…").
    re.compile(
        r"\b("
        r"j'?ai|je\s+viens\s+de|j'?aimerais|"
        r"saisi[sr]?|saisis|"
        r"enregistre[rz]?|cree[rz]?|ajoute[rz]?|"
        r"sauvegarde[rz]?|sauve[rz]?|retien[stz]?|donne[rz]?"
        r")\b",
        re.IGNORECASE,
    ),
    # Action verbs (past participle / infinitive / present 1st-person).
    # Wide French agricultural vocabulary — extended through field-collected
    # phrases in `tests/fixtures/golden_phrases.yaml`. New verbs go here.
    re.compile(
        r"\b("
        # Soil work
        r"laboure[rz]?|herse[rz]?|decompacte[rz]?|dechaume[rz]?|"
        r"butte[rz]?|chausse[rz]?|dechausse[rz]?|rechausse[rz]?|"
        r"terrasse[rz]?|destratifie[rz]?|"
        # Plants / vine
        r"seme[rz]?|plante[rz]?|complante[rz]?|arrache[rz]?|"
        r"ecime[rz]?|eclaircir|effeuille[rz]?|efeuille[rz]?|"
        r"epamprer?|ebourgeonner?|"
        r"palisse[rz]?|taille[rz]?|releve[rz]?|relever?|"
        r"plie[rz]?|enleve[rz]?|retire[rz]?|souleve[rz]?|"
        # Application / inputs
        r"pulverise[rz]?|traite[rz]?|fertilise[rz]?|amende[rz]?|"
        r"irrigue[rz]?|arrose[rz]?|epande[rz]?|prepare[rz]?|"
        # Harvest / output
        r"recolte[rz]?|fauche[rz]?|moissonne[rz]?|vendange[rz]?|"
        r"ensile[rz]?|presse[rz]?|cueillir?|cueille[rz]?|"
        r"ramasse[rz]?|rammasse[rz]?|"
        # Maintenance
        r"nettoie[rz]?|nettoye[rz]?|repare[rz]?|monte[rz]?|pose[rz]?|"
        r"broie[rz]?|change[rz]?|superpose[rz]?|"
        # Animal
        r"nourri[rs]?|deplace[rz]?|insemine[rz]?|trait[ez]|"
        # Other procedures
        r"bine[rz]?|defane[rz]?|desherbe[rz]?"
        r")\b",
        re.IGNORECASE,
    ),
    # Procedure nouns (broad list — the `?s` suffix accepts plurals).
    # `intervention` and `saisie` are deliberately NOT here — they live in
    # the dedicated pattern below to avoid double-counting on plurals.
    re.compile(
        r"\b("
        r"administrative|administratives|"
        r"alimentation|amendement|arrachage|arrosage|bachage|binage|"
        r"broyage|buttage|changement|chaussage|"
        r"complantation|conditionnement|continionnement|"
        r"debachage|dechaumage|dechaussage|decompactage|"
        r"defanage|deplacement|desherbage|desherbinage|desinfection|"
        r"destratification|ebourgeonnage|ecimage|efeuillage|effeuillage|"
        r"ensilage|epamprage|epandage|fauchage|fenaisons?|fertilisation|"
        r"hersage|identification|implantation|insemination|"
        r"irrigation|irriguation|"
        r"labour|labourage|manutention|moisson|montage|paillage|"
        r"palissage|paturage|pliage|plantation|pollenisation|pollinisation|"
        r"preparation|pressage|"
        r"protection|pulverisation|pulverisaiton|pulve|"
        r"ramassage|rammassage|rechaussage|recolte|retrait|"
        r"relevage|reparation|rognage|sauvegarde|semis|soulevage|"
        r"superposition|taille|tamisage|terrassement|tirage|traite|"
        r"traitement|triage|vendanges?|veterinaire|vindange"
        r")\b",
        re.IGNORECASE,
    ),
    # Multi-word procedure phrases with embedded prepositions.
    re.compile(r"\bmise\s+en\s+(place|paturage)\b", re.IGNORECASE),
    # "faire le plein" (refueling) — Procedo `fuel_up`. Standalone "plein"
    # is too noisy ("le plein soleil"), so we anchor on the verb.
    re.compile(r"\bfait\s+le\s+plein\b|\bfaire\s+le\s+plein\b", re.IGNORECASE),
    re.compile(r"\b(intervention|saisie|operation)s?\b", re.IGNORECASE),
]

_META_PATTERNS = [
    # Meta / help questions ("Comment enregistrer une intervention ?") —
    # checked before record_intervention so they aren't swept up by the
    # "enregistrer" verb match. Currently classified as `unknown` so the
    # orchestrator returns the canned help message.
    re.compile(r"^\s*comment\b", re.IGNORECASE),
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

    # Meta-questions ("Comment X ?") sit before the record/QA branches so the
    # "enregistrer" verb doesn't accidentally classify a help question as an
    # intervention recording.
    if any(p.search(normalized) for p in _META_PATTERNS):
        return IntentResult(intent=Intent.UNKNOWN, confidence=0.7)

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
