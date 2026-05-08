"""Train a custom Duke NER on top of a spaCy base model.

Usage::

    uv run python -m duke.cli.train_ner \
        --base-model fr_core_news_lg \
        --corpus tests/fixtures/golden_phrases.yaml \
        --n-synth 800 \
        --n-iter 30 \
        --output models/ner/duke-fr-v1

The output directory is a standard spaCy model and can be loaded back with
`spacy.load(<dir>)`. Configure Duke to use it via `DUKE_NER_MODEL_PATH`
(see `Settings.duke_ner_model_path`).

Training swaps the base model's NER for a fresh component restricted to the
Duke labels (`DUKE_PRODUCT`, `DUKE_PROCEDURE`, `DUKE_PARCEL`, `DUKE_QUANTITY`).
The base model's other components (tokenizer, lemmatizer, tagger…) are kept
untouched.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import structlog
from spacy.language import Language
from spacy.scorer import Scorer
from spacy.training import Example
from spacy.util import compounding, minibatch

from duke.nlu.entity_ruler import (
    LABEL_PARCEL,
    LABEL_PROCEDURE,
    LABEL_PRODUCT,
    LABEL_QUANTITY,
    LABEL_TOOL,
    LABEL_WORKER,
)
from duke.nlu.pipeline import load_nlp
from duke.nlu.training import (
    SynthConfig,
    examples_to_spacy,
    load_annotated_corpus,
    synthesize_corpus,
)
from duke.nlu.training.synth import merge_corpora

log = structlog.get_logger(__name__)

DUKE_LABELS = (
    LABEL_PRODUCT,
    LABEL_PROCEDURE,
    LABEL_PARCEL,
    LABEL_QUANTITY,
    LABEL_WORKER,
    LABEL_TOOL,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="duke.cli.train_ner")
    parser.add_argument(
        "--base-model",
        default="fr_core_news_lg",
        help="spaCy base model name (default: fr_core_news_lg)",
    )
    parser.add_argument(
        "--corpus",
        action="append",
        default=[],
        help="Path to an annotated YAML corpus (repeatable). "
        "Defaults to tests/fixtures/golden_phrases.yaml when absent.",
    )
    parser.add_argument(
        "--n-synth",
        type=int,
        default=800,
        help="Synthesized examples to add to the training set (default: 800).",
    )
    parser.add_argument(
        "--n-iter",
        type=int,
        default=30,
        help="Number of training epochs (default: 30).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for shuffles + synthesis (default: 42).",
    )
    parser.add_argument(
        "--eval-ratio",
        type=float,
        default=0.2,
        help="Fraction of the corpus held out for evaluation (default: 0.2).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Directory where the trained model is saved.",
    )
    parser.add_argument(
        "--dropout",
        type=float,
        default=0.2,
        help="Dropout applied during nlp.update (default: 0.2).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    corpus_paths = [Path(p) for p in args.corpus] or [
        Path("tests/fixtures/golden_phrases.yaml")
    ]
    log.info("train_ner.start", base_model=args.base_model, corpus=[str(p) for p in corpus_paths])

    # Load + synthesize annotations
    annotated = []
    for path in corpus_paths:
        annotated.extend(load_annotated_corpus(path))
    synth_cfg = SynthConfig(seed=args.seed, n_examples=args.n_synth)
    synth = synthesize_corpus(synth_cfg)
    all_annotated = merge_corpora(annotated, synth)
    log.info(
        "train_ner.corpus",
        golden=len(annotated),
        synth=len(synth),
        total=len(all_annotated),
    )

    # Build the base nlp and replace its NER with a fresh Duke-only component.
    # `load_nlp` falls back to `spacy.blank("fr")` if the model isn't installed —
    # training proceeds (NER is built from scratch anyway) but lemmatizer / tagger
    # won't be available downstream. Document this in CI logs.
    nlp = load_nlp(args.base_model)
    # Drop pipes Duke doesn't use. The lemmatizer in particular pulls in
    # `spacy-lookups-data` at initialize time (E955), and we have no use
    # for parser / morphologizer / attribute_ruler at runtime — we only
    # need the tokenizer (always present) and our fresh NER. Removing
    # them shaves ~150 MB from the saved pipeline too.
    unused_pipes = ("parser", "tagger", "morphologizer", "attribute_ruler", "lemmatizer")
    for pipe_name in unused_pipes:
        if pipe_name in nlp.pipe_names:
            nlp.remove_pipe(pipe_name)
    if "ner" in nlp.pipe_names:
        nlp.remove_pipe("ner")
    ner = nlp.add_pipe("ner", last=True)
    for label in DUKE_LABELS:
        ner.add_label(label)

    examples = examples_to_spacy(nlp, all_annotated)
    rng = random.Random(args.seed)
    rng.shuffle(examples)

    cutoff = int(len(examples) * (1.0 - args.eval_ratio))
    train_ex = examples[:cutoff]
    eval_ex = examples[cutoff:]
    log.info("train_ner.split", train=len(train_ex), eval=len(eval_ex))

    if not train_ex:
        log.error("train_ner.empty_train_set")
        return 1

    # Initialize the fresh NER component using only the training fold.
    optimizer = nlp.initialize(get_examples=lambda: train_ex)

    with nlp.select_pipes(enable=["ner"]):
        for itn in range(args.n_iter):
            rng.shuffle(train_ex)
            losses: dict[str, float] = {}
            batches = minibatch(train_ex, size=compounding(4.0, 32.0, 1.001))
            for batch in batches:
                nlp.update(batch, drop=args.dropout, sgd=optimizer, losses=losses)
            if itn % 5 == 0 or itn == args.n_iter - 1:
                log.info("train_ner.epoch", itn=itn, losses=losses)

    metrics = _evaluate(nlp, eval_ex)
    log.info("train_ner.metrics", **{k: v for k, v in metrics.items() if k != "ents_per_type"})

    args.output.mkdir(parents=True, exist_ok=True)
    nlp.to_disk(args.output)
    (args.output / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True))
    log.info("train_ner.done", path=str(args.output))
    return 0


def _evaluate(nlp: Language, eval_ex: list[Example]) -> dict:
    if not eval_ex:
        return {"ents_p": None, "ents_r": None, "ents_f": None, "ents_per_type": {}}
    rescored: list[Example] = []
    for ex in eval_ex:
        pred = nlp(ex.reference.text)
        rescored.append(Example(pred, ex.reference))
    scorer = Scorer()
    scores = scorer.score(rescored)
    return {
        "ents_p": scores.get("ents_p"),
        "ents_r": scores.get("ents_r"),
        "ents_f": scores.get("ents_f"),
        "ents_per_type": scores.get("ents_per_type") or {},
    }


if __name__ == "__main__":
    raise SystemExit(main())
