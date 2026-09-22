"""HTTP routes. Thin: authenticate, parse, map, hand to the ledger, respond.

Endpoints are `def`, not `async def`. They do blocking database work, and
FastAPI runs sync endpoints in a thread pool, which is exactly what we want.
The one async piece is the body-reading dependency, because reading the
request body is the only genuinely async operation here.
"""

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ValidationError
from sqlalchemy import text

from gateway import ledger
from gateway.api import mappers
from gateway.api.signature import verify
from gateway.config import Settings
from gateway.models.status import EventStatus, Source
from gateway.observability import get_logger, metrics
from gateway.queue import QueueMessage, QueuePublisher

log = get_logger(__name__)
router = APIRouter()

TIMESTAMP_HEADER = "X-Webhook-Timestamp"
SIGNATURE_HEADER = "X-Webhook-Signature"


# --- Response models ---------------------------------------------------------


class AcceptedResponse(BaseModel):
    event_id: str
    status: EventStatus
    correlation_id: str


class RejectedResponse(BaseModel):
    event_id: str
    status: EventStatus
    correlation_id: str
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

    # One id per authenticated request, on every log line from here to
    # the upload, and returned to the caller so a support ticket can quote
    # it. Cleared in the finally so it cannot bleed into the next request
    # on this thread.
    correlation_id = uuid.uuid4().hex
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id, source=src.value)
    response.headers["X-Correlation-Id"] = correlation_id
    try:
        return _ingest(request, response, src, body, now, correlation_id, session_factory)
    finally:
        structlog.contextvars.clear_contextvars()


def _ingest(
    request: Request,
    response: Response,
    src: Source,
    body: bytes,
    now: datetime,
    correlation_id: str,
    session_factory: Any,
) -> AcceptedResponse | RejectedResponse:
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
            correlation_id,
        )

    try:
        event = mappers.parse_and_map(src, payload).model_copy(
            update={"correlation_id": correlation_id}
        )
    except ValidationError as exc:
        return _reject(
            session_factory,
            response,
            src,
            body,
            mappers.extract_source_event_id(src, payload),
            mappers.rejection_reasons(exc),
            now,
            correlation_id,
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
            log.info("duplicate delivery", event_id=existing.event_id)
            return AcceptedResponse(
                event_id=existing.event_id,
                status=EventStatus(existing.status),
                correlation_id=existing.correlation_id or correlation_id,
            )
        accepted = AcceptedResponse(
            event_id=row.event_id, status=EventStatus(row.status), correlation_id=correlation_id
        )
    # Counted after the insert so a duplicate delivery is not a new event.
    metrics.events_received_total.labels(
        source=src.value, conversion_action=event.conversion_action
    ).inc()

    # Publish AFTER the commit. There is no transaction that spans Postgres
    # and the queue, so one of the two must go first, and the durable one
    # should: a row without a message is found by `gateway reconcile`,
    # whereas a message without a row is an orphan the worker can only
    # drop. The identifiers ride on the message and nowhere else.
    publisher: QueuePublisher = request.app.state.publisher
    try:
        publisher.publish(
            QueueMessage(
                event_id=event.event_id,
                identifiers=event.identifiers,
                enqueued_at=now,
                correlation_id=correlation_id,
            )
        )
    except Exception:
        # The event is durable and QUEUED; still 202. Logged at error level
        # because a run of these means the queue is down, not the client.
        log.exception("publish failed; event left QUEUED", event_id=event.event_id)
    log.info("accepted", event_id=event.event_id, match_key_type=event.match_key_type.value)
    return accepted


def _reject(
    session_factory: Any,
    response: Response,
    source: Source,
    body: bytes,
    source_event_id: str | None,
    errors: list[dict[str, str]],
    now: datetime,
    correlation_id: str,
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
        row = ledger.record_rejected(session, event_id, source.value, source_event_id, reason, now)
        if row is not None:
            row.correlation_id = correlation_id
    metrics.events_received_total.labels(source=source.value, conversion_action="unknown").inc()
    for e in errors:
        metrics.events_rejected_total.labels(
            source=source.value, conversion_action="unknown", reason=e["reason"]
        ).inc()
    log.info("rejected", event_id=event_id, reason=reason)

    response.status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return RejectedResponse(
        event_id=event_id, status=EventStatus.REJECTED, correlation_id=correlation_id, errors=errors
    )


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


@router.get("/metrics")
def metrics_endpoint() -> Response:
    # A plain route rather than mounting the client library's ASGI app:
    # a mount at /metrics answers /metrics with a redirect to /metrics/,
    # which some scrapers do not follow.
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


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
    except Exception as exc:  # noqa: BLE001 - any failure means not ready
        # The response stays generic (no connection strings to a caller),
        # but the log says what actually went wrong.
        log.warning("readiness check failed", error=type(exc).__name__, detail=str(exc)[:200])
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}
