# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder

# Build-time toggle for the server-side Whisper backend. Pulls
# faster-whisper + ctranslate2 + onnxruntime (~250 MB) into the venv —
# off by default to keep the image slim. Flip with `--build-arg INSTALL_STT=true`
# (or via the `INSTALL_STT` env var picked up by docker-compose).
# Runtime activation also requires `ENABLE_SERVER_STT=true`.
ARG INSTALL_STT=false

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app
# pyproject.toml references both LICENSE and README.md (license file +
# readme), so they must be present at build time even though they are
# not used at runtime.
COPY pyproject.toml uv.lock LICENSE README.md ./
COPY src ./src

RUN if [ "$INSTALL_STT" = "true" ]; then \
        uv sync --no-dev --frozen --extra stt; \
    else \
        uv sync --no-dev --frozen; \
    fi


# Trainer stage: download fr_core_news_lg and run the Duke NER training CLI
# on the curated corpus + synthesized examples. The output `/tmp/duke-ner`
# is a self-contained spaCy model directory copied verbatim into runtime.
# Layered separately so:
#   - the heavy `spacy download` step caches between builds (BuildKit reuses
#     it when the venv layer hasn't changed),
#   - corpus-only edits trigger a re-train without rebuilding the venv.
FROM python:3.12-slim AS trainer

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src ./src

# `spacy download` shells out to a package installer to fetch the model
# wheel — the slim base image's `python` doesn't ship pip, and the venv
# we copied from the builder is uv-managed (no pip either). Bring uv
# into this stage so spaCy can find a package installer at runtime.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

RUN python -m spacy download fr_core_news_lg

# Corpus is the input most likely to change between releases. Keeping it
# as a late COPY means dependency / spaCy layers stay cached when only the
# corpus is edited.
COPY tests/fixtures ./tests/fixtures

RUN python -m duke.cli.train_ner \
        --base-model fr_core_news_lg \
        --corpus tests/fixtures/golden_phrases.yaml \
        --n-synth 800 \
        --n-iter 30 \
        --output /opt/duke-ner


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    DUKE_NER_MODEL_PATH=/app/models/duke-ner

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system duke \
    && useradd --system --gid duke --create-home --home-dir /home/duke duke \
    && install -d -o duke -g duke /home/duke/.cache/huggingface

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
# Trained NER model (the saved spaCy pipeline is self-contained — no need
# to install fr_core_news_lg in runtime). Owned by `duke` so the unprivileged
# user can read it at startup.
COPY --from=trainer --chown=duke:duke /opt/duke-ner /app/models/duke-ner

USER duke

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "duke.main:app", "--host", "0.0.0.0", "--port", "8000"]
