"""SQLAlchemy 2.x async repository for the Duke audit log.

Records conversation sessions, individual turns, intervention drafts and
generic audit events. Errors are caught at the call site (best-effort
persistence: a Duke DB outage must not block the user-facing WS handler).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from duke.integration.store.models import (
    AuditEvent,
    ConversationSession,
    ConversationTurn,
    TurnOutcome,
    TurnRole,
)
from duke.integration.store.models import (
    InterventionDraft as InterventionDraftRow,
)

log = structlog.get_logger(__name__)


class ConversationRepository:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def start_session(
        self,
        *,
        tenant_hash: str,
        user_hash: str,
        llm_provider: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        row = ConversationSession(
            tenant_hash=tenant_hash,
            user_hash=user_hash,
            llm_provider=llm_provider,
            extra_metadata=metadata or {},
        )
        async with self._sessionmaker() as db:
            db.add(row)
            await db.commit()
            return row.id

    async def end_session(self, session_id: uuid.UUID) -> None:
        async with self._sessionmaker() as db:
            await db.execute(
                update(ConversationSession)
                .where(ConversationSession.id == session_id)
                .values(ended_at=datetime.now(UTC))
            )
            await db.commit()

    async def record_turn(
        self,
        *,
        session_id: uuid.UUID,
        role: TurnRole,
        text: str | None,
        intent: str | None = None,
        entities: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        llm_tokens_in: int | None = None,
        llm_tokens_out: int | None = None,
        outcome: TurnOutcome | None = None,
    ) -> uuid.UUID:
        row = ConversationTurn(
            session_id=session_id,
            role=role,
            text=text,
            intent=intent,
            entities=entities or {},
            latency_ms=latency_ms,
            llm_tokens_in=llm_tokens_in,
            llm_tokens_out=llm_tokens_out,
            outcome=outcome,
        )
        async with self._sessionmaker() as db:
            db.add(row)
            await db.commit()
            return row.id

    async def record_intervention_draft(
        self,
        *,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        draft: dict[str, Any],
        confirmed: bool = False,
        ekylibre_intervention_id: int | None = None,
    ) -> uuid.UUID:
        row = InterventionDraftRow(
            session_id=session_id,
            turn_id=turn_id,
            draft=draft,
            confirmed=confirmed,
            ekylibre_intervention_id=ekylibre_intervention_id,
            confirmed_at=datetime.now(UTC) if confirmed else None,
        )
        async with self._sessionmaker() as db:
            db.add(row)
            await db.commit()
            return row.id

    async def mark_draft_confirmed(
        self,
        *,
        draft_id: uuid.UUID,
        ekylibre_intervention_id: int,
    ) -> None:
        async with self._sessionmaker() as db:
            await db.execute(
                update(InterventionDraftRow)
                .where(InterventionDraftRow.id == draft_id)
                .values(
                    confirmed=True,
                    ekylibre_intervention_id=ekylibre_intervention_id,
                    confirmed_at=datetime.now(UTC),
                )
            )
            await db.commit()

    async def record_audit(
        self,
        *,
        event_type: str,
        session_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        row = AuditEvent(
            event_type=event_type,
            session_id=session_id,
            details=details or {},
        )
        async with self._sessionmaker() as db:
            db.add(row)
            await db.commit()
