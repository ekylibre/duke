"""RGPD retention job.

Anonymizes `conversation_turn.text` after `RETENTION_DAYS_TURN_TEXT` and emits
an audit row per run. Metadata (intent, entity counts, latencies) is preserved
to allow continuous NLU evaluation without keeping personal data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from duke.integration.store.models import AuditEvent, ConversationTurn

log = structlog.get_logger(__name__)


async def purge_old_turn_text(
    sessionmaker: async_sessionmaker,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> int:
    """Set `text=NULL` on turns older than retention_days.

    Returns the number of rows updated. Idempotent: already-purged rows
    (text already NULL) are not counted because the WHERE clause filters them.
    """
    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")

    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)

    async with sessionmaker() as db:
        result = await db.execute(
            update(ConversationTurn)
            .where(
                ConversationTurn.occurred_at < cutoff,
                ConversationTurn.text.is_not(None),
            )
            .values(text=None)
        )
        purged = result.rowcount or 0

        db.add(
            AuditEvent(
                event_type="retention.purge_turn_text",
                details={
                    "retention_days": retention_days,
                    "cutoff": cutoff.isoformat(),
                    "purged_rows": purged,
                },
            )
        )
        await db.commit()

    log.info("retention.purge_done", retention_days=retention_days, purged=purged)
    return purged
