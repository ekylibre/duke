"""LexiconRepository.

The Ekylibre `lexicon` Postgres schema holds master data shared across tenants
(product variants, Procedo procedures, units, ...). Duke caches a normalized
view in memory and exposes fuzzy-match lookups for the NLU pipeline.

Iteration 2 ships:
- in-memory lexicon (`InMemoryLexiconRepository`) seeded for tests and dev,
- a `PostgresLexiconRepository` skeleton wired to `EkylibreReadDb` whose SQL
  queries will be filled in once the actual `lexicon` schema is inspected on
  a dev instance (tracked as a follow-up).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

import asyncpg
import structlog
from rapidfuzz import fuzz, process

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ProductEntry:
    id: int
    name: str
    nature: str | None = None
    unit: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProcedureEntry:
    name: str
    label: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnitEntry:
    name: str
    label: str
    aliases: tuple[str, ...] = ()


@dataclass
class Lexicon:
    products: list[ProductEntry] = field(default_factory=list)
    procedures: list[ProcedureEntry] = field(default_factory=list)
    units: list[UnitEntry] = field(default_factory=list)


@dataclass(frozen=True)
class Match:
    raw: str
    label: str
    score: float
    payload: object


class LexiconRepository(Protocol):
    async def load(self) -> Lexicon: ...
    def find_product(self, query: str, limit: int = 3) -> list[Match]: ...
    def find_procedure(self, query: str, limit: int = 3) -> list[Match]: ...
    def find_unit(self, query: str, limit: int = 1) -> list[Match]: ...
    @property
    def lexicon(self) -> Lexicon: ...


class _BaseLexiconRepository:
    def __init__(self) -> None:
        self._lexicon = Lexicon()
        self._product_index: list[tuple[str, ProductEntry]] = []
        self._procedure_index: list[tuple[str, ProcedureEntry]] = []
        self._unit_index: list[tuple[str, UnitEntry]] = []

    @property
    def lexicon(self) -> Lexicon:
        return self._lexicon

    def _index(self, lexicon: Lexicon) -> None:
        self._lexicon = lexicon
        self._product_index = [
            (alias, p) for p in lexicon.products for alias in (p.name, *p.aliases)
        ]
        self._procedure_index = [
            (alias, p) for p in lexicon.procedures for alias in (p.label, p.name, *p.aliases)
        ]
        self._unit_index = [
            (alias, u) for u in lexicon.units for alias in (u.label, u.name, *u.aliases)
        ]

    def find_product(self, query: str, limit: int = 3) -> list[Match]:
        return _fuzzy(query, self._product_index, "product", limit, threshold=70)

    def find_procedure(self, query: str, limit: int = 3) -> list[Match]:
        return _fuzzy(query, self._procedure_index, "procedure", limit, threshold=70)

    def find_unit(self, query: str, limit: int = 1) -> list[Match]:
        return _fuzzy(query, self._unit_index, "unit", limit, threshold=80)


def _fuzzy(
    query: str,
    index: Iterable[tuple[str, object]],
    label: str,
    limit: int,
    threshold: int,
) -> list[Match]:
    items = list(index)
    if not items:
        return []
    choices = [name for name, _ in items]
    scored = process.extract(query, choices, scorer=fuzz.WRatio, limit=limit)
    out: list[Match] = []
    seen_payloads: set[int] = set()
    for name, score, idx in scored:
        if score < threshold:
            continue
        payload = items[idx][1]
        key = id(payload)
        if key in seen_payloads:
            continue
        seen_payloads.add(key)
        out.append(Match(raw=name, label=label, score=float(score), payload=payload))
    return out


class InMemoryLexiconRepository(_BaseLexiconRepository):
    def __init__(self, lexicon: Lexicon | None = None) -> None:
        super().__init__()
        if lexicon is not None:
            self._index(lexicon)

    async def load(self) -> Lexicon:
        return self._lexicon

    def seed(self, lexicon: Lexicon) -> None:
        self._index(lexicon)


class PostgresLexiconRepository(_BaseLexiconRepository):
    """Loads from the Ekylibre `lexicon` Postgres schema.

    The exact SQL is to be finalized once the lexicon schema is mapped on a real
    Ekylibre dev instance (see ARCHITECTURE.md §10 dependency tracker).
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        super().__init__()
        self._pool = pool

    async def load(self) -> Lexicon:
        async with self._pool.acquire() as conn:
            await conn.execute("SET LOCAL search_path TO lexicon, public")
            try:
                products = await self._load_products(conn)
                procedures = await self._load_procedures(conn)
                units = await self._load_units(conn)
            except asyncpg.UndefinedTableError as exc:
                log.warning("lexicon.schema_not_ready", error=str(exc))
                products, procedures, units = [], [], []

        lexicon = Lexicon(products=products, procedures=procedures, units=units)
        self._index(lexicon)
        return lexicon

    async def _load_products(self, conn: asyncpg.Connection) -> list[ProductEntry]:
        rows = await conn.fetch("SELECT id, name FROM product_nature_variants ORDER BY name")
        return [ProductEntry(id=r["id"], name=r["name"]) for r in rows]

    async def _load_procedures(self, conn: asyncpg.Connection) -> list[ProcedureEntry]:
        rows = await conn.fetch("SELECT name, human_name FROM procedures ORDER BY human_name")
        return [ProcedureEntry(name=r["name"], label=r["human_name"]) for r in rows]

    async def _load_units(self, conn: asyncpg.Connection) -> list[UnitEntry]:
        rows = await conn.fetch("SELECT name, human_name FROM units ORDER BY human_name")
        return [UnitEntry(name=r["name"], label=r["human_name"]) for r in rows]


# Canonical procedure names below match Ekylibre's actual Procedo registry
# (validated against `Procedo.procedures.collect(&:name)` on a closeriedesterres
# tenant, 2026-05-08). The legacy placeholders (`ploughing`, `vine_spraying`)
# are kept as aliases so older drafts and LLM mistakes still canonicalize.
DEFAULT_PROCEDURES = [
    ProcedureEntry(
        name="spraying",
        label="Pulverisation",
        aliases=("pulvérisation", "pulve", "traitement phyto", "spray"),
    ),
    ProcedureEntry(
        name="vine_spraying_without_fertilizing",
        label="Pulverisation viticole",
        aliases=(
            "pulvé vigne",
            "traitement vigne",
            "pulverisation vigne",
            "vine_spraying",  # legacy alias kept for back-compat
        ),
    ),
    ProcedureEntry(
        name="sowing",
        label="Semis",
        aliases=("semer", "semence", "ensemencement"),
    ),
    ProcedureEntry(
        name="harvesting",
        label="Recolte",
        aliases=("récolte", "moisson"),
    ),
    ProcedureEntry(
        name="vine_harvesting",
        label="Vendange",
        aliases=("vendange", "vendanger"),
    ),
    ProcedureEntry(
        name="generic_tillage",
        label="Labour",
        aliases=(
            "labour",
            "labourage",
            "labourer",
            "travail du sol",
            "ploughing",  # legacy alias kept for back-compat
            "plowing",
        ),
    ),
    ProcedureEntry(
        name="fertilizing",
        label="Fertilisation",
        aliases=("epandage", "engrais", "fertiliser", "amendement"),
    ),
]


DEFAULT_UNITS = [
    UnitEntry(name="liter", label="Litre", aliases=("L", "l", "litres")),
    UnitEntry(name="kilogram", label="Kilogramme", aliases=("kg", "kilo", "kilos")),
    UnitEntry(name="hectare", label="Hectare", aliases=("ha",)),
    UnitEntry(name="hour", label="Heure", aliases=("h", "heures")),
]
