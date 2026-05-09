# Duke

Agricultural assistant chatbot for Ekylibre. Python service exposing a WebSocket API to a JS chat widget embedded in Ekylibre's backend. Records interventions in natural French ("j'ai pulvérisé 2L de Karaté Zeon sur la parcelle Bel Air ce matin pendant 2h") and answers read-only questions over the farm data ("combien de Karaté Zeon me reste-t-il ?").

See [`REQUIREMENTS.md`](./REQUIREMENTS.md) and [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full spec.

## Status

MVP delivered through iteration 8 (saisie d'intervention + Q&A + voix + clarify). Itérations 9 (stabilisation Ekylibre live) et 10 (NER agricole entraîné, F1 0.94) durcissent l'intégration — première intervention créée bout-en-bout en live le 2026-05-08. Itération 11 (Whisper STT serveur) ajoute un fallback `POST /api/v1/stt/transcribe` (faster-whisper) pour les navigateurs sans Web Speech API (Firefox, certains contextes mobile).

| Layer | Delivered |
|---|---|
| Foundations | FastAPI + WS transport, Alembic migrations, structured logs, Prometheus metrics, multi-tenant Postgres isolation primitive (`SET LOCAL search_path` + readonly tx) |
| NLU | spaCy pipeline (Duke-trained NER baked at `/app/models/duke-ner` with auto-detection, fallback to `fr_core_news_lg` then blank-fr), French temporal parser, `EntityRuler` from lexicon, rule-based intent classifier, golden corpus + accuracy gate |
| LLM | `LLMRouter` Claude + Mistral with automatic fallback, streaming for Q&A, function-calling for intervention extraction, prompt caching |
| Use cases | `InterventionRecorder` (POST /api/v2/interventions), `QueryAnswerer` (qa_stock + qa_history via Postgres direct read) |
| Persistence | `conversation_session` / `conversation_turn` / `intervention_draft` / `audit_event` in Duke's own DB, RGPD retention job, hashed tenant/user identifiers |
| Hardening | Per-session sliding-window rate limiter, best-effort persistence (Duke DB outages don't block users) |
| Frontend | Vanilla JS chat widget (bubble + panel + draft card + bouton micro Web Speech API fr-FR) embedded in Ekylibre's `backend.html.haml` via `app/javascript/duke/` and `app/views/shared/_duke_widget.html.haml` |
| Ekylibre side | `GET /api/v2/users/me` endpoint, `duke_reader` read-only Postgres role + Rake task, `Backend::DukeWidgetController#show` config endpoint |

**Tests**: 406 collected by default (unit + integration testcontainers) + 6 opt-in e2e against a running Ekylibre + 1 opt-in NER training smoke + 1 opt-in real-Whisper smoke (`RUN_STT_SMOKE=1`).

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

To bake the Whisper STT backend into the image (adds ~250 MB for
faster-whisper + ctranslate2 + onnxruntime), set `INSTALL_STT=true` at
build time *and* `ENABLE_SERVER_STT=true` at runtime — they're two
distinct toggles (image content vs. feature flag):

```bash
INSTALL_STT=true docker compose -f docker/docker-compose.yml build duke-api
docker compose -f docker/docker-compose.yml up -d
```

Model weights (~150 MB for `small`) download on first transcription and
persist in the `whisper-cache` named volume mounted at
`/home/duke/.cache/huggingface`, so subsequent rebuilds reuse them.

Endpoints:
- `GET /healthz` — liveness
- `GET /readyz` — readiness (Duke DB + Ekylibre DB)
- `GET /metrics` — Prometheus
- `WS /ws` — chat WebSocket entry point
- `POST /api/v1/stt/transcribe` — opt-in Whisper fallback (multipart `audio` + `Authorization: simple-token <email> <token>` + `X-Tenant: <tenant>`). Returns `{"text": "..."}`. Disabled (503) unless `ENABLE_SERVER_STT=true`; backend deps via `uv sync --extra stt`.

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

### Local HTTPS for voice / STT testing

The browser mic APIs (`navigator.mediaDevices`, Web Speech,
`MediaRecorder`) require a [secure context](https://developer.mozilla.org/docs/Web/Security/Secure_Contexts)
— either `localhost` or HTTPS. Accessing Ekylibre via an IP or LAN
hostname over plain HTTP silently hides the mic button in Firefox and
refuses mic permission in Chrome.

#### 1. Install a TLS-terminating dev proxy

Two interchangeable tools — pick whichever your machine supports:

| Tool | When to pick | Install |
|---|---|---|
| **Caddy** *(recommended, works everywhere)* | Default. Single static Go binary, no glibc dependency. Reverse-proxies `duke.test` + `ekylibre.test`, generates a local CA on first run and pushes it into both system and Firefox trust stores. | apt repo (see below) |
| [slim.sh](https://slim.sh) | Nicer CLI, but the release binary requires **glibc ≥ 2.34**. Won't run on Ubuntu 20.04 / Debian 11. | `curl -sL https://slim.sh/install.sh \| sh` |

**Caddy** (Debian/Ubuntu):

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https libnss3-tools
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

`libnss3-tools` provides `certutil`, which Caddy calls to install its CA
into Firefox's separate NSS trust store (Chrome uses the system store
directly, so it doesn't need this — but installing it doesn't cost
anything).

#### 2. Map the dev domains to localhost

```bash
echo "127.0.0.1 duke.test ekylibre.test" | sudo tee -a /etc/hosts
```

#### 3. Start the proxy

This repo ships both a `Caddyfile` (proxies both projects in a single
process) and a `.slim.yaml` (Duke-only; Ekylibre has its own).

```bash
# Caddy — from this directory, binds :443 (sudo required)
cd ~/projects/duke
sudo caddy run

# OR slim — one process per project
cd ~/projects/duke    && slim up
cd ~/projects/ekylibre && slim up
```

First run, Caddy emits `certificate authority is now trusted` once it
finishes installing the CA. If Firefox was already running, restart it
— it only reads NSS at startup. Verify:

```bash
curl -sI https://duke.test/healthz       # HTTP/2 200
curl -sI https://ekylibre.test/          # whatever Rails returns
```

#### 4. Wire Ekylibre to the HTTPS Duke

In Ekylibre's compose, point Rails at the HTTPS URLs and enable the
server STT fallback:

```yaml
services:
  app:
    environment:
      - DUKE_WS_URL=wss://duke.test/ws
      - DUKE_HTTP_URL=https://duke.test          # used to derive stt_url
      - DUKE_STT_SERVER_ENABLED=true             # exposes the mic fallback
      - ELEVATOR=header
```

Restart Rails (`docker compose restart app`) and rebuild the JS bundle
if your asset pipeline doesn't auto-reload. Open `https://ekylibre.test`
in Firefox: the mic button appears. In Chrome, the permission prompt
resolves to "allow".

WebSocket upgrades (`wss://duke.test/ws` → `ws://localhost:8000/ws`)
are handled transparently by both proxies — no extra config.

### CLI tools

```bash
# RGPD retention: anonymize conversation_turn.text past RETENTION_DAYS_TURN_TEXT
uv run python -m duke.cli.retention purge

# Database migrations
uv run alembic upgrade head

# Inspect the NER training corpus before training (label distribution,
# span alignment, duplicates).
uv run python -m duke.cli.corpus_stats

# Train a custom Duke NER (writes a spaCy model to ./models/ner/duke-fr-v1).
# Wire it in via DUKE_NER_MODEL_PATH=./models/ner/duke-fr-v1 — Duke loads the
# trained model in place of SPACY_MODEL while keeping the EntityRuler overlay.
uv run python -m duke.cli.train_ner \
  --base-model fr_core_news_lg \
  --corpus tests/fixtures/golden_phrases.yaml \
  --n-synth 800 --n-iter 30 \
  --output models/ner/duke-fr-v1
```

### NER corpus enrichment

The training corpus lives in `tests/fixtures/golden_phrases.yaml`. Each
entry is a French phrase plus the entity spans the model should learn:

```yaml
- text: "j'ai pulvérisé 2L de Karaté Zeon sur la parcelle Bel Air ce matin"
  intent: record_intervention
  entities:
    - {label: DUKE_PROCEDURE, span: "pulvérisé"}
    - {label: DUKE_QUANTITY, span: "2L"}
    - {label: DUKE_PRODUCT, span: "Karaté Zeon"}
    - {label: DUKE_PARCEL, span: "Bel Air"}
```

Conventions:

- **`span`** is the literal substring as it appears in `text` (case +
  accents must match). The converter resolves it to char offsets at load
  time. If the same substring repeats, add `nth: 0|1|2…` to pick which
  occurrence.
- **Labels**: `DUKE_PRODUCT`, `DUKE_PROCEDURE`, `DUKE_PARCEL`,
  `DUKE_QUANTITY`, `DUKE_WORKER` (operators, doers), `DUKE_TOOL`
  (equipment / motorized vehicles). Add new ones consistently across
  phrases or the model won't have enough signal to learn them.
- **Dates and durations are NOT NER entities** — `src/duke/nlu/temporal.py`
  parses them deterministically from French phrasing ("ce matin",
  "pendant 2 heures", "à 14h30", "15/03/2024") into structured
  `started_at` / `stopped_at` / `working_duration` and feeds the result
  to the LLM via hints. Annotating them with NER labels would duplicate
  signal without improving resolution. The 3 `DUKE_QUANTITY` annotations
  in the corpus today cover physical quantities (`200kg`, `2L`) only.
- **`entities` is optional** — `qa_history` / `out_of_scope` / `unknown`
  phrases often have nothing to extract.
- Run `uv run python -m duke.cli.corpus_stats` after each batch of edits;
  it surfaces token-misaligned spans (the silent killer of NER training)
  with concrete examples to fix.

Sizing guidance: the bigger the corpus, the more useful the trained NER
gets. ~50–100 hand-curated phrases per recurring user pattern is a
healthy floor; the synthesizer adds another 800 templated examples on
top. The Docker build bakes the trained model into the runtime image
(see `docker/Dockerfile` trainer stage), so updating the corpus and
rebuilding the image is the canonical way to ship a new NER.

## Tests

```bash
uv run pytest                    # 406 tests (unit + integration with testcontainers)
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

Opt-in real Whisper smoke test (downloads a small faster-whisper model on first run):

```bash
RUN_STT_SMOKE=1 uv run pytest -m stt_smoke
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
| 9 | Stabilisation Ekylibre live — `provider` envelope, payload à plat (Hash form), canonicalisation procédure via lexique, hydration `ProcedureRegistry` au 1ᵉʳ auth, mapping spec-aware (`reference_name` issu des slots Procedo), `description` = phrase utilisateur originale | ✅ |
| 10 | NER agricole entraîné — corpus enrichi (267 phrases, 401 spans), 6 labels (+ DUKE_WORKER + DUKE_TOOL), CLI `train_ner` baked en stage Docker. F1 0.94 global, 1.00 sur PRODUCT, 0.97 sur PARCEL. Modèle auto-détecté à `/app/models/duke-ner` au runtime | ✅ |
| 11 | Whisper STT serveur — endpoint `POST /api/v1/stt/transcribe` (faster-whisper, lazy-load, auth `simple-token` + `X-Tenant`), opt-in via `ENABLE_SERVER_STT=true`. Widget bascule sur `MediaRecorder` + POST quand Web Speech API absent (Firefox, certains mobile) ; transcript injecté dans le textarea, le pipeline NLU n'a aucun chemin spécifique au STT | ✅ |
| 12+ | Multi-instance scaling Redis, fonctions Ekylibre phase 2 (grand livre) | future |

External-side dependencies (`ARCHITECTURE.md §10`): D1–D5 done, D6 (LLM API keys) is ops/secret management.

## Project layout

```
src/duke/
├── transport/         # WS server + STT HTTP route + Pydantic message schemas
├── application/       # Orchestrator, InterventionRecorder, QueryAnswerer
├── nlu/               # spaCy pipeline, intent classifier, temporal parser
│   └── llm/           # LLMProvider Protocol + Claude / Mistral / Router
├── stt/               # WhisperService (faster-whisper, lazy-load, async wrapper)
├── domain/            # Pure Pydantic models (Intent, InterventionDraft, ...)
├── integration/
│   ├── ekylibre/      # api_client, read_db, lexicon_repo, mappers
│   └── store/         # SQLAlchemy models, repositories, retention, hashing
├── observability/     # structlog config, Prometheus metrics
└── cli/               # Operational entrypoints (retention)
```
