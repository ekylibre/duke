# Duke

Agricultural assistant chatbot for Ekylibre. Python service exposing a WebSocket API to a JS client embedded in Ekylibre. Records interventions and answers read-only questions over the farm data.

See [`REQUIREMENTS.md`](./REQUIREMENTS.md) and [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full spec.

## Status — iteration 1: foundations

This iteration delivers the skeleton: project layout, configuration, FastAPI app with health/readiness/metrics, WebSocket transport (auth + heartbeat + dispatcher with stubbed handlers), Ekylibre API client (token validation only), Ekylibre read-only Postgres adapter with the per-tenant isolation primitive, Duke DB schema with Alembic migrations, structured logging, Prometheus metrics, Docker setup, and tests including the multi-tenant isolation test.

NLU (spaCy), LLM providers (Claude + Mistral), and the use cases (`InterventionRecorder`, `QueryAnswerer`) are stubbed and land in iteration 2.

## Bootstrap (dev)

Requires `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`) and Docker.

```bash
uv sync --extra dev

cp .env.example .env
# Edit .env: set EKYLIBRE_DB_DSN to the Ekylibre read-only account, point DUKE_DB_DSN to the local postgres-duke

docker compose -f docker/docker-compose.yml up -d postgres-duke

uv run alembic upgrade head

uv run uvicorn duke.main:app --reload
```

The service listens on `http://localhost:8000`. Endpoints:

- `GET /healthz` — liveness
- `GET /readyz` — readiness (checks Duke DB and Ekylibre DB)
- `GET /metrics` — Prometheus
- `WS /ws` — WebSocket entry point

## Tests

```bash
uv run pytest                    # unit tests (no docker required)
uv run pytest -m integration     # adds the multi-tenant isolation test (needs docker)
uv run ruff check                # lint
uv run ruff format --check       # formatting
```

The integration test spins up a throwaway Postgres via `testcontainers` and verifies that `EkylibreReadDb.with_tenant()` only ever sees one tenant schema at a time, that pool connections are reset between tenants, and that invalid schema names are rejected.

## Roadmap

- **Iteration 2 — NLU + use cases**: spaCy pipeline (`fr_core_news_lg`), `EntityRuler` driven by `LexiconRepository`, French temporal parser, `LLMRouter` (Claude + Mistral), `InterventionRecorder` use case bound to US-1.
- **Iteration 3 — Q&A**: `QueryAnswerer` use case backed by `EkylibreReadDb` + LLM phrasing, golden-set evaluation in CI.
- **Iteration 4 — Hardening**: retention/anonymization job, rate limiting, end-to-end test against a real Ekylibre container.

## External dependencies (must land before MVP can run end-to-end)

See `ARCHITECTURE.md` §10. Notably: `GET /api/v2/users/me` route to add on the Ekylibre side, and the `duke_reader` Postgres role provisioned with `USAGE` on tenant schemas + `lexicon`.
