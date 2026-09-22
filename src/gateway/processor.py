"""The processor: what happens to one queued event.

process_message() is the whole decision. It takes an open session and a
message and returns what it did; the worker loop around it owns ack/nack
and the transaction. Keeping the decision separate from the loop means the
consent truth table and the rejection cases are tested by calling one
function with a session, no queue involved.

Order of operations, per the design:
  1. consent gate (section 6) — before touching identifiers at all, so a
     denied user's PII is never even normalised
  2. normalise + hash (section 5) — any rejection is terminal
  3. persist digests, PROCESSED
"""

from datetime import datetime
from enum import StrEnum

from gateway import ledger
from gateway.consent import ConsentSignals, ConsentStatus, evaluate_consent
from gateway.db import Session
from gateway.models.ledger import Event
from gateway.models.status import EventStatus
from gateway.normalise import RawIdentifiers, RejectionReason, build_user_identifiers
from gateway.observability import get_logger, metrics
from gateway.queue import QueueMessage

log = get_logger(__name__)


class ProcessOutcome(StrEnum):
    PROCESSED = "processed"
    SUPPRESSED = "suppressed"
    REJECTED = "rejected"
    # At-least-once delivery means the same message can arrive twice. The
    # second time the row is no longer QUEUED and there is nothing to do.
    SKIPPED_NOT_QUEUED = "skipped_not_queued"
    # A message whose row does not exist: the API published but its ledger
    # commit failed. Nothing to transition; the CRM will have retried.
    SKIPPED_UNKNOWN_EVENT = "skipped_unknown_event"


def _consent_from_row(event: Event) -> ConsentSignals:
    # The ledger holds the consent strings as received; None for absent.
    return ConsentSignals(
        ad_user_data=ConsentStatus(event.consent_ad_user_data)
        if event.consent_ad_user_data
        else None,
        ad_personalization=ConsentStatus(event.consent_ad_personalization)
        if event.consent_ad_personalization
        else None,
    )


def process_message(session: Session, message: QueueMessage, now: datetime) -> ProcessOutcome:
    event = ledger.get_event(session, message.event_id)
    if event is None:
        log.warning("orphan message: no ledger row", event_id=message.event_id)
        return ProcessOutcome.SKIPPED_UNKNOWN_EVENT
    if event.status != EventStatus.QUEUED.value:
        return ProcessOutcome.SKIPPED_NOT_QUEUED

    labels = {"source": event.source, "conversion_action": metrics.unknown(event.conversion_action)}

    decision = evaluate_consent(_consent_from_row(event))
    if not decision.permitted:
        assert decision.reason is not None
        ledger.transition(session, event, EventStatus.SUPPRESSED, decision.reason.value, now)
        metrics.events_suppressed_total.labels(**labels, reason=decision.reason.value).inc()
        log.info("suppressed", event_id=event.event_id, reason=decision.reason.value)
        return ProcessOutcome.SUPPRESSED

    if message.identifiers == RawIdentifiers() and event.hashed_identifiers is not None:
        # A replayed event: the raw identifiers are gone but the digests
        # are already on the row. Re-running the consent gate above is
        # right; re-running normalisation on nothing would wipe them.
        ledger.record_processed(session, event, event.hashed_identifiers, now)
        log.info("processed (replay, digests reused)", event_id=event.event_id)
        return ProcessOutcome.PROCESSED

    result = build_user_identifiers(message.identifiers)
    # A click id is itself a complete match key, so "no identifiers" is
    # not a problem for a click-id event. Any *other* rejection still is:
    # a malformed phone is a data-quality fault regardless of match path.
    rejections = [
        r
        for r in result.rejections
        if not (event.click_id and r.reason is RejectionReason.NO_MATCH_KEY)
    ]
    if rejections:
        # Same "reason:field" shape the API uses for schema rejections, so
        # both kinds group together in the ledger.
        reason = ";".join(f"{r.reason.value}:{r.field}" for r in rejections)[:512]
        ledger.transition(session, event, EventStatus.REJECTED, reason, now)
        for r in rejections:
            metrics.events_rejected_total.labels(**labels, reason=r.reason.value).inc()
        log.info("rejected", event_id=event.event_id, reason=reason)
        return ProcessOutcome.REJECTED

    ledger.record_processed(session, event, result.identifiers.model_dump(), now)
    log.info("processed", event_id=event.event_id)
    return ProcessOutcome.PROCESSED
