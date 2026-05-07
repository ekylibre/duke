from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import asyncpg
import structlog

log = structlog.get_logger(__name__)

# NOTE on schema assumptions:
# Ekylibre's Rails STI model stores `LandParcel`, generic `Product` instances and
# `Activity` rows. The queries below assume:
#   - `products(id, name, type, variant_id, population_count, dead_at, updated_at)`
#   - `interventions(id, procedure_name, nature, started_at, stopped_at, state)`
#   - `intervention_targets(id, intervention_id, product_id, reference_name)`
#   - `activities(id, name)`
# Column names should be confirmed on a real Ekylibre dev instance and may need
# adjustments before production use (tracked in ARCHITECTURE.md §10).

_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _quote_ident(name: str) -> str:
    """Quote a Postgres identifier. Defense-in-depth on top of regex validation."""
    return '"' + name.replace('"', '""') + '"'


class EkylibreReadDb:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def health(self) -> bool:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval("SELECT 1")
            return value == 1

    @asynccontextmanager
    async def with_tenant(self, tenant_schema: str) -> AsyncIterator[ScopedReader]:
        if not _SCHEMA_RE.match(tenant_schema):
            raise ValueError(f"invalid tenant schema: {tenant_schema!r}")
        quoted = _quote_ident(tenant_schema)

        async with self._pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL search_path TO {quoted}, lexicon, public")
            yield ScopedReader(conn)


class ScopedReader:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        return await self._conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        return await self._conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await self._conn.fetchval(query, *args)

    async def list_land_parcels(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(
            "SELECT id, name FROM products "
            "WHERE type = 'LandParcel' AND dead_at IS NULL "
            "ORDER BY name LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]

    async def list_products(self, limit: int = 1000) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(
            "SELECT id, name, variant_id FROM products "
            "WHERE dead_at IS NULL "
            "ORDER BY name LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]

    async def list_activities(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(
            "SELECT id, name FROM activities ORDER BY name LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]

    async def stock_for_variant(self, variant_id: int) -> dict[str, Any] | None:
        row = await self._conn.fetchrow(
            "SELECT COALESCE(SUM(p.population_count), 0)::float AS total, "
            "MAX(p.updated_at) AS last_update "
            "FROM products p "
            "WHERE p.variant_id = $1 AND p.dead_at IS NULL",
            variant_id,
        )
        if row is None:
            return None
        return {
            "variant_id": variant_id,
            "total": float(row["total"]),
            "last_update": row["last_update"],
        }

    async def interventions_in_range(
        self,
        start: datetime,
        end: datetime,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(
            "SELECT id, procedure_name, started_at, stopped_at "
            "FROM interventions "
            "WHERE state IN ('done', 'validated') "
            "AND started_at >= $1 AND started_at < $2 "
            "ORDER BY started_at DESC LIMIT $3",
            start,
            end,
            limit,
        )
        return [dict(r) for r in rows]

    async def land_parcels_for_intervention(self, intervention_id: int) -> list[dict[str, Any]]:
        rows = await self._conn.fetch(
            "SELECT p.id, p.name "
            "FROM intervention_targets t "
            "JOIN products p ON p.id = t.product_id "
            "WHERE t.intervention_id = $1 AND p.type = 'LandParcel'",
            intervention_id,
        )
        return [dict(r) for r in rows]
