"""Retention job integration test."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from duke.integration.store.models import AuditEvent, Base, ConversationTurn, TurnRole
from duke.integration.store.retention import purge_old_turn_text

pytestmark = pytest.mark.integration

try:
    from testcontainers.postgres import PostgresContainer

    _HAS_TC = True
except Exception:  # pragma: no cover
    _HAS_TC = False


def _async_dsn(container: PostgresContainer) -> str:
    return (
        f"postgresql+asyncpg://{container.username}:{container.password}@"
        f"{container.get_container_host_ip()}:{container.get_exposed_port(5432)}/{container.dbname}"
    )


@pytest.fixture(scope="module")
def pg_container():
    if not _HAS_TC:
        pytest.skip("testcontainers not installed")
    with PostgresContainer("postgres:16-alpine") as c:
        yield c


@pytest.fixture
async def sessionmaker(pg_container):
    engine = create_async_engine(_async_dsn(pg_container))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sm
    finally:
        await engine.dispose()


async def _seed_turn(
    sessionmaker,
    *,
    text_value: str,
    occurred_at: datetime,
) -> None:
    from duke.integration.store.models import ConversationSession

    async with sessionmaker() as db:
        session = ConversationSession(tenant_hash="t", user_hash="u")
        db.add(session)
        await db.commit()
        await db.refresh(session)

        turn = ConversationTurn(
            session_id=session.id,
            role=TurnRole.USER,
            text=text_value,
        )
        db.add(turn)
        await db.commit()

        # Backdate manually so we exercise the cutoff branch.
        await db.execute(
            text("UPDATE conversation_turn SET occurred_at = :ts WHERE id = :id").bindparams(
                ts=occurred_at, id=turn.id
            )
        )
        await db.commit()


async def test_purges_old_text_keeps_recent(sessionmaker) -> None:
    now = datetime.now(UTC)
    await _seed_turn(
        sessionmaker, text_value="ancient secret", occurred_at=now - timedelta(days=120)
    )
    await _seed_turn(sessionmaker, text_value="recent text", occurred_at=now - timedelta(days=5))

    purged = await purge_old_turn_text(sessionmaker, retention_days=90, now=now)
    assert purged == 1

    async with sessionmaker() as db:
        rows = (
            (await db.execute(select(ConversationTurn).order_by(ConversationTurn.occurred_at)))
            .scalars()
            .all()
        )
        assert rows[0].text is None  # the old one
        assert rows[1].text == "recent text"

        audits = (await db.execute(select(AuditEvent))).scalars().all()
        assert len(audits) == 1
        assert audits[0].event_type == "retention.purge_turn_text"
        assert audits[0].details["purged_rows"] == 1


async def test_purge_is_idempotent(sessionmaker) -> None:
    now = datetime.now(UTC)
    await _seed_turn(sessionmaker, text_value="old", occurred_at=now - timedelta(days=120))

    first = await purge_old_turn_text(sessionmaker, retention_days=90, now=now)
    second = await purge_old_turn_text(sessionmaker, retention_days=90, now=now)
    assert first == 1
    assert second == 0


async def test_invalid_retention_rejected(sessionmaker) -> None:
    with pytest.raises(ValueError):
        await purge_old_turn_text(sessionmaker, retention_days=0)
