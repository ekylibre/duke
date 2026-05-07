from __future__ import annotations

import asyncpg
import pytest

from duke.integration.ekylibre.read_db import EkylibreReadDb

pytestmark = pytest.mark.integration

try:
    from testcontainers.postgres import PostgresContainer

    _HAS_TESTCONTAINERS = True
except Exception:  # pragma: no cover
    _HAS_TESTCONTAINERS = False


def _asyncpg_dsn(container: PostgresContainer) -> str:
    user = container.username
    password = container.password
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    db = container.dbname
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture(scope="module")
def pg_container():
    if not _HAS_TESTCONTAINERS:
        pytest.skip("testcontainers not installed")
    with PostgresContainer("postgres:16-alpine") as container:
        yield container


@pytest.fixture
async def pool(pg_container) -> asyncpg.Pool:  # type: ignore[no-untyped-def]
    dsn = _asyncpg_dsn(pg_container)

    setup = await asyncpg.connect(dsn)
    try:
        await setup.execute("DROP SCHEMA IF EXISTS tenant_a CASCADE")
        await setup.execute("DROP SCHEMA IF EXISTS tenant_b CASCADE")
        await setup.execute("DROP SCHEMA IF EXISTS lexicon CASCADE")
        await setup.execute("CREATE SCHEMA tenant_a")
        await setup.execute("CREATE SCHEMA tenant_b")
        await setup.execute("CREATE SCHEMA lexicon")

        await setup.execute(
            "CREATE TABLE tenant_a.land_parcels (id int PRIMARY KEY, name text NOT NULL)"
        )
        await setup.execute(
            "CREATE TABLE tenant_b.land_parcels (id int PRIMARY KEY, name text NOT NULL)"
        )
        await setup.execute(
            "INSERT INTO tenant_a.land_parcels VALUES (1, 'Pre du Moulin'), (2, 'Bel Air A')"
        )
        await setup.execute(
            "INSERT INTO tenant_b.land_parcels VALUES (1, 'Vigne du Bas'), (2, 'Bel Air B')"
        )
    finally:
        await setup.close()

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


async def test_with_tenant_a_sees_only_tenant_a(pool: asyncpg.Pool) -> None:
    db = EkylibreReadDb(pool)
    async with db.with_tenant("tenant_a") as reader:
        rows = await reader.fetch("SELECT name FROM land_parcels ORDER BY id")
    names = [r["name"] for r in rows]
    assert names == ["Pre du Moulin", "Bel Air A"]


async def test_with_tenant_b_sees_only_tenant_b(pool: asyncpg.Pool) -> None:
    db = EkylibreReadDb(pool)
    async with db.with_tenant("tenant_b") as reader:
        rows = await reader.fetch("SELECT name FROM land_parcels ORDER BY id")
    names = [r["name"] for r in rows]
    assert names == ["Vigne du Bas", "Bel Air B"]


async def test_outside_with_tenant_table_not_visible(pool: asyncpg.Pool) -> None:
    """Without applying tenant search_path, the unqualified table must not resolve."""
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.UndefinedTableError):
            await conn.fetch("SELECT name FROM land_parcels")


async def test_invalid_schema_name_rejected(pool: asyncpg.Pool) -> None:
    db = EkylibreReadDb(pool)
    with pytest.raises(ValueError):
        async with db.with_tenant("'; DROP TABLE x; --") as _:
            pass
    with pytest.raises(ValueError):
        async with db.with_tenant("Tenant_A") as _:
            pass
    with pytest.raises(ValueError):
        async with db.with_tenant("9tenant") as _:
            pass


async def test_pool_connections_reset_between_tenants(pool: asyncpg.Pool) -> None:
    """SET LOCAL must scope search_path to the transaction; the next acquire must not inherit it."""
    db = EkylibreReadDb(pool)
    async with db.with_tenant("tenant_a") as reader:
        await reader.fetch("SELECT name FROM land_parcels")

    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.UndefinedTableError):
            await conn.fetch("SELECT name FROM land_parcels")


async def test_concurrent_tenants_isolated(pool: asyncpg.Pool) -> None:
    import asyncio

    db = EkylibreReadDb(pool)

    async def read_for(schema: str, expected_first: str) -> None:
        async with db.with_tenant(schema) as reader:
            await asyncio.sleep(0.01)
            rows = await reader.fetch("SELECT name FROM land_parcels ORDER BY id")
        assert rows[0]["name"] == expected_first

    await asyncio.gather(
        read_for("tenant_a", "Pre du Moulin"),
        read_for("tenant_b", "Vigne du Bas"),
        read_for("tenant_a", "Pre du Moulin"),
        read_for("tenant_b", "Vigne du Bas"),
    )
