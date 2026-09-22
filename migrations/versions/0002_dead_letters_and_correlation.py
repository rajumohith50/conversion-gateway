"""dead_letters table; events.correlation_id

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("correlation_id", sa.String(32)))

    op.create_table(
        "dead_letters",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "event_id",
            sa.String(255),
            sa.ForeignKey("events.event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("conversion_action", sa.String(255)),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_class", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column("platform_error_code", sa.String(128)),
        sa.Column("platform_error_message", sa.String(1024)),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("attempt_history", JSONB, nullable=False),
        sa.Column("replayed_at", sa.DateTime(timezone=True)),
        sa.Column("replay_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("ix_dead_letters_event_id", "dead_letters", ["event_id"])
    op.create_index(
        "ix_dead_letters_open", "dead_letters", ["replayed_at", "failure_class", "dead_lettered_at"]
    )


def downgrade() -> None:
    op.drop_table("dead_letters")
    op.drop_column("events", "correlation_id")
