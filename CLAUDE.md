# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repo. Keep
this file specific to *this* codebase — non-obvious commands, decisions
that span multiple files, gotchas. Don't restate generic Python practices.

## What Duke is

Agricultural-assistant chatbot for Ekylibre. Python service exposing a
WebSocket to a JS chat widget embedded in Ekylibre's backend. Records
interventions in natural French (`POST /api/v2/interventions` on Ekylibre)
and answers read-only questions over the farm data (Postgres direct read
via `duke_reader`). MVP scope is in `REQUIREMENTS.md`; full design in
`ARCHITECTURE.md`; ops + bootstrap in `README.md`.

## Stack

- **Python 3.12+** with `uv` (no pip / poetry). `uv sync --extra dev` for
  test deps; `uv sync --extra stt` to add the Whisper backend.
- **FastAPI + Starlette WS** for transport, **SQLAlchemy 2 async** +
  Alembic for Duke's own DB, **asyncpg** for the read-only Ekylibre DB.
- **spaCy + LLMRouter** (Claude/Mistral) hybrid NLU. spaCy extracts cheap
  candidates; LLM handles ambiguity + structured extraction via tool use.
- **faster-whisper** (CTranslate2) for the opt-in server STT fallback.

## Common commands

```bash
uv run pytest                    # 406 tests by default (unit + testcontainers integration)
uv run pytest -m integration     # docker-backed subset only
uv run ruff check                # lint (configured in pyproject.toml)
uv run alembic upgrade head      # apply Duke DB migrations
uv run python -m duke.cli.retention purge      # RGPD anonymization
uv run python -m duke.cli.corpus_stats         # validate the NER corpus
uv run python -m duke.cli.train_ner ...        # train a Duke-NER model
```

Opt-in test markers (skipped by default):
- `RUN_EKYLIBRE_E2E=1 pytest -m ekylibre_real` — needs a running Ekylibre dev container
- `RUN_NER_TRAINING=1 pytest -m ner_training` — training smoke (forces blank-fr)
- `RUN_STT_SMOKE=1 pytest -m stt_smoke` — pulls the real faster-whisper model

## Architecture invariants

- **Multi-tenant isolation** is enforced both in the app (regex-validated
  identifiers + readonly tx) and in the DB (`REVOKE` writes on
  `duke_reader`). Reads do `SET LOCAL search_path TO {tenant}, lexicon, public`
  per query inside a readonly transaction.
- **Auth to Ekylibre** is `Authorization: simple-token <email> <token>` +
  `X-Tenant: <tenant>`. Validated via `GET /api/v2/users/me` on every WS
  open AND on every HTTP STT request. Tokens never persist in Duke.
- **Procedo registry** (`integration/ekylibre/procedure_registry.py`)
  hydrates lazily on the first auth using the user's session credentials.
  Static `DEFAULT_PROCEDURES` is the bootstrap fallback while empty.
- **Best-effort persistence**: Duke-DB outages don't block users.
  `_safe(coro)` in `transport/ws_server.py` swallows persistence errors.
- **Streaming Q&A**: `assistant_token` deltas over WS, finalized with
  `assistant_message`. Q&A SQL is deterministic from intent — the LLM
  only formats the answer over fetched evidence.
- **Mapper to Ekylibre interventions** is Procedo-aware — `reference_name`
  on each parameter slot comes from the procedure spec, not the lexicon.
  See ARCHITECTURE.md §11 iteration 9 for the full list of Rails-side
  contracts (`provider` envelope, flat payload, working_periods always
  emitted, etc.) that Duke must respect.

## Server-side Whisper STT (iteration 11)

Two distinct toggles — don't conflate them:

| Toggle | Where | Purpose |
|---|---|---|
| `INSTALL_STT=true` | Docker build-arg | Bake `faster-whisper` into the image (~250 MB) |
| `ENABLE_SERVER_STT=true` | runtime env in `.env` | Make `POST /api/v1/stt/transcribe` respond instead of 503 |

The route exists unconditionally so FastAPI can introspect its
`UploadFile` parameter at import; that's why `python-multipart` is in
base deps, not in an extra. The model loads lazily on the first
transcription and caches in the `whisper-cache` Docker volume mounted at
`/home/duke/.cache/huggingface`.

Browser-side mic APIs (Web Speech, MediaDevices) need a **secure
context**. For dev that means HTTPS or `localhost` — not LAN HTTP. The
repo ships `Caddyfile` + `.slim.yaml` for local TLS termination.
On Ubuntu 20.04 / Debian 11, slim's release binary won't run (glibc
2.31 < 2.34); use Caddy.

## Testing the widget end-to-end

The widget lives in `/home/djoulin/projects/ekylibre/app/javascript/duke/`.
Tests for `Backend::DukeWidgetController` are in
`test/controllers/backend/duke_widget_controller_test.rb`. There's no
Jest suite for the JS — manual validation in Chrome (Web Speech path)
and Firefox (server-STT path, requires `DUKE_STT_SERVER_ENABLED=true`).

When changing widget config keys, update both:
- `Backend::DukeWidgetController#show` (JSON shape)
- The corresponding controller test (asserts `expected_keys`)

## What lives where

```
src/duke/
├── transport/         # WS server, STT HTTP route, Pydantic message schemas
├── application/       # ConversationOrchestrator, InterventionRecorder, QueryAnswerer
├── nlu/               # spaCy pipeline, intent classifier, temporal parser
│   └── llm/           # LLMProvider Protocol + Claude / Mistral / LLMRouter
├── stt/               # WhisperService (faster-whisper, lazy-load, async wrapper)
├── domain/            # Pure Pydantic models (Intent, InterventionDraft, ...)
├── integration/
│   ├── ekylibre/      # api_client, read_db, lexicon_repo, procedure_registry, mappers
│   └── store/         # SQLAlchemy models, repositories, retention, hashing
├── observability/     # structlog config, Prometheus metrics
└── cli/               # Operational entrypoints (retention, train_ner, corpus_stats)
```

## Conventions

- Follow the existing module pattern when adding a route or service:
  Pydantic models in `domain/`, integration boundary in `integration/<system>/`,
  pure async business logic in `application/`, transport plumbing in `transport/`.
- Tests: unit tests under `tests/unit/`, integration (testcontainers)
  under `tests/integration/`. Add a marker in `pyproject.toml` for any
  new opt-in suite that needs an external resource.
- When you change `pyproject.toml`, run `uv lock --check` (the Dockerfile
  builds with `--frozen` and refuses out-of-sync lockfiles).
