# Duke

Agricultural assistant chatbot for Ekylibre. Python service exposing a WebSocket API to a JS chat widget embedded in Ekylibre's backend. Records interventions in natural French ("j'ai pulvérisé 2L de Karaté Zeon sur la parcelle Bel Air ce matin pendant 2h") and answers read-only questions over the farm data ("combien de Karaté Zeon me reste-t-il ?").

See [`REQUIREMENTS.md`](./REQUIREMENTS.md) and [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full spec.

## Status

MVP delivered through iteration 6 ; itération 7 ajoute l'infrastructure d'entraînement NER agricole (corpus annoté + synthétiseur + CLI + intégration pipeline).

| Layer | Delivered |
|---|---|
| Foundations | FastAPI + WS transport, Alembic migrations, structured logs, Prometheus metrics, multi-tenant Postgres isolation primitive (`SET LOCAL search_path` + readonly tx) |
| NLU | spaCy pipeline (`fr_core_news_lg` with blank-fr fallback), French temporal parser, `EntityRuler` from lexicon, rule-based intent classifier, golden corpus + accuracy gate |
| LLM | `LLMRouter` Claude + Mistral with automatic fallback, streaming for Q&A, function-calling for intervention extraction, prompt caching |
| Use cases | `InterventionRecorder` (POST /api/v2/interventions), `QueryAnswerer` (qa_stock + qa_history via Postgres direct read) |
| Persistence | `conversation_session` / `conversation_turn` / `intervention_draft` / `audit_event` in Duke's own DB, RGPD retention job, hashed tenant/user identifiers |
| Hardening | Per-session sliding-window rate limiter, best-effort persistence (Duke DB outages don't block users) |
| Frontend | Vanilla JS chat widget (bubble + panel + draft card + bouton micro Web Speech API fr-FR) embedded in Ekylibre's `backend.html.haml` via `app/javascript/duke/` and `app/views/shared/_duke_widget.html.haml` |
| Ekylibre side | `GET /api/v2/users/me` endpoint, `duke_reader` read-only Postgres role + Rake task, `Backend::DukeWidgetController#show` config endpoint |

**Tests**: 104 passing (default suite) + 6 opt-in e2e against a running Ekylibre.

## Bootstrap

Requires `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`) and Docker.

### Backend (this repo)

```bash
uv sync --extra dev

cp .env.example .env
$EDITOR .env  # set EKYLIBRE_DB_DSN, DUKE_DB_DSN, ANTHROPIC_API_KEY, HASH_SECRET

# Bring up Duke + its Postgres on the shared `ekylibre` Docker network.
docker compose -f docker/docker-compose.yml up -d
```

Endpoints:
- `GET /healthz` — liveness
- `GET /readyz` — readiness (Duke DB + Ekylibre DB)
- `GET /metrics` — Prometheus
- `WS /ws` — chat WebSocket entry point

### Ekylibre side (in `/home/djoulin/projects/ekylibre`)

The widget is consumed by Ekylibre. Three artifacts must be in place:

1. **`GET /api/v2/users/me`** route + controller (branch `duke/api-v2-users-me`, merged).
2. **`duke_reader` Postgres role** provisioned via `db/setup/duke_reader.sql` and `rake duke_reader:grant_tenants` (branch `duke/duke-reader-role`, merged).
3. **Chat widget** in `app/javascript/duke/` + `app/views/shared/_duke_widget.html.haml` rendered from `backend.html.haml` (branch `duke/chat-widget`).

Tell Rails where to reach Duke via env (in Ekylibre's compose):
```yaml
services:
  app:
    environment:
      - DUKE_WS_URL=ws://localhost:8000/ws
      - ELEVATOR=header   # so Duke can reach a tenant via X-Tenant header
```

### CLI tools

```bash
# RGPD retention: anonymize conversation_turn.text past RETENTION_DAYS_TURN_TEXT
uv run python -m duke.cli.retention purge

# Database migrations
uv run alembic upgrade head

# Train a custom Duke NER (writes a spaCy model to ./models/ner/duke-fr-v1).
# Wire it in via DUKE_NER_MODEL_PATH=./models/ner/duke-fr-v1 — Duke loads the
# trained model in place of SPACY_MODEL while keeping the EntityRuler overlay.
uv run python -m duke.cli.train_ner \
  --base-model fr_core_news_lg \
  --corpus tests/fixtures/golden_phrases.yaml \
  --n-synth 800 --n-iter 30 \
  --output models/ner/duke-fr-v1
```

## Tests

```bash
uv run pytest                    # 104 tests (unit + integration with testcontainers)
uv run pytest -m integration     # only the docker-backed subset
uv run ruff check                # lint
```

Opt-in e2e against a running Ekylibre (see `tests/integration/README.md` for the full procedure):

```bash
RUN_EKYLIBRE_E2E=1 uv run pytest -m ekylibre_real
```

Opt-in NER training smoke test (forces blank-fr to keep the run lightweight):

```bash
RUN_NER_TRAINING=1 uv run pytest -m ner_training
```

## Architecture overview

- **Reads** go directly to Ekylibre's Postgres via `duke_reader` (read-only role, `SET LOCAL search_path TO {tenant}, lexicon, public` per query).
- **Writes** (intervention creation) go through Ekylibre's REST API v2.
- **Token validation** via `GET /api/v2/users/me` on every WS auth.
- **NLU** is a hybrid: spaCy extracts cheap candidates (entities, temporal, intent), the LLM (Claude or Mistral via fallback router) handles ambiguity and structured extraction via function calling.
- **Q&A** is grounded: the SQL is deterministic (Duke decides what to fetch from intent), the LLM only formats the answer.
- **Multi-tenant isolation** is enforced both app-side (regex-validated identifiers + readonly tx) and DB-side (REVOKE writes on `duke_reader`).

See `ARCHITECTURE.md` for the full design.

## Iterations

| # | Theme | Status |
|---|---|---|
| 1 | Foundations (transport, migrations, isolation primitive) | ✅ |
| 2 | NLU (spaCy + LLM router) + InterventionRecorder | ✅ |
| 3 | Q&A (QueryAnswerer + streaming + golden corpus) | ✅ |
| 4 | Hardening (persistence, retention, rate limiting) | ✅ |
| 5 | Real e2e (Ekylibre `/users/me` + `duke_reader` + opt-in test suite) | ✅ |
| 6 | Frontend chat widget in Ekylibre backend | ✅ |
| 7 | NER agricole — corpus annoté + synth + train CLI + load via `DUKE_NER_MODEL_PATH` | ✅ |
| 8 | Saisie vocale + clarify — bouton micro Web Speech API (fr-FR), résolution d'ambiguïtés via `clarify` (textarea bascule, fiche replacée en place, draft re-extrait par Duke) | ✅ |
| 9+ | Whisper STT serveur, multi-instance scaling, fonctions Ekylibre phase 2 | future |

External-side dependencies (`ARCHITECTURE.md §10`): D1–D5 done, D6 (LLM API keys) is ops/secret management.

## Project layout

```
src/duke/
├── transport/         # WS server + Pydantic message schemas
├── application/       # Orchestrator, InterventionRecorder, QueryAnswerer
├── nlu/               # spaCy pipeline, intent classifier, temporal parser
│   └── llm/           # LLMProvider Protocol + Claude / Mistral / Router
├── domain/            # Pure Pydantic models (Intent, InterventionDraft, ...)
├── integration/
│   ├── ekylibre/      # api_client, read_db, lexicon_repo, mappers
│   └── store/         # SQLAlchemy models, repositories, retention, hashing
├── observability/     # structlog config, Prometheus metrics
└── cli/               # Operational entrypoints (retention)
```
