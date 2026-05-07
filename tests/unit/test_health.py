from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

from duke.main import create_app


@pytest.fixture
def app_without_lifespan() -> FastAPI:
    """Build the app but skip lifespan (tests inject their own state)."""
    app = create_app()
    app.router.lifespan_context = _noop_lifespan  # type: ignore[assignment]
    return app


@asynccontextmanager
async def _noop_lifespan(_app: FastAPI):
    yield


def _make_session_factory(should_fail: bool):
    """Return an async_sessionmaker-like callable usable with `async with sm() as session`."""

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def execute(self, _stmt):
            if should_fail:
                raise RuntimeError("duke db down")
            return MagicMock()

    return MagicMock(side_effect=lambda: _Session())


def _make_pool(should_fail: bool):
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        if should_fail:
            raise RuntimeError("eky db down")

        class _Conn:
            async def fetchval(self, _q):
                return 1

        yield _Conn()

    pool.acquire = acquire
    return pool


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_healthz_always_ok(app_without_lifespan: FastAPI) -> None:
    resp = await _get(app_without_lifespan, "/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_ok_when_dbs_up(app_without_lifespan: FastAPI) -> None:
    app_without_lifespan.state.duke_sessionmaker = _make_session_factory(should_fail=False)
    app_without_lifespan.state.ekylibre_pool = _make_pool(should_fail=False)

    resp = await _get(app_without_lifespan, "/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"checks": {"duke_db": "ok", "ekylibre_db": "ok"}}


@pytest.mark.asyncio
async def test_readyz_fail_when_duke_db_down(app_without_lifespan: FastAPI) -> None:
    app_without_lifespan.state.duke_sessionmaker = _make_session_factory(should_fail=True)
    app_without_lifespan.state.ekylibre_pool = _make_pool(should_fail=False)

    resp = await _get(app_without_lifespan, "/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["duke_db"] == "fail"
    assert resp.json()["checks"]["ekylibre_db"] == "ok"


@pytest.mark.asyncio
async def test_readyz_fail_when_ekylibre_db_down(app_without_lifespan: FastAPI) -> None:
    app_without_lifespan.state.duke_sessionmaker = _make_session_factory(should_fail=False)
    app_without_lifespan.state.ekylibre_pool = _make_pool(should_fail=True)

    resp = await _get(app_without_lifespan, "/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["ekylibre_db"] == "fail"
