"""Each terminal state increments its counter with the right labels, and
/metrics exposes them."""

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from gateway import ledger, worker
from gateway.api import mappers
from gateway.models.status import Source
from gateway.observability import metrics as m
from gateway.processor import process_message
from gateway.queue import MemoryQueue, QueueMessage
from gateway.upload.errors import PermanentUploadError, TransientUploadError
from gateway.upload.fake import FakeUploadClient, with_failures
from tests.api.payloads import salesforce_payload
from tests.conftest import SALESFORCE_SECRET, signed_headers
from tests.upload.test_uploader import make_uploader, processed_events

T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
SF = {"source": "salesforce", "conversion_action": "closed_won"}


def _post(client: TestClient, payload: dict) -> object:  # type: ignore[type-arg]
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/crm/salesforce", content=body, headers=signed_headers(SALESFORCE_SECRET, body)
    )


def test_received_increments_on_accept_not_on_duplicate(client: TestClient) -> None:
    before = m.read_counter(m.events_received_total, **SF)
    _post(client, salesforce_payload())
    _post(client, salesforce_payload())  # duplicate
    assert m.read_counter(m.events_received_total, **SF) == before + 1


def test_schema_rejection_increments_rejected_with_reason(client: TestClient) -> None:
    labels = {"source": "salesforce", "conversion_action": "unknown", "reason": "missing"}
    before = m.read_counter(m.events_rejected_total, **labels)
    payload = salesforce_payload()
    del payload["Lead"]["Id"]
    _post(client, payload)
    assert m.read_counter(m.events_rejected_total, **labels) == before + 1


def test_malformed_body_is_a_rejection_too(client: TestClient) -> None:
    labels = {"source": "salesforce", "conversion_action": "unknown", "reason": "malformed_json"}
    before = m.read_counter(m.events_rejected_total, **labels)
    body = b"{"
    client.post(
        "/webhooks/crm/salesforce", content=body, headers=signed_headers(SALESFORCE_SECRET, body)
    )
    assert m.read_counter(m.events_rejected_total, **labels) == before + 1


def _queued(session_factory: sessionmaker[Session], **lead: object) -> QueueMessage:
    payload = salesforce_payload()
    payload["Lead"].update(lead)
    event = mappers.parse_and_map(Source.SALESFORCE, payload)
    with session_factory.begin() as session:
        ledger.record_validated(session, event, T0)
    return QueueMessage(event_id=event.event_id, identifiers=event.identifiers, enqueued_at=T0)


def test_suppressed_increments_with_consent_reason(session_factory: sessionmaker[Session]) -> None:
    labels = {**SF, "reason": "ad_user_data_denied"}
    before = m.read_counter(m.events_suppressed_total, **labels)
    msg = _queued(session_factory, Consent_Ad_User_Data__c="DENIED")
    with session_factory.begin() as session:
        process_message(session, msg, T0)
    assert m.read_counter(m.events_suppressed_total, **labels) == before + 1


def test_normalisation_rejection_increments_per_field_reason(
    session_factory: sessionmaker[Session],
) -> None:
    labels = {**SF, "reason": "unparseable"}
    before = m.read_counter(m.events_rejected_total, **labels)
    msg = _queued(session_factory, Phone="nope")
    with session_factory.begin() as session:
        process_message(session, msg, T0)
    assert m.read_counter(m.events_rejected_total, **labels) == before + 1


def test_uploaded_increments_and_observes_ingest_to_upload(
    session_factory: sessionmaker[Session],
) -> None:
    before = m.read_counter(m.conversions_uploaded_total, **SF)
    before_hist = m.histogram_count(m.ingest_to_upload_seconds, **SF)
    before_lat = m.histogram_count(m.upload_latency_seconds, outcome="ok")
    processed_events(session_factory, 2)
    make_uploader(session_factory, FakeUploadClient()).run_once(T0)
    assert m.read_counter(m.conversions_uploaded_total, **SF) == before + 2
    assert m.histogram_count(m.ingest_to_upload_seconds, **SF) == before_hist + 2
    assert m.histogram_count(m.upload_latency_seconds, outcome="ok") == before_lat + 1


def _fresh_processed(session_factory: sessionmaker[Session], prefix: str, n: int) -> None:
    with session_factory.begin() as session:
        for i in range(n):
            event = mappers.parse_and_map(
                Source.SALESFORCE, salesforce_payload(EventId=f"{prefix}-{i}")
            )
            ledger.record_validated(session, event, T0)
            process_message(
                session,
                QueueMessage(
                    event_id=event.event_id, identifiers=event.identifiers, enqueued_at=T0
                ),
                T0,
            )


def test_dead_lettered_increments_by_failure_class(session_factory: sessionmaker[Session]) -> None:
    partial = {**SF, "failure_class": "partial"}
    permanent = {**SF, "failure_class": "permanent"}
    poison = {**SF, "failure_class": "poison"}
    b_partial = m.read_counter(m.conversions_dead_lettered_total, **partial)
    b_perm = m.read_counter(m.conversions_dead_lettered_total, **permanent)
    b_poison = m.read_counter(m.conversions_dead_lettered_total, **poison)
    b_lat_t = m.histogram_count(m.upload_latency_seconds, outcome="transient")
    b_lat_p = m.histogram_count(m.upload_latency_seconds, outcome="permanent")

    _fresh_processed(session_factory, "a", 2)
    client = FakeUploadClient([with_failures(2, {1: "EXPIRED_CLICK"})])
    make_uploader(session_factory, client).run_once(T0)
    assert m.read_counter(m.conversions_dead_lettered_total, **partial) == b_partial + 1

    _fresh_processed(session_factory, "b", 1)
    client = FakeUploadClient([PermanentUploadError("http_401")])
    make_uploader(session_factory, client).run_once(T0)
    assert m.read_counter(m.conversions_dead_lettered_total, **permanent) == b_perm + 1
    assert m.histogram_count(m.upload_latency_seconds, outcome="permanent") == b_lat_p + 1

    _fresh_processed(session_factory, "c", 2)
    client = FakeUploadClient([TransientUploadError("http_503")] * 5)
    make_uploader(session_factory, client).run_once(T0)
    assert m.read_counter(m.conversions_dead_lettered_total, **poison) == b_poison + 2
    assert m.histogram_count(m.upload_latency_seconds, outcome="transient") == b_lat_t + 3


def test_metrics_endpoint_exposes_every_section_8_metric(client: TestClient) -> None:
    _post(client, salesforce_payload())
    text = client.get("/metrics").text
    for name in (
        "events_received_total",
        "events_rejected_total",
        "events_suppressed_total",
        "conversions_uploaded_total",
        "conversions_dead_lettered_total",
        "upload_latency_seconds",
        "dlq_depth",
        "ingest_to_upload_seconds",
    ):
        assert name in text, name
    assert 'events_received_total{conversion_action="closed_won",source="salesforce"}' in text


def test_worker_binds_correlation_id_and_clears_it(
    session_factory: sessionmaker[Session],
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    import structlog

    from gateway.observability.logging import configure_logging

    configure_logging("INFO")
    try:
        queue = MemoryQueue()
        msg = _queued(session_factory).model_copy(update={"correlation_id": "corr-xyz"})
        queue.publish(msg)
        [delivery] = queue.receive(1, 0)
        worker.handle_delivery(queue, session_factory, delivery)
        raw = capsys.readouterr().out.splitlines()
        lines = [json.loads(line) for line in raw if line.startswith("{")]
        processed = [line for line in lines if line["event"] == "processed"]
        assert processed and processed[0]["correlation_id"] == "corr-xyz"
        assert structlog.contextvars.get_contextvars() == {}
    finally:
        structlog.reset_defaults()


def test_metrics_route_answers_without_redirect(client: TestClient) -> None:
    resp = client.get("/metrics", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


def test_dlq_depth_returns_to_zero_after_replay(session_factory: sessionmaker[Session]) -> None:
    from gateway.dlq import store as dlq
    from gateway.upload.errors import PermanentUploadError

    _fresh_processed(session_factory, "z", 1)
    uploader = make_uploader(session_factory, FakeUploadClient([PermanentUploadError("http_401")]))
    uploader.run_once(T0)
    assert m.read_gauge(m.dlq_depth, source="salesforce") == 1
    with session_factory.begin() as session:
        [row] = dlq.list_dead_letters(session, dlq.DlqFilter())
        dlq.replay(session, row, MemoryQueue(), T0)
    # Next upload cycle refreshes the gauge even with nothing to upload.
    uploader._refresh_dlq_depth()
    assert m.read_gauge(m.dlq_depth, source="salesforce") == 0
