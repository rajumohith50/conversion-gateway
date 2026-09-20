"""create ledger tables

Revision ID: 0001
Revises:
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_event_id", sa.String(255), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conversion_action", sa.String(255)),
        sa.Column("conversion_time", sa.DateTime(timezone=True)),
        sa.Column("conversion_value", sa.Numeric(18, 6)),
        sa.Column("currency", sa.String(3)),
        sa.Column("match_key_type", sa.String(32)),
        sa.Column("click_id", sa.String(255)),
        sa.Column("hashed_identifiers", JSONB),
        sa.Column("consent_ad_user_data", sa.String(16)),
        sa.Column("consent_ad_personalization", sa.String(16)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("status_reason", sa.String(512)),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_events_status_received_at", "events", ["status", "received_at"])
    op.create_index("ix_events_source_status", "events", ["source", "status"])

    op.create_table(
        "event_transitions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "event_id",
            sa.String(255),
            sa.ForeignKey("events.event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(512)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_transitions_event_id", "event_transitions", ["event_id"])


def downgrade() -> None:
    op.drop_table("event_transitions")
    op.drop_table("events")
