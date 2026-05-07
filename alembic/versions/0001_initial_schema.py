"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-07

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    turn_role = postgresql.ENUM("user", "assistant", "system", name="turn_role", create_type=True)
    turn_outcome = postgresql.ENUM(
        "ok", "ambiguity", "error", "cancelled", name="turn_outcome", create_type=True
    )
    turn_role.create(op.get_bind(), checkfirst=True)
    turn_outcome.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "conversation_session",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_hash", sa.Text(), nullable=False),
        sa.Column("user_hash", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("llm_provider", sa.String(length=32)),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index(
        "ix_conversation_session_tenant_hash",
        "conversation_session",
        ["tenant_hash"],
    )
    op.create_index(
        "ix_conversation_session_user_hash",
        "conversation_session",
        ["user_hash"],
    )

    op.create_table(
        "conversation_turn",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "role",
            postgresql.ENUM(name="turn_role", create_type=False),
            nullable=False,
        ),
        sa.Column("text", sa.Text()),
        sa.Column("intent", sa.String(length=64)),
        sa.Column(
            "entities",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("llm_tokens_in", sa.Integer()),
        sa.Column("llm_tokens_out", sa.Integer()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column(
            "outcome",
            postgresql.ENUM(name="turn_outcome", create_type=False),
        ),
    )
    op.create_index("ix_conversation_turn_session_id", "conversation_turn", ["session_id"])

    op.create_table(
        "intervention_draft",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation_turn.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("draft", postgresql.JSONB(), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("ekylibre_intervention_id", sa.Integer()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_intervention_draft_session_id", "intervention_draft", ["session_id"])
    op.create_index("ix_intervention_draft_turn_id", "intervention_draft", ["turn_id"])

    op.create_table(
        "audit_event",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation_session.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "details",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index("ix_audit_event_event_type", "audit_event", ["event_type"])
    op.create_index("ix_audit_event_session_id", "audit_event", ["session_id"])


def downgrade() -> None:
    op.drop_table("audit_event")
    op.drop_table("intervention_draft")
    op.drop_table("conversation_turn")
    op.drop_table("conversation_session")
    op.execute("DROP TYPE IF EXISTS turn_outcome")
    op.execute("DROP TYPE IF EXISTS turn_role")
