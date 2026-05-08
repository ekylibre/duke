"""Inspect the NER training corpus.

Usage::

    uv run python -m duke.cli.corpus_stats
    uv run python -m duke.cli.corpus_stats --corpus tests/fixtures/golden_phrases.yaml

Prints phrase count, intent distribution, entity-label distribution, span
alignment health, and a few sample misalignments — the warning signs to fix
before running `duke.cli.train_ner`. Pure read, never mutates the corpus.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import spacy
import structlog

from duke.nlu.training.converter import (
    AnnotatedExample,
    examples_to_spacy,
    load_annotated_corpus,
)

log = structlog.get_logger(__name__)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="duke.cli.corpus_stats")
    parser.add_argument(
        "--corpus",
        action="append",
        default=[],
        help="Path to an annotated YAML corpus (repeatable). "
        "Defaults to tests/fixtures/golden_phrases.yaml.",
    )
    parser.add_argument(
        "--show-misaligned",
        type=int,
        default=5,
        help="Print up to N misaligned spans (default 5; 0 disables).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    paths = [Path(p) for p in args.corpus] or [
        Path("tests/fixtures/golden_phrases.yaml")
    ]

    examples: list[AnnotatedExample] = []
    for path in paths:
        if not path.exists():
            print(f"error: corpus not found: {path}", file=sys.stderr)
            return 1
        loaded = load_annotated_corpus(path)
        examples.extend(loaded)
        print(f"loaded {len(loaded):>4d} phrases from {path}")

    if not examples:
        print("error: corpus is empty", file=sys.stderr)
        return 1

    print()
    print(f"total phrases:    {len(examples)}")
    print(
        f"with entities:    {sum(1 for e in examples if e.entities)} "
        f"({sum(len(e.entities) for e in examples)} spans)"
    )
    print(f"without entities: {sum(1 for e in examples if not e.entities)}")

    intent_counts = Counter(e.intent for e in examples if e.intent)
    print()
    print("intents:")
    for intent, count in sorted(intent_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4d}  {intent}")

    label_counts = Counter(s.label for e in examples for s in e.entities)
    print()
    print("entity labels:")
    for label, count in sorted(label_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4d}  {label}")

    # Token alignment is the silent killer of NER training: a span that
    # crosses a token boundary gets dropped at training time without
    # surfacing here would be too late to fix. Run the same conversion
    # the training CLI uses, then inspect any drops.
    nlp = spacy.blank("fr")
    spacy_examples = examples_to_spacy(nlp, examples)
    aligned_spans = sum(len(ex.reference.ents) for ex in spacy_examples)
    expected_spans = sum(len(e.entities) for e in examples)
    dropped = expected_spans - aligned_spans

    print()
    print(f"span alignment: {aligned_spans}/{expected_spans} kept", end="")
    if dropped:
        print(f" — {dropped} dropped (token-boundary mismatch)")
        if args.show_misaligned:
            _print_misaligned(examples, nlp, limit=args.show_misaligned)
    else:
        print(" (perfect)")

    return 0


def _print_misaligned(
    examples: list[AnnotatedExample], nlp, limit: int
) -> None:
    """Surface the first `limit` spans that don't align cleanly to tokens.

    Useful when the user adds a phrase like "2L de blé" and the span
    "2L" doesn't match because spaCy tokenizes it differently. The
    fix is usually to rephrase or add a space ("2 L de blé").
    """
    shown = 0
    print()
    print("misaligned span samples:")
    for ex in examples:
        if shown >= limit:
            break
        doc = nlp.make_doc(ex.text)
        for entity in ex.entities:
            if shown >= limit:
                break
            span = doc.char_span(entity.start, entity.end, alignment_mode="contract")
            if span is None:
                literal = ex.text[entity.start : entity.end]
                print(f"  • {ex.text!r}")
                print(f"    {entity.label} @ [{entity.start}:{entity.end}] = {literal!r}")
                shown += 1


if __name__ == "__main__":
    raise SystemExit(main())
