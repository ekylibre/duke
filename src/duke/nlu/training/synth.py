"""Synthesize annotated NER training data from templates x lexicon.

The golden corpus alone (~24 phrases) is too small to train a NER. This module
generates additional French agricultural phrases by sampling from a small set
of templates and slot pools (procedures, products, parcels, quantities, dates).

Determinism: a `SynthConfig.seed` controls the random sampler so the same call
produces byte-identical output across runs (CI-friendly).

Quality caveats: synthetic data is regular by construction — it teaches the
NER to recognize patterns more than vocabulary. Real user feedback (extending
`golden_phrases.yaml`) remains the most valuable signal.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from duke.integration.ekylibre.lexicon_repo import Lexicon
from duke.nlu.entity_ruler import (
    LABEL_PARCEL,
    LABEL_PROCEDURE,
    LABEL_PRODUCT,
    LABEL_QUANTITY,
    LABEL_TOOL,
    LABEL_WORKER,
)
from duke.nlu.training.converter import AnnotatedExample, EntitySpan

# Verb / noun pairs for the most common record_intervention procedures.
# Both forms are labelled DUKE_PROCEDURE — the NER learns morphological
# variants from the same class.
DEFAULT_PROCEDURE_PAIRS: list[tuple[str, str]] = [
    ("pulvérisé", "pulvérisation"),
    ("traité", "traitement"),
    ("semé", "semis"),
    ("labouré", "labour"),
    ("récolté", "récolte"),
    ("fauché", "fauchage"),
    ("fertilisé", "fertilisation"),
    ("vendangé", "vendange"),
    ("épandu", "épandage"),
]

DEFAULT_PRODUCTS: list[str] = [
    "Karaté Zeon",
    "Bouillie bordelaise",
    "Roundup",
    "Pulvébois",
    "blé",
    "orge",
    "fioul",
    "glyphosate",
    "azote",
    "soufre",
    "Decis Protech",
    "Movento",
]

DEFAULT_PARCELS: list[str] = [
    "Bel Air",
    "Vigne du Bas",
    "Pré du Moulin",
    "Cantemerle",
    "Les Coteaux",
    "Le Grand Pré",
    "La Croix",
    "Champ Long",
    "Plaine du Nord",
    "Le Clos",
    "Les Vignes Hautes",
    "Coteau Saint-Martin",
]

DEFAULT_QUANTITIES: list[str] = [
    "2L",
    "5L",
    "10 litres",
    "1 litre",
    "200kg",
    "150 kg",
    "20 kilos",
    "500kg",
    "3 hectares",
    "1 ha",
]

DEFAULT_DURATIONS: list[str] = [
    "pendant 1h30",
    "pendant 2h",
    "pendant 3 heures",
    "pendant 30 minutes",
]

DEFAULT_DATES: list[str] = [
    "ce matin",
    "hier",
    "hier après-midi",
    "lundi",
    "mardi",
    "il y a une semaine",
    "aujourd'hui",
    "ce week-end",
    "à 14h30",
]

# Common French first names — annotated as DUKE_WORKER. Names alone aren't
# obviously workers without context; the templates below place them as the
# subject of an action verb so the model learns "<Name> a <verb>" as a
# worker-attribution pattern.
DEFAULT_WORKERS: list[str] = [
    "Antoine",
    "David",
    "Jean-Michel",
    "Marie",
    "Philippe",
    "Sophie",
    "Thomas",
    "Lucas",
    "Camille",
    "Pierre",
    "Patrick",
    "Margaret",
]

# Common agricultural tools / equipment. The Procedo registry distinguishes
# these by `filter` (motorized_vehicle, equipment, …) but for the NER all
# fall under DUKE_TOOL.
DEFAULT_TOOLS: list[str] = [
    "le tracteur",
    "le sécateur",
    "la charrue",
    "l'andaineur",
    "le pulvérisateur",
    "la herse",
    "le gyrobroyeur",
    "la moissonneuse",
    "la remorque",
    "le motoculteur",
]


@dataclass(frozen=True)
class _Template:
    """A pattern with `{slot}` placeholders and a label per slot."""

    pattern: str
    intent: str
    slot_labels: dict[str, str]


# Slot keys used in templates:
#   verb     -> verbal form of a procedure  (DUKE_PROCEDURE)
#   noun     -> nominal form of a procedure (DUKE_PROCEDURE)
#   product  -> product name                (DUKE_PRODUCT)
#   parcel   -> parcel name                 (DUKE_PARCEL)
#   qty      -> quantity expression         (DUKE_QUANTITY)
#   duration -> duration expression         (no entity label by default)
#   date     -> temporal expression         (no entity label — handled by temporal.py)
RECORD_TEMPLATES: list[_Template] = [
    _Template(
        "j'ai {verb} {qty} de {product} sur {parcel} {date}",
        "record_intervention",
        {
            "verb": LABEL_PROCEDURE,
            "qty": LABEL_QUANTITY,
            "product": LABEL_PRODUCT,
            "parcel": LABEL_PARCEL,
        },
    ),
    _Template(
        "{noun} de {product} sur {parcel} {date}",
        "record_intervention",
        {"noun": LABEL_PROCEDURE, "product": LABEL_PRODUCT, "parcel": LABEL_PARCEL},
    ),
    _Template(
        "j'ai {verb} {parcel} {date} {duration}",
        "record_intervention",
        {"verb": LABEL_PROCEDURE, "parcel": LABEL_PARCEL},
    ),
    _Template(
        "{noun} sur {parcel} {date}",
        "record_intervention",
        {"noun": LABEL_PROCEDURE, "parcel": LABEL_PARCEL},
    ),
    _Template(
        "enregistre une {noun} sur {parcel} {date}",
        "record_intervention",
        {"noun": LABEL_PROCEDURE, "parcel": LABEL_PARCEL},
    ),
    _Template(
        "saisie d'une intervention de {noun} {date}",
        "record_intervention",
        {"noun": LABEL_PROCEDURE},
    ),
    _Template(
        "{noun} de {product} {duration}",
        "record_intervention",
        {"noun": LABEL_PROCEDURE, "product": LABEL_PRODUCT},
    ),
    # Worker-attribution patterns: "<Worker> a <verb> sur <parcel>".
    _Template(
        "{worker} a {verb} sur {parcel} {date}",
        "record_intervention",
        {"worker": LABEL_WORKER, "verb": LABEL_PROCEDURE, "parcel": LABEL_PARCEL},
    ),
    _Template(
        "{noun} par {worker} sur {parcel}",
        "record_intervention",
        {"noun": LABEL_PROCEDURE, "worker": LABEL_WORKER, "parcel": LABEL_PARCEL},
    ),
    # Tool-attribution patterns: "<verb> avec <tool>".
    _Template(
        "j'ai {verb} {parcel} avec {tool} {date}",
        "record_intervention",
        {"verb": LABEL_PROCEDURE, "parcel": LABEL_PARCEL, "tool": LABEL_TOOL},
    ),
    _Template(
        "{noun} sur {parcel} par {worker} avec {tool}",
        "record_intervention",
        {
            "noun": LABEL_PROCEDURE,
            "parcel": LABEL_PARCEL,
            "worker": LABEL_WORKER,
            "tool": LABEL_TOOL,
        },
    ),
]

QA_STOCK_TEMPLATES: list[_Template] = [
    _Template(
        "combien de {product} me reste-t-il ?",
        "qa_stock",
        {"product": LABEL_PRODUCT},
    ),
    _Template(
        "stock actuel de {product}",
        "qa_stock",
        {"product": LABEL_PRODUCT},
    ),
    _Template(
        "il me reste combien de {product} ?",
        "qa_stock",
        {"product": LABEL_PRODUCT},
    ),
    _Template(
        "quantité disponible de {product}",
        "qa_stock",
        {"product": LABEL_PRODUCT},
    ),
]

QA_HISTORY_TEMPLATES: list[_Template] = [
    _Template(
        "historique des interventions sur {parcel}",
        "qa_history",
        {"parcel": LABEL_PARCEL},
    ),
    _Template(
        "qu'ai-je fait sur {parcel} {date} ?",
        "qa_history",
        {"parcel": LABEL_PARCEL},
    ),
    _Template(
        "montre-moi les dernières {noun}",
        "qa_history",
        {"noun": LABEL_PROCEDURE},
    ),
]

ALL_TEMPLATES: list[_Template] = RECORD_TEMPLATES + QA_STOCK_TEMPLATES + QA_HISTORY_TEMPLATES


@dataclass
class SynthConfig:
    seed: int = 42
    n_examples: int = 500
    procedure_pairs: list[tuple[str, str]] = field(
        default_factory=lambda: list(DEFAULT_PROCEDURE_PAIRS)
    )
    products: list[str] = field(default_factory=lambda: list(DEFAULT_PRODUCTS))
    parcels: list[str] = field(default_factory=lambda: list(DEFAULT_PARCELS))
    quantities: list[str] = field(default_factory=lambda: list(DEFAULT_QUANTITIES))
    durations: list[str] = field(default_factory=lambda: list(DEFAULT_DURATIONS))
    dates: list[str] = field(default_factory=lambda: list(DEFAULT_DATES))
    workers: list[str] = field(default_factory=lambda: list(DEFAULT_WORKERS))
    tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    templates: list[_Template] = field(default_factory=lambda: list(ALL_TEMPLATES))

    @classmethod
    def from_lexicon(cls, lexicon: Lexicon | None, **overrides) -> SynthConfig:
        """Build a config preferring the live lexicon for product/procedure names."""
        cfg = cls(**overrides)
        if lexicon is None:
            return cfg
        if lexicon.products:
            cfg.products = [p.name for p in lexicon.products if p.name]
        # Procedure pairs need both verbal and nominal forms; the lexicon only
        # carries labels (nominal). Keep the curated pairs for now and seed
        # extra nominal-only forms from the lexicon for the {noun} slot.
        return cfg


def synthesize_corpus(config: SynthConfig | None = None) -> list[AnnotatedExample]:
    cfg = config or SynthConfig()
    rng = random.Random(cfg.seed)
    out: list[AnnotatedExample] = []
    for _ in range(cfg.n_examples):
        template = rng.choice(cfg.templates)
        slots = _pick_slots(rng, template, cfg)
        out.append(_render(template, slots))
    return out


def _pick_slots(
    rng: random.Random, template: _Template, cfg: SynthConfig
) -> dict[str, str]:
    needed = _placeholders(template.pattern)
    verb, noun = rng.choice(cfg.procedure_pairs)
    pool: dict[str, str] = {
        "verb": verb,
        "noun": noun,
        "product": rng.choice(cfg.products),
        "parcel": rng.choice(cfg.parcels),
        "qty": rng.choice(cfg.quantities),
        "duration": rng.choice(cfg.durations),
        "date": rng.choice(cfg.dates),
        "worker": rng.choice(cfg.workers),
        "tool": rng.choice(cfg.tools),
    }
    return {slot: pool[slot] for slot in needed}


def _placeholders(pattern: str) -> list[str]:
    return re.findall(r"\{(\w+)\}", pattern)


_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _render(template: _Template, slots: dict[str, str]) -> AnnotatedExample:
    parts: list[str] = []
    entities: list[EntitySpan] = []
    pos = 0
    for match in _PLACEHOLDER_RE.finditer(template.pattern):
        parts.append(template.pattern[pos : match.start()])
        slot_name = match.group(1)
        slot_value = slots[slot_name]
        out_pos = sum(len(p) for p in parts)
        parts.append(slot_value)
        label = template.slot_labels.get(slot_name)
        if label:
            entities.append(EntitySpan(label=label, start=out_pos, end=out_pos + len(slot_value)))
        pos = match.end()
    parts.append(template.pattern[pos:])
    text = "".join(parts)
    return AnnotatedExample(text=text, intent=template.intent, entities=entities)


def merge_corpora(*corpora: Iterable[AnnotatedExample]) -> list[AnnotatedExample]:
    merged: list[AnnotatedExample] = []
    for corpus in corpora:
        merged.extend(corpus)
    return merged
