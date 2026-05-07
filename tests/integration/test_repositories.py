"""Repository integration tests against a throwaway Postgres."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from duke.integration.store.models import (
    AuditEvent,
    Base,
    ConversationSession,
    ConversationTurn,
    InterventionDraft,
    TurnOutcome,
    TurnRole,
)
from duke.integration.store.repositories import ConversationRepository

pytestmark = pytest.mark.integration

try:
    from testcontainers.postgres import PostgresContainer

    _HAS_TC = True
except Exception:  # pragma: no cover
    _HAS_TC = False


def _async_dsn(container: PostgresContainer) -> str:
    user = container.username
    password = container.password
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    db = container.dbname
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


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


@pytest.fixture
def repo(sessionmaker) -> ConversationRepository:
    return ConversationRepository(sessionmaker)


async def test_start_and_end_session(repo: ConversationRepository, sessionmaker) -> None:
    session_id = await repo.start_session(
        tenant_hash="abc",
        user_hash="def",
        llm_provider="claude",
    )
    assert isinstance(session_id, uuid.UUID)

    await repo.end_session(session_id)

    async with sessionmaker() as db:
        row = (
            await db.execute(
                select(ConversationSession).where(ConversationSession.id == session_id)
            )
        ).scalar_one()
        assert row.tenant_hash == "abc"
        assert row.user_hash == "def"
        assert row.llm_provider == "claude"
        assert row.ended_at is not None


async def test_record_turn_with_outcome(repo: ConversationRepository, sessionmaker) -> None:
    sid = await repo.start_session(tenant_hash="t", user_hash="u", llm_provider=None)
    turn_id = await repo.record_turn(
        session_id=sid,
        role=TurnRole.USER,
        text="combien de Karaté ?",
        intent="qa_stock",
        latency_ms=420,
        outcome=TurnOutcome.OK,
    )

    async with sessionmaker() as db:
        row = (
            await db.execute(select(ConversationTurn).where(ConversationTurn.id == turn_id))
        ).scalar_one()
        assert row.role == TurnRole.USER
        assert row.intent == "qa_stock"
        assert row.text == "combien de Karaté ?"
        assert row.latency_ms == 420
        assert row.outcome == TurnOutcome.OK


async def test_record_and_confirm_intervention_draft(
    repo: ConversationRepository, sessionmaker
) -> None:
    sid = await repo.start_session(tenant_hash="t", user_hash="u", llm_provider="claude")
    turn_id = await repo.record_turn(session_id=sid, role=TurnRole.USER, text="...")
    draft_id = await repo.record_intervention_draft(
        session_id=sid,
        turn_id=turn_id,
        draft={"procedure_name": "spraying"},
    )

    await repo.mark_draft_confirmed(draft_id=draft_id, ekylibre_intervention_id=999)

    async with sessionmaker() as db:
        row = (
            await db.execute(select(InterventionDraft).where(InterventionDraft.id == draft_id))
        ).scalar_one()
        assert row.confirmed is True
        assert row.ekylibre_intervention_id == 999
        assert row.confirmed_at is not None
        assert row.draft == {"procedure_name": "spraying"}


async def test_record_audit(repo: ConversationRepository, sessionmaker) -> None:
    sid = await repo.start_session(tenant_hash="t", user_hash="u", llm_provider=None)
    await repo.record_audit(
        event_type="test.event",
        session_id=sid,
        details={"foo": "bar"},
    )

    async with sessionmaker() as db:
        row = (
            await db.execute(select(AuditEvent).where(AuditEvent.event_type == "test.event"))
        ).scalar_one()
        assert row.session_id == sid
        assert row.details == {"foo": "bar"}
