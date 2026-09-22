"""Ledger operations: the only code that writes to the events tables.

Every function takes an open Session and does not commit. The caller owns
the transaction boundary, which is what lets phase 3 put "write the row" and
"enqueue" inside one boundary. All functions take `now` explicitly rather
than calling datetime.now() so tests can pin timestamps.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from gateway.db import Session
from gateway.models.canonical import CanonicalLeadEvent
from gateway.models.ledger import Event, EventTransition
from gateway.models.status import ALLOWED_TRANSITIONS, EventStatus


class IllegalTransition(Exception):
    """Raised when code attempts a transition the state machine forbids.
    This is a programming error, not a data error: it should never be
    caught and turned into a 4xx."""

    def __init__(self, event_id: str, from_status: str, to_status: EventStatus) -> None:
        self.event_id = event_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"{event_id}: {from_status} -> {to_status} is not allowed")


def get_event(session: Session, event_id: str) -> Event | None:
    return session.get(Event, event_id)


def transition(
    session: Session, event: Event, to_status: EventStatus, reason: str | None, now: datetime
) -> None:
    """Move an event to a new state, recording the transition.

    Both the current-state column and the audit row change in the same
    session, so the ledger cannot end up with a status that has no
    transition explaining it.
    """
    from_status = EventStatus(event.status)
    if to_status not in ALLOWED_TRANSITIONS[from_status]:
        raise IllegalTransition(event.event_id, event.status, to_status)

    event.status = to_status.value
    event.status_reason = reason
    session.add(
        EventTransition(
            event_id=event.event_id,
            from_status=from_status.value,
            to_status=to_status.value,
            reason=reason,
            occurred_at=now,
        )
    )


def _insert_if_absent(session: Session, row: dict[str, object]) -> bool:
    """INSERT ... ON CONFLICT (event_id) DO NOTHING. Returns True if the row
    was inserted, False if it already existed.

    This is the idempotency mechanism. A SELECT-then-INSERT would have a
    window in which two concurrent deliveries of the same event both see
    "not present" and both insert; one would then fail with an
    IntegrityError after the other committed. Letting Postgres arbitrate
    at the constraint closes that window in one statement.
    """
    stmt = pg_insert(Event).values(**row).on_conflict_do_nothing().returning(Event.event_id)
    # RETURNING yields the row only if it was actually written; on conflict
    # it yields nothing. This is more reliable than rowcount, which some
    # driver paths report as -1 for statements with RETURNING.
    return session.execute(stmt).scalar_one_or_none() is not None


def record_validated(session: Session, event: CanonicalLeadEvent, now: datetime) -> Event | None:
    """Persist a freshly validated event as RECEIVED -> VALIDATED -> QUEUED.

    All three states land in one transaction. VALIDATED is never a resting
    state in practice, but recording it keeps the audit trail honest about
    the steps the event went through.

    Returns the new Event, or None if this event_id was already in the
    ledger (a duplicate delivery). On None the caller must not write
    anything else: the original row is the truth.

    Only fields that are not raw PII are written. `event.identifiers` is
    deliberately not referenced here.
    """
    inserted = _insert_if_absent(
        session,
        {
            "event_id": event.event_id,
            "source": event.source.value,
            "source_event_id": event.source_event_id,
            "received_at": now,
            "correlation_id": event.correlation_id,
            "conversion_action": event.conversion_action,
            "conversion_time": event.conversion_time,
            "conversion_value": event.conversion_value,
            "currency": event.currency,
            "match_key_type": event.match_key_type.value,
            "click_id": event.click_id,
            "consent_ad_user_data": _consent_value(event.consent.ad_user_data),
            "consent_ad_personalization": _consent_value(event.consent.ad_personalization),
            "status": EventStatus.RECEIVED.value,
            "status_reason": None,
            "attempt_count": 0,
        },
    )
    if not inserted:
        return None

    row = session.get(Event, event.event_id)
    assert row is not None  # we just inserted it in this transaction
    session.add(
        EventTransition(
            event_id=row.event_id,
            from_status=None,
            to_status=EventStatus.RECEIVED.value,
            reason=None,
            occurred_at=now,
        )
    )
    transition(session, row, EventStatus.VALIDATED, None, now)
    transition(session, row, EventStatus.QUEUED, None, now)
    return row


def record_rejected(
    session: Session, event_id: str, source: str, source_event_id: str, reason: str, now: datetime
) -> Event | None:
    """Persist a request that authenticated but failed validation, so it is
    queryable rather than lost in a 4xx. Idempotent on event_id like
    record_validated: the same bad payload redelivered makes one row."""
    inserted = _insert_if_absent(
        session,
        {
            "event_id": event_id,
            "source": source,
            "source_event_id": source_event_id,
            "received_at": now,
            "status": EventStatus.RECEIVED.value,
            "attempt_count": 0,
        },
    )
    if not inserted:
        return None

    row = session.get(Event, event_id)
    assert row is not None
    session.add(
        EventTransition(
            event_id=row.event_id,
            from_status=None,
            to_status=EventStatus.RECEIVED.value,
            reason=None,
            occurred_at=now,
        )
    )
    transition(session, row, EventStatus.REJECTED, reason, now)
    return row


def record_processed(
    session: Session, event: Event, hashed_identifiers: dict[str, Any], now: datetime
) -> None:
    """The worker's happy path: digests written, event moves to PROCESSED.
    This is the only function that writes hashed_identifiers."""
    event.hashed_identifiers = hashed_identifiers
    transition(session, event, EventStatus.PROCESSED, None, now)


def find_stuck_queued(session: Session, older_than: datetime) -> list[Event]:
    """Events that were marked QUEUED before `older_than` and are still
    QUEUED. With a healthy worker the queue drains in seconds, so anything
    past a sensible threshold most likely never made it onto the queue
    (publish failed after the ledger commit). The reconcile CLI uses this."""
    stmt = (
        select(Event)
        .where(Event.status == EventStatus.QUEUED.value, Event.received_at < older_than)
        .order_by(Event.received_at)
    )
    return list(session.scalars(stmt))


def claim_for_upload(
    session: Session, now: datetime, limit: int, retry_after: timedelta, stale_after: timedelta
) -> list[Event]:
    """Pick rows ready to upload and mark them UPLOADING in one transaction.

    Eligible:
      PROCESSED          hashed and never attempted
      FAILED_RETRYABLE   a row-level retryable error, once retry_after has
                         passed since the last attempt
      UPLOADING          stale: claimed longer than stale_after ago by an
                         uploader that must have died mid-batch

    FOR UPDATE SKIP LOCKED means two uploaders can run against the same
    ledger and never claim the same row: each skips rows the other has
    locked instead of blocking on them.
    """
    stmt = (
        select(Event)
        .where(
            or_(
                Event.status == EventStatus.PROCESSED.value,
                (Event.status == EventStatus.FAILED_RETRYABLE.value)
                & (Event.last_attempt_at <= now - retry_after),
                (Event.status == EventStatus.UPLOADING.value)
                & (Event.last_attempt_at <= now - stale_after),
            )
        )
        .order_by(Event.received_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = list(session.scalars(stmt))
    for event in claimed:
        if event.status != EventStatus.UPLOADING.value:
            transition(session, event, EventStatus.UPLOADING, None, now)
        else:
            # Re-claiming a stale UPLOADING row is not a state change, but
            # it is worth an audit line saying why the attempt count moved.
            session.add(
                EventTransition(
                    event_id=event.event_id,
                    from_status=event.status,
                    to_status=event.status,
                    reason="reclaimed_stale",
                    occurred_at=now,
                )
            )
        event.attempt_count += 1
        event.last_attempt_at = now
    return claimed


def record_retry_attempt(session: Session, event: Event, reason: str, now: datetime) -> None:
    """Between two attempts of the same batch: UPLOADING -> FAILED_RETRYABLE
    -> UPLOADING, so the ledger shows every attempt and why it failed."""
    transition(session, event, EventStatus.FAILED_RETRYABLE, reason, now)
    transition(session, event, EventStatus.UPLOADING, None, now)
    event.attempt_count += 1
    event.last_attempt_at = now


def list_transitions(session: Session, event_id: str) -> list[EventTransition]:
    stmt = (
        select(EventTransition)
        .where(EventTransition.event_id == event_id)
        .order_by(EventTransition.id)
    )
    return list(session.scalars(stmt))


def _consent_value(status: object) -> str | None:
    # ConsentStatus is a StrEnum; store its plain value or NULL for absent.
    return None if status is None else str(status)
