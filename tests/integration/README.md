# Integration tests

The integration suite contains three families of tests, gated by markers:

## 1. `pytest.mark.integration` — testcontainers Postgres

Self-contained tests that spin up a throwaway Postgres via `testcontainers` and
exercise:

- `EkylibreReadDb.with_tenant()` multi-tenant isolation (`test_read_db_isolation.py`)
- `ConversationRepository` CRUD against the Duke schema (`test_repositories.py`)
- The retention job (`test_retention.py`)
- The full WS flow with mocked LLM/HTTP (`test_intervention_e2e.py`, `test_qa_e2e.py`)
- Golden NLU corpus accuracy (`test_golden_intents.py`)

These run with no extra setup:

```bash
uv run pytest -m integration
```

Docker must be reachable. The first run downloads `postgres:16-alpine`.

## 2. `pytest.mark.ekylibre_real` — opt-in e2e against a running Ekylibre

These connect to a real Ekylibre instance and validate that:
- `GET /api/v2/users/me` returns the expected payload
- A bad token is rejected with 401 (raises `EkylibreAuthError`)
- The `duke_reader` Postgres role can SELECT in a tenant schema
- `stock_for_variant()` returns a row backed by `product_populations`
- `duke_reader` cannot UPDATE (defense in depth, both app-level and DB-level)

### Prerequisites

1. **Ekylibre dev container running** (Rails on `:3000`, Postgres on `:5431`):

   ```bash
   cd /home/djoulin/projects/ekylibre
   docker compose -f docker/dev/docker-compose.yml up -d
   ```

2. **`duke_reader` Postgres role provisioned** (in the Ekylibre repo, branch
   `duke/duke-reader-role`):

   ```bash
   # Create the role and grant on lexicon + public.
   docker compose -f docker/dev/docker-compose.yml exec \
     -e PGPASSWORD=ekylibre app \
     psql -h db -U ekylibre -d eky_development \
     -v duke_password='duke_reader_dev' -f /app/db/setup/duke_reader.sql

   # Grant SELECT on every tenant schema.
   docker compose -f docker/dev/docker-compose.yml exec app \
     bundle exec rake duke_reader:grant_tenants

   # Verify no write privileges leak.
   docker compose -f docker/dev/docker-compose.yml exec app \
     bundle exec rake duke_reader:verify
   ```

3. **A real user with an `authentication_token`** in the target tenant. To
   fetch one from the dev DB:

   ```bash
   docker compose -f docker/dev/docker-compose.yml exec app \
     bundle exec rails runner '
       Apartment::Tenant.switch!("closeriedesterres")
       u = User.first
       puts "#{u.email} #{u.authentication_token}"
     '
   ```

4. **`tests/integration/.env.ekylibre_real`** filled out from the example:

   ```bash
   cp tests/integration/.env.ekylibre_real.example tests/integration/.env.ekylibre_real
   $EDITOR tests/integration/.env.ekylibre_real
   ```

   The default URL `http://closeriedesterres.lvh.me:3000` works in dev because
   `lvh.me` resolves to `127.0.0.1`, and the Apartment subdomain elevator picks
   up the tenant from the host. In production with the header elevator, the
   `X-Tenant` header sent by `EkylibreApiClient` is what drives tenant routing.

### Run

```bash
RUN_EKYLIBRE_E2E=1 uv run pytest -m ekylibre_real
```

Without `RUN_EKYLIBRE_E2E=1` the entire module is skipped, so these tests are
safe to leave in the suite.

## 3. Default suite (no marker)

```bash
uv run pytest
```

Runs everything except `ekylibre_real`. Currently 104 tests, all green.
