"""Webhook in, hashed ledger row out, through the in-memory queue."""

import json
import logging

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from gateway import ledger, worker
from gateway.api.app import create_app
from gateway.config import Settings
from gateway.normalise import sha256_hex
from gateway.queue import MemoryQueue, QueueMessage, QueuePublisher
from tests.api.payloads import hubspot_payload, salesforce_payload
from tests.conftest import HUBSPOT_SECRET, SALESFORCE_SECRET, signed_headers


def _post(client: TestClient, source: str, payload: dict, secret: str):  # type: ignore[no-untyped-def,type-arg]
    body = json.dumps(payload).encode()
    return client.post(
        f"/webhooks/crm/{source}", content=body, headers=signed_headers(secret, body)
    )


def test_webhook_to_hashed_ledger_row(
    client: TestClient, session_factory: sessionmaker[Session], queue: MemoryQueue
) -> None:
    # 1. Ingest: row is QUEUED and exactly one message was published.
    resp = _post(client, "salesforce", salesforce_payload(), SALESFORCE_SECRET)
    assert resp.status_code == 202
    assert resp.json()["status"] == "QUEUED"
    assert queue.ready_count() == 1

    # 2. The message carries the raw identifiers; the ledger does not.
    [delivery] = queue.receive(1, 0)
    assert delivery.message.event_id == "salesforce:e-sf-0001"
    assert delivery.message.identifiers.email == "M.Ohith+leads@Gmail.com"
    with session_factory() as session:
        row = ledger.get_event(session, "salesforce:e-sf-0001")
        assert row is not None
        assert row.hashed_identifiers is None

    # 3. Worker processes it: digests persisted, PROCESSED, message acked.
    assert worker.handle_delivery(queue, session_factory, delivery) is not None
    assert queue.inflight_count() == 0

    with session_factory() as session:
        row = ledger.get_event(session, "salesforce:e-sf-0001")
        assert row is not None
        assert row.status == "PROCESSED"
        assert row.hashed_identifiers is not None
        assert row.hashed_identifiers["hashed_email"] == sha256_hex("mohith@gmail.com")
        assert row.hashed_identifiers["hashed_phone"] == sha256_hex("+14155552671")
        assert [t.to_status for t in ledger.list_transitions(session, row.event_id)] == [
            "RECEIVED",
            "VALIDATED",
            "QUEUED",
            "PROCESSED",
        ]

    # 4. And the lifecycle is visible over HTTP.
    assert client.get("/events/salesforce:e-sf-0001").json()["status"] == "PROCESSED"


def test_hubspot_path_produces_identical_digests(
    client: TestClient, session_factory: sessionmaker[Session], queue: MemoryQueue
) -> None:
    _post(client, "hubspot", hubspot_payload(), HUBSPOT_SECRET)
    _post(client, "salesforce", salesforce_payload(), SALESFORCE_SECRET)
    for delivery in queue.receive(10, 0):
        worker.handle_delivery(queue, session_factory, delivery)
    with session_factory() as session:
        hs = ledger.get_event(session, "hubspot:987654321")
        sf = ledger.get_event(session, "salesforce:e-sf-0001")
        assert hs is not None and sf is not None
        assert hs.hashed_identifiers == sf.hashed_identifiers


def test_duplicate_webhook_publishes_once(
    client: TestClient, session_factory: sessionmaker[Session], queue: MemoryQueue
) -> None:
    for _ in range(3):
        _post(client, "salesforce", salesforce_payload(), SALESFORCE_SECRET)
    assert queue.ready_count() == 1


def test_consent_denied_webhook_ends_suppressed(
    client: TestClient, session_factory: sessionmaker[Session], queue: MemoryQueue
) -> None:
    payload = salesforce_payload()
    payload["Lead"]["Consent_Ad_User_Data__c"] = "DENIED"
    _post(client, "salesforce", payload, SALESFORCE_SECRET)
    [delivery] = queue.receive(1, 0)
    worker.handle_delivery(queue, session_factory, delivery)
    data = client.get("/events/salesforce:e-sf-0001").json()
    assert data["status"] == "SUPPRESSED"
    assert data["status_reason"] == "ad_user_data_denied"


class FailingPublisher(QueuePublisher):
    def publish(self, message: QueueMessage) -> None:
        raise ConnectionError("queue unreachable")


def test_publish_failure_still_accepts_and_leaves_event_queued(
    settings: Settings,
    session_factory: sessionmaker[Session],
    caplog,  # type: ignore[no-untyped-def]
) -> None:
    client = TestClient(create_app(settings, session_factory, FailingPublisher()))
    with caplog.at_level(logging.ERROR):
        resp = _post(client, "salesforce", salesforce_payload(), SALESFORCE_SECRET)

    # The row is durable, so the CRM is told 202 and must not retry.
    assert resp.status_code == 202
    with session_factory() as session:
        row = ledger.get_event(session, "salesforce:e-sf-0001")
        assert row is not None
        assert row.status == "QUEUED"
    assert "publish failed" in caplog.text
    # And nothing about the person made it into the log line.
    assert "Gmail" not in caplog.text
