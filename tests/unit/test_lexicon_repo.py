from __future__ import annotations

from duke.integration.ekylibre.lexicon_repo import (
    DEFAULT_PROCEDURES,
    DEFAULT_UNITS,
    InMemoryLexiconRepository,
    Lexicon,
    ProductEntry,
)

PRODUCTS = [
    ProductEntry(id=1, name="Karaté Zeon", aliases=("Karate Zeon", "karaté zéon")),
    ProductEntry(id=2, name="Roundup"),
    ProductEntry(id=3, name="Glyphosate"),
]


def _repo() -> InMemoryLexiconRepository:
    return InMemoryLexiconRepository(
        Lexicon(products=PRODUCTS, procedures=list(DEFAULT_PROCEDURES), units=list(DEFAULT_UNITS))
    )


def test_find_product_exact() -> None:
    matches = _repo().find_product("Karaté Zeon")
    assert matches
    assert matches[0].payload.id == 1


def test_find_product_typo() -> None:
    matches = _repo().find_product("Karate Zeon")
    assert matches
    assert matches[0].payload.id == 1


def test_find_procedure_french_label() -> None:
    matches = _repo().find_procedure("pulvérisation")
    assert matches
    assert matches[0].payload.name == "spraying"


def test_find_unit_alias() -> None:
    matches = _repo().find_unit("L")
    assert matches
    assert matches[0].payload.name == "liter"


def test_low_score_filtered_out() -> None:
    matches = _repo().find_product("xyz123")
    assert matches == []
