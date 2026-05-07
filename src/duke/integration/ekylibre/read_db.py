from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import structlog

log = structlog.get_logger(__name__)

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

    async def list_land_parcels(self) -> list[dict[str, Any]]:
        # TODO iteration 2: real query against Ekylibre tenant schema (LandParcel model).
        return []

    async def list_products(self) -> list[dict[str, Any]]:
        # TODO iteration 2: real query against Ekylibre product variants.
        return []
