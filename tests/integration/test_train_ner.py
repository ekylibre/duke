"""End-to-end training smoke test.

Skipped by default — training even a small NER takes seconds and depends on
spaCy's pinned thinc backend, which is heavier than the rest of the unit
suite. Opt in via:

    RUN_NER_TRAINING=1 uv run pytest -m ner_training

The test forces the `blank-fr` fallback (via a non-existent base model) so
no large French model needs to be installed in CI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import spacy
import yaml

pytestmark = [pytest.mark.ner_training, pytest.mark.integration]

if os.environ.get("RUN_NER_TRAINING") != "1":
    pytest.skip(
        "Set RUN_NER_TRAINING=1 to run NER training smoke tests.",
        allow_module_level=True,
    )

from duke.cli.train_ner import main as train_main  # noqa: E402


def _write_corpus(path: Path) -> None:
    data = [
        {
            "text": "j'ai pulvérisé 2L de Karaté Zeon sur Bel Air ce matin",
            "intent": "record_intervention",
            "entities": [
                {"label": "DUKE_PROCEDURE", "span": "pulvérisé"},
                {"label": "DUKE_QUANTITY", "span": "2L"},
                {"label": "DUKE_PRODUCT", "span": "Karaté Zeon"},
                {"label": "DUKE_PARCEL", "span": "Bel Air"},
            ],
        },
        {
            "text": "j'ai semé 200kg de blé sur Le Clos hier",
            "intent": "record_intervention",
            "entities": [
                {"label": "DUKE_PROCEDURE", "span": "semé"},
                {"label": "DUKE_QUANTITY", "span": "200kg"},
                {"label": "DUKE_PRODUCT", "span": "blé"},
                {"label": "DUKE_PARCEL", "span": "Le Clos"},
            ],
        },
        {
            "text": "stock actuel de Roundup",
            "intent": "qa_stock",
            "entities": [{"label": "DUKE_PRODUCT", "span": "Roundup"}],
        },
    ]
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_training_cli_produces_loadable_model(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.yaml"
    _write_corpus(corpus)

    output = tmp_path / "model"

    rc = train_main(
        [
            "--base-model",
            "duke-test-no-such-model",  # forces blank-fr fallback
            "--corpus",
            str(corpus),
            "--n-synth",
            "30",
            "--n-iter",
            "3",
            "--seed",
            "0",
            "--eval-ratio",
            "0.2",
            "--output",
            str(output),
        ]
    )
    assert rc == 0

    # Model should load back and expose the Duke labels on its NER pipe.
    nlp = spacy.load(output)
    assert "ner" in nlp.pipe_names
    labels = nlp.get_pipe("ner").labels
    expected = {"DUKE_PRODUCT", "DUKE_PROCEDURE", "DUKE_PARCEL", "DUKE_QUANTITY"}
    assert expected.issubset(set(labels))

    metrics = json.loads((output / "metrics.json").read_text())
    for key in ("ents_p", "ents_r", "ents_f", "ents_per_type"):
        assert key in metrics
