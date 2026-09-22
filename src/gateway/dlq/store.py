"""Writing to and replaying from the dead-letter store.

dead_letter() is the only way an event becomes DEAD_LETTERED: it performs
the ledger transition, writes the DLQ row, and increments the metric in
one place, so the three can never disagree.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from gateway import ledger
from gateway.db import Session
from gateway.models.ledger import DeadLetter, Event
from gateway.models.status import EventStatus
from gateway.normalise import RawIdentifiers
from gateway.observability import metrics
from gateway.queue import QueueMessage, QueuePublisher
from gateway.upload.classifier import FailureClass


def dead_letter(
    session: Session,
    event: Event,
    *,
    failure_class: FailureClass,
    reason: str,
    error_code: str | None,
    error_message: str | None,
    payload: dict[str, Any],
    now: datetime,
) -> DeadLetter:
    ledger.transition(session, event, EventStatus.DEAD_LETTERED, reason, now)
    history = [
        {"at": t.occurred_at.isoformat(), "status": t.to_status, "reason": t.reason}
        for t in ledger.list_transitions(session, event.event_id)
    ]
    row = DeadLetter(
        event_id=event.event_id,
        source=event.source,
        conversion_action=event.conversion_action,
        dead_lettered_at=now,
        failure_class=failure_class.value,
        reason=reason,
        platform_error_code=error_code,
        platform_error_message=(error_message or "")[:1024] or None,
        payload=payload,
        attempt_history=history,
    )
    session.add(row)
    metrics.conversions_dead_lettered_total.labels(
        source=event.source,
        conversion_action=metrics.unknown(event.conversion_action),
        failure_class=failure_class.value,
    ).inc()
    return row


@dataclass(frozen=True)
class DlqFilter:
    """What `dlq list` and `dlq replay --filter` select on. All optional
    and ANDed. `reason` is a prefix match so "permanent_row:" selects every
    row-level permanent failure and "permanent_row:CONVERSION_ACTION" a
    specific one."""

    event_id: str | None = None
    source: str | None = None
    failure_class: str | None = None
    reason_prefix: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    include_replayed: bool = False


def list_dead_letters(session: Session, flt: DlqFilter, limit: int = 100) -> list[DeadLetter]:
    stmt = select(DeadLetter).order_by(DeadLetter.dead_lettered_at.desc()).limit(limit)
    if not flt.include_replayed:
        stmt = stmt.where(DeadLetter.replayed_at.is_(None))
    if flt.event_id:
        stmt = stmt.where(DeadLetter.event_id == flt.event_id)
    if flt.source:
        stmt = stmt.where(DeadLetter.source == flt.source)
    if flt.failure_class:
        stmt = stmt.where(DeadLetter.failure_class == flt.failure_class)
    if flt.reason_prefix:
        stmt = stmt.where(DeadLetter.reason.startswith(flt.reason_prefix))
    if flt.since:
        stmt = stmt.where(DeadLetter.dead_lettered_at >= flt.since)
    if flt.until:
        stmt = stmt.where(DeadLetter.dead_lettered_at < flt.until)
    return list(session.scalars(stmt))


def get_dead_letter(session: Session, dlq_id: int) -> DeadLetter | None:
    return session.get(DeadLetter, dlq_id)


def open_depth_by_source(session: Session) -> dict[str, int]:
    stmt = (
        select(DeadLetter.source, func.count())
        .where(DeadLetter.replayed_at.is_(None))
        .group_by(DeadLetter.source)
    )
    return {source: int(count) for source, count in session.execute(stmt)}


def replay(session: Session, row: DeadLetter, publisher: QueuePublisher, now: datetime) -> bool:
    """Send one dead-lettered event back through the pipeline.

    DEAD_LETTERED -> QUEUED, then a queue message. The message carries no
    identifiers: the raw ones are long gone, and the digests are on the
    ledger row, which the processor reuses. attempt_count resets so the
    event gets a full retry budget after whatever was fixed; the DLQ row
    keeps the old history.

    Returns False if the row was already replayed or the event is no
    longer DEAD_LETTERED (someone else got there first).
    """
    if row.replayed_at is not None:
        return False
    event = ledger.get_event(session, row.event_id)
    if event is None or event.status != EventStatus.DEAD_LETTERED.value:
        return False

    ledger.transition(session, event, EventStatus.QUEUED, f"replayed:dlq:{row.id}", now)
    event.attempt_count = 0
    event.last_attempt_at = None
    row.replayed_at = now
    row.replay_count += 1
    # Publish after the ledger write, same ordering as ingest: if the
    # publish fails the row is durable and `reconcile` finds it.
    publisher.publish(
        QueueMessage(
            event_id=event.event_id,
            identifiers=RawIdentifiers(),
            enqueued_at=now,
            correlation_id=event.correlation_id,
        )
    )
    return True
