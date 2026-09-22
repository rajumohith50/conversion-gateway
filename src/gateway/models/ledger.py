"""SQLAlchemy ORM for the event ledger. Design section 4.

Two tables:

  events             one row per event, holding its CURRENT state
  event_transitions  append-only log of every state change with a reason

"Where did event X end up and why" is `SELECT status, status_reason FROM
events`; "how did it get there" is the transitions table. Both are in
Postgres because the design (section 10) wants one dependency and a single
transaction around "write the row and record the transition".
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    # Namespaced CRM event id, e.g. "salesforce:00Q5e00000ABC". Primary key
    # is the idempotency key: a duplicate delivery hits the constraint.
    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Generated at ingest and threaded through every log line the event
    # produces in any process. Persisted so the worker and uploader can
    # bind it without the API having to pass it on the queue message alone.
    correlation_id: Mapped[str | None] = mapped_column(String(32))

    # Nullable because a REJECTED row may not have parsed far enough to
    # know any of these.
    conversion_action: Mapped[str | None] = mapped_column(String(255))
    conversion_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conversion_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    currency: Mapped[str | None] = mapped_column(String(3))

    match_key_type: Mapped[str | None] = mapped_column(String(32))
    # Not in the section 4 table, but the click-id path cannot upload
    # without it. It identifies an ad click, not a person, so storing it
    # does not violate the no-raw-PII rule.
    click_id: Mapped[str | None] = mapped_column(String(255))
    # Digests only. Written by the processor in phase 3; NULL until then.
    hashed_identifiers: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    consent_ad_user_data: Mapped[str | None] = mapped_column(String(16))
    consent_ad_personalization: Mapped[str | None] = mapped_column(String(16))

    # Lifecycle state as plain strings, not a Postgres ENUM type. Adding a
    # state to a native enum is an ALTER TYPE migration that cannot run
    # inside a transaction on older Postgres; a VARCHAR checked by the
    # Python enum at write time avoids that for no real loss.
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(String(512))

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    transitions: Mapped[list["EventTransition"]] = relationship(
        back_populates="event", order_by="EventTransition.id", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # The operational queries: "everything stuck in QUEUED older than X"
        # and "rejection rate by source".
        Index("ix_events_status_received_at", "status", "received_at"),
        Index("ix_events_source_status", "source", "status"),
    )


class EventTransition(Base):
    __tablename__ = "event_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False
    )
    # NULL from_status marks the row's creation.
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(512))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    event: Mapped[Event] = relationship(back_populates="transitions")

    __table_args__ = (Index("ix_event_transitions_event_id", "event_id"),)


class DeadLetter(Base):
    """One row per dead-lettering. Design section 7: the normalised payload,
    the failure class, the platform's error, and the attempt history.

    Kept after replay (replayed_at set, replay_count bumped) so the record
    of what failed and why survives the fix. An event dead-lettered twice
    has two rows.
    """

    __tablename__ = "dead_letters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False
    )
    # Denormalised from events so `dlq list` filters without a join.
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    conversion_action: Mapped[str | None] = mapped_column(String(255))
    dead_lettered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # partial | permanent | poison
    failure_class: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    platform_error_code: Mapped[str | None] = mapped_column(String(128))
    platform_error_message: Mapped[str | None] = mapped_column(String(1024))
    # The exact conversion row that was (or would have been) sent: digests
    # and unhashed-by-spec fields only. Never raw PII.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # [{"at": iso, "status": ..., "reason": ...}, ...] from the ledger's
    # transition log, so the DLQ row is self-contained.
    attempt_history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replay_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_dead_letters_event_id", "event_id"),
        Index("ix_dead_letters_open", "replayed_at", "failure_class", "dead_lettered_at"),
    )
