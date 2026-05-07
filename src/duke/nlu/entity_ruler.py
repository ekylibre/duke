"""Build spaCy EntityRuler patterns from the lexicon and per-tenant data.

We use phrase patterns (case-insensitive) for product, procedure and parcel names,
and a token pattern for quantity expressions like "2 L" or "10 kg".
"""

from __future__ import annotations

from collections.abc import Iterable

from duke.integration.ekylibre.lexicon_repo import Lexicon

LABEL_PRODUCT = "DUKE_PRODUCT"
LABEL_PROCEDURE = "DUKE_PROCEDURE"
LABEL_PARCEL = "DUKE_PARCEL"
LABEL_QUANTITY = "DUKE_QUANTITY"


def patterns_from_lexicon(lexicon: Lexicon, parcel_names: Iterable[str] = ()) -> list[dict]:
    patterns: list[dict] = []

    for product in lexicon.products:
        for alias in {product.name, *product.aliases}:
            if alias:
                patterns.append({"label": LABEL_PRODUCT, "pattern": alias, "id": str(product.id)})

    for proc in lexicon.procedures:
        for alias in {proc.label, proc.name, *proc.aliases}:
            if alias:
                patterns.append({"label": LABEL_PROCEDURE, "pattern": alias, "id": proc.name})

    for name in parcel_names:
        if name:
            patterns.append({"label": LABEL_PARCEL, "pattern": name})

    unit_choices = [
        "l",
        "litre",
        "litres",
        "kg",
        "kilo",
        "kilos",
        "kilogramme",
        "kilogrammes",
        "ha",
        "hectare",
        "hectares",
    ]
    patterns.append(
        {
            "label": LABEL_QUANTITY,
            "pattern": [{"LIKE_NUM": True}, {"LOWER": {"IN": unit_choices}}],
        }
    )
    patterns.append(
        {
            "label": LABEL_QUANTITY,
            "pattern": [{"TEXT": {"REGEX": r"^\d+([.,]\d+)?(L|l|kg|ha)$"}}],
        }
    )

    return patterns
