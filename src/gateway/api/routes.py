"""HTTP routes. Thin: authenticate, parse, map, hand to the ledger, respond.

Endpoints are `def`, not `async def`. They do blocking database work, and
FastAPI runs sync endpoints in a thread pool, which is exactly what we want.
The one async piece is the body-reading dependency, because reading the
request body is the only genuinely async operation here.
"""

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import text

from gateway import ledger
from gateway.api import mappers
from gateway.api.signature import verify
from gateway.config import Settings
from gateway.models.status import EventStatus, Source
from gateway.queue import QueueMessage, QueuePublisher

log = logging.getLogger(__name__)
router = APIRouter()

TIMESTAMP_HEADER = "X-Webhook-Timestamp"
SIGNATURE_HEADER = "X-Webhook-Signature"


# --- Response models ---------------------------------------------------------


class AcceptedResponse(BaseModel):
    event_id: str
    status: EventStatus


class RejectedResponse(BaseModel):
    event_id: str
    status: EventStatus
    errors: list[dict[str, str]]


class EventResponse(BaseModel):
    event_id: str
    source: str
    status: EventStatus
    status_reason: str | None
    received_at: datetime
    conversion_action: str | None
    match_key_type: str | None
    attempt_count: int
    transitions: list["TransitionResponse"]


class TransitionResponse(BaseModel):
    from_status: str | None
    to_status: str
    reason: str | None
    occurred_at: datetime


# --- Dependencies ------------------------------------------------------------


def _source_or_404(source: str) -> Source:
    try:
        return Source(source)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown source") from None


def _secret_for(settings: Settings, source: Source) -> str:
    if source is Source.SALESFORCE:
        return settings.webhook_secret_salesforce
    return settings.webhook_secret_hubspot


async def verified_body(request: Request, source: str) -> bytes:
    """Read the raw body and verify its signature before anything parses it.

    Signature failures never touch the ledger: the body is untrusted until
    this passes, and writing unauthenticated junk to the database would let
    anyone fill it.
    """
    src = _source_or_404(source)
    settings: Settings = request.app.state.settings
    body = await request.body()
    failure = verify(
        secret=_secret_for(settings, src),
        timestamp_header=request.headers.get(TIMESTAMP_HEADER),
        signature_header=request.headers.get(SIGNATURE_HEADER),
        body=body,
        now=time.time(),
        tolerance_seconds=settings.webhook_timestamp_tolerance_seconds,
    )
    if failure is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"reason": failure.value})
    return body


# --- Routes ------------------------------------------------------------------


@router.post(
    "/webhooks/crm/{source}",
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"model": AcceptedResponse}, 422: {"model": RejectedResponse}},
)
def ingest(
    source: str, request: Request, response: Response, body: bytes = Depends(verified_body)
) -> AcceptedResponse | RejectedResponse:
    src = _source_or_404(source)
    now = datetime.now(UTC)
    session_factory = request.app.state.session_factory

    # Two ways to fail validation: not JSON at all, or JSON of the wrong
    # shape. Both are authenticated requests, so both are recorded.
    payload: Any
    try:
        payload = json.loads(body)
    except ValueError:
        return _reject(
            session_factory,
            response,
            src,
            body,
            None,
            [{"field": "body", "reason": "malformed_json"}],
            now,
        )

    try:
        event = mappers.parse_and_map(src, payload)
    except ValidationError as exc:
        return _reject(
            session_factory,
            response,
            src,
            body,
            mappers.extract_source_event_id(src, payload),
            mappers.rejection_reasons(exc),
            now,
        )

    with session_factory.begin() as session:
        row = ledger.record_validated(session, event, now)
        if row is None:
            # Duplicate delivery. 200 rather than 202: nothing new was
            # accepted, and the client gets the id it already has. No
            # publish either: the original delivery already did that.
            response.status_code = status.HTTP_200_OK
            existing = ledger.get_event(session, event.event_id)
            assert existing is not None
            return AcceptedResponse(event_id=existing.event_id, status=EventStatus(existing.status))
        accepted = AcceptedResponse(event_id=row.event_id, status=EventStatus(row.status))

    # Publish AFTER the commit. There is no transaction that spans Postgres
    # and the queue, so one of the two must go first, and the durable one
    # should: a row without a message is found by `gateway reconcile`,
    # whereas a message without a row is an orphan the worker can only
    # drop. The identifiers ride on the message and nowhere else.
    publisher: QueuePublisher = request.app.state.publisher
    try:
        publisher.publish(
            QueueMessage(event_id=event.event_id, identifiers=event.identifiers, enqueued_at=now)
        )
    except Exception:
        # The event is durable and QUEUED; still 202. Logged at error level
        # because a run of these means the queue is down, not the client.
        log.exception("publish failed; event left QUEUED", extra={"event_id": event.event_id})
    return accepted


def _reject(
    session_factory: Any,
    response: Response,
    source: Source,
    body: bytes,
    source_event_id: str | None,
    errors: list[dict[str, str]],
    now: datetime,
) -> RejectedResponse:
    """Record a validation failure as a REJECTED row and answer 422."""
    if source_event_id is None:
        # No usable id in the payload. Key the row on the body digest so the
        # same garbage redelivered still produces exactly one row.
        source_event_id = "unparsed:" + hashlib.sha256(body).hexdigest()[:16]
    event_id = mappers.make_event_id(source, source_event_id)
    # status_reason is a compact "reason:field;reason:field" string so it
    # fits one column and can be grouped on.
    reason = ";".join(f"{e['reason']}:{e['field']}" for e in errors)[:512]

    with session_factory.begin() as session:
        ledger.record_rejected(session, event_id, source.value, source_event_id, reason, now)

    response.status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return RejectedResponse(event_id=event_id, status=EventStatus.REJECTED, errors=errors)


@router.get("/events/{event_id}")
def get_event(event_id: str, request: Request) -> EventResponse:
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        row = ledger.get_event(session, event_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown event")
        transitions = ledger.list_transitions(session, event_id)
        return EventResponse(
            event_id=row.event_id,
            source=row.source,
            status=EventStatus(row.status),
            status_reason=row.status_reason,
            received_at=row.received_at,
            conversion_action=row.conversion_action,
            match_key_type=row.match_key_type,
            attempt_count=row.attempt_count,
            transitions=[
                TransitionResponse(
                    from_status=t.from_status,
                    to_status=t.to_status,
                    reason=t.reason,
                    occurred_at=t.occurred_at,
                )
                for t in transitions
            ],
        )


@router.get("/healthz")
def healthz() -> dict[str, str]:
    # Liveness: the process is up and serving. No dependencies checked, so
    # a database outage does not make the orchestrator restart the API.
    return {"status": "ok"}


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, str]:
    # Readiness: can we actually do work? A failed check takes the instance
    # out of the load balancer without killing it.
    session_factory = request.app.state.session_factory
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - any failure means not ready
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}
