"""Opt-in e2e tests against a running Ekylibre instance.

These tests are skipped by default. Run them with:

    RUN_EKYLIBRE_E2E=1 uv run pytest -m ekylibre_real

Requires:
- Ekylibre dev container running (rails on :3000, postgres on :5431)
- duke_reader role provisioned (db/setup/duke_reader.sql + rake task)
- A `.env.ekylibre_real` file at tests/integration/, see the .example template

The tests cover:
- POST /api/v2/users/me with a real token (validate_token)
- 401 path with a wrong token
- Direct Postgres read of land_parcels via with_tenant
- stock_for_variant against real product_populations data
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import httpx
import pytest

from duke.integration.ekylibre.api_client import (
    EkylibreApiClient,
    EkylibreAuthError,
    EkylibreCredentials,
)
from duke.integration.ekylibre.read_db import EkylibreReadDb

pytestmark = [pytest.mark.ekylibre_real, pytest.mark.integration]

if os.environ.get("RUN_EKYLIBRE_E2E") != "1":
    pytest.skip(
        "Set RUN_EKYLIBRE_E2E=1 to run the Ekylibre e2e tests.",
        allow_module_level=True,
    )


def _load_env_file(path: Path) -> None:
    """Minimal `.env` loader so the test stays dependency-free."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(Path(__file__).parent / ".env.ekylibre_real")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} not set; copy .env.ekylibre_real.example to .env.ekylibre_real")
    return value


@pytest.fixture(scope="module")
def cfg() -> dict[str, str]:
    return {
        "url": _required("EKYLIBRE_TEST_URL"),
        "tenant": _required("EKYLIBRE_TEST_TENANT"),
        "email": _required("EKYLIBRE_TEST_USER_EMAIL"),
        "token": _required("EKYLIBRE_TEST_USER_TOKEN"),
        "db_dsn": _required("EKYLIBRE_TEST_DB_DSN"),
    }


@pytest.fixture
async def http(cfg) -> httpx.AsyncClient:
    async with httpx.AsyncClient(base_url=cfg["url"], timeout=10.0) as client:
        yield client


@pytest.fixture
async def pool(cfg) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn=cfg["db_dsn"], min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


def _creds(cfg: dict[str, str], *, token: str | None = None) -> EkylibreCredentials:
    return EkylibreCredentials(
        email=cfg["email"],
        token=token or cfg["token"],
        tenant=cfg["tenant"],
        base_url=cfg["url"],
    )


# --- API ---


async def test_validate_token_returns_user(cfg, http) -> None:
    api = EkylibreApiClient(_creds(cfg), http)
    user = await api.validate_token()
    assert user.email == cfg["email"]
    assert user.id > 0
    assert user.full_name


async def test_validate_token_rejects_bad_token(cfg, http) -> None:
    api = EkylibreApiClient(_creds(cfg, token="not-a-real-token"), http)
    with pytest.raises(EkylibreAuthError):
        await api.validate_token()


# --- Read DB ---


async def test_with_tenant_reads_land_parcels(cfg, pool) -> None:
    """Verify the query runs and the schema is right; the row count depends on
    the dev tenant's data (some tenants have all parcels marked dead_at != NULL)."""
    db = EkylibreReadDb(pool)
    async with db.with_tenant(cfg["tenant"]) as reader:
        parcels = await reader.list_land_parcels(limit=20)
        # Without the dead_at filter, we should always see rows in any tenant
        # that ever had land parcels — confirms the schema is reachable.
        any_parcels = await reader.fetchval(
            "SELECT COUNT(*) FROM products WHERE type = 'LandParcel'"
        )

    assert isinstance(parcels, list)
    if parcels:
        assert {"id", "name"} <= set(parcels[0].keys())
    assert any_parcels >= 1, "expected at least one historical land parcel in this tenant"


async def test_with_tenant_reads_products(cfg, pool) -> None:
    db = EkylibreReadDb(pool)
    async with db.with_tenant(cfg["tenant"]) as reader:
        products = await reader.list_products(limit=10)
    assert len(products) >= 1
    assert {"id", "name", "variant_id"} <= set(products[0].keys())


async def test_stock_for_variant_returns_population(cfg, pool) -> None:
    """Pick any variant that has population data and assert the query returns a total."""
    db = EkylibreReadDb(pool)
    async with db.with_tenant(cfg["tenant"]) as reader:
        row = await reader.fetchrow(
            """
            SELECT p.variant_id
            FROM products p
            JOIN product_populations pp ON pp.product_id = p.id
            WHERE p.dead_at IS NULL AND p.variant_id IS NOT NULL
            GROUP BY p.variant_id
            LIMIT 1
            """
        )
        assert row is not None, "no variant with population data found in dev tenant"
        variant_id = row["variant_id"]
        stock = await reader.stock_for_variant(variant_id)

    assert stock is not None
    assert stock["variant_id"] == variant_id
    assert stock["total"] >= 0.0
    assert stock["last_update"] is not None


async def test_duke_reader_cannot_write(cfg, pool) -> None:
    """Defense in depth: app-level (readonly transaction) and DB-level (REVOKE)
    both block writes. We assert SOME PostgresError is raised — exact subclass
    depends on which layer fires first."""
    db = EkylibreReadDb(pool)
    with pytest.raises(asyncpg.PostgresError):
        async with db.with_tenant(cfg["tenant"]) as reader:
            await reader._conn.execute("UPDATE products SET name = name WHERE id = 1")

    # Also check the DB-level REVOKE without our readonly transaction wrapper.
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("UPDATE public.users SET email = email WHERE id = 1")
