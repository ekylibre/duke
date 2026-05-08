"""Convert annotated phrase corpora to spaCy training Examples.

The on-disk format (`golden_phrases.yaml`) lets annotators reference entity
spans by literal substring (`{label, span, nth?}`) instead of hand-counted
character offsets. This module resolves those substrings to char offsets,
validates token alignment via `Doc.char_span`, and produces `Example`
objects ready for `nlp.update`.

Misaligned spans (the substring crosses a token boundary) are dropped with
a structured warning rather than failing the run — the training CLI reports
the count so the corpus can be cleaned up incrementally.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import structlog
import yaml
from spacy.language import Language
from spacy.training import Example

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class EntitySpan:
    label: str
    start: int
    end: int


@dataclass
class AnnotatedExample:
    text: str
    intent: str | None = None
    entities: list[EntitySpan] = field(default_factory=list)


def load_annotated_corpus(path: Path) -> list[AnnotatedExample]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a top-level YAML list")

    out: list[AnnotatedExample] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict) or "text" not in entry:
            raise ValueError(f"{path}[{idx}]: missing 'text' field")
        text = entry["text"]
        intent = entry.get("intent")
        entity_specs = entry.get("entities") or []
        spans = resolve_spans(text, entity_specs, source=f"{path}[{idx}]")
        out.append(AnnotatedExample(text=text, intent=intent, entities=spans))
    return out


def resolve_spans(
    text: str,
    specs: Iterable[dict[str, Any]],
    source: str = "<inline>",
) -> list[EntitySpan]:
    """Resolve `{label, span, nth?}` shorthand to concrete (start, end, label) tuples.

    Spans that contain explicit `start`/`end` are taken as-is. The shorthand
    `span` is located via `_nth_index` to support repeated substrings.
    """

    resolved: list[EntitySpan] = []
    for spec in specs:
        label = spec["label"]
        if "start" in spec and "end" in spec:
            start = int(spec["start"])
            end = int(spec["end"])
        else:
            literal = spec.get("span")
            if not literal:
                raise ValueError(f"{source}: entity needs `span` or `start`/`end`: {spec!r}")
            nth = int(spec.get("nth", 0))
            start = _nth_index(text, literal, nth)
            if start < 0:
                raise ValueError(
                    f"{source}: span {literal!r} (nth={nth}) not found in {text!r}"
                )
            end = start + len(literal)
        if start < 0 or end > len(text) or end <= start:
            raise ValueError(f"{source}: invalid span [{start}:{end}] for label {label!r}")
        resolved.append(EntitySpan(label=label, start=start, end=end))

    _check_overlap(resolved, source=source)
    return resolved


def examples_to_spacy(
    nlp: Language,
    annotated: Sequence[AnnotatedExample],
) -> list[Example]:
    """Build `Example` objects for spaCy training, dropping misaligned spans."""

    examples: list[Example] = []
    dropped = 0
    for entry in annotated:
        doc = nlp.make_doc(entry.text)
        kept: list[tuple[int, int, str]] = []
        for ent in entry.entities:
            span = doc.char_span(
                ent.start, ent.end, label=ent.label, alignment_mode="contract"
            )
            if span is None:
                dropped += 1
                log.warning(
                    "training.span_unaligned",
                    text=entry.text,
                    label=ent.label,
                    start=ent.start,
                    end=ent.end,
                )
                continue
            kept.append((span.start_char, span.end_char, ent.label))
        examples.append(Example.from_dict(doc, {"entities": kept}))

    if dropped:
        log.info(
            "training.spans_dropped",
            count=dropped,
            total=sum(len(e.entities) for e in annotated),
        )
    return examples


def _nth_index(text: str, needle: str, nth: int) -> int:
    pos = -1
    for _ in range(nth + 1):
        pos = text.find(needle, pos + 1)
        if pos == -1:
            return -1
    return pos


def _check_overlap(spans: list[EntitySpan], source: str) -> None:
    sorted_spans = sorted(spans, key=lambda s: s.start)
    for prev, curr in pairwise(sorted_spans):
        if curr.start < prev.end:
            raise ValueError(
                f"{source}: overlapping spans {prev.label}@[{prev.start}:{prev.end}] "
                f"and {curr.label}@[{curr.start}:{curr.end}] (spaCy NER forbids overlaps)"
            )
