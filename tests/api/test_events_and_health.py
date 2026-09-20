"""GET /events/{id}, /healthz, /readyz."""

import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from gateway.api.app import create_app
from gateway.config import Settings
from gateway.db import make_session_factory
from gateway.queue import MemoryQueue
from tests.api.payloads import salesforce_payload
from tests.conftest import SALESFORCE_SECRET, signed_headers


def test_get_event_returns_lifecycle_and_transitions(client: TestClient) -> None:
    body = json.dumps(salesforce_payload()).encode()
    client.post(
        "/webhooks/crm/salesforce", content=body, headers=signed_headers(SALESFORCE_SECRET, body)
    )

    resp = client.get("/events/salesforce:e-sf-0001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["event_id"] == "salesforce:e-sf-0001"
    assert data["source"] == "salesforce"
    assert data["status"] == "QUEUED"
    assert data["status_reason"] is None
    assert data["conversion_action"] == "closed_won"
    assert data["match_key_type"] == "click_id"
    assert data["attempt_count"] == 0
    assert [(t["from_status"], t["to_status"]) for t in data["transitions"]] == [
        (None, "RECEIVED"),
        ("RECEIVED", "VALIDATED"),
        ("VALIDATED", "QUEUED"),
    ]
    # The response exposes lifecycle, never identifiers.
    assert "Gmail" not in resp.text
    assert "hashed" not in resp.text


def test_get_rejected_event_shows_reason(client: TestClient) -> None:
    body = b"{nope"
    posted = client.post(
        "/webhooks/crm/hubspot", content=body, headers=signed_headers("hs-test-secret", body)
    )
    resp = client.get(f"/events/{posted.json()['event_id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"
    assert resp.json()["status_reason"] == "malformed_json:body"


def test_get_unknown_event_is_404(client: TestClient) -> None:
    assert client.get("/events/salesforce:nope").status_code == 404


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_ok_when_database_reachable(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


def test_readyz_503_when_database_unreachable(settings: Settings) -> None:
    # Port 1 refuses immediately; no waiting on a timeout.
    dead_engine = create_engine("postgresql+psycopg://x:x@127.0.0.1:1/x")
    dead_factory: sessionmaker[Session] = make_session_factory(dead_engine)
    app = create_app(settings, dead_factory, MemoryQueue())
    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unavailable", "database": "unreachable"}
    # Liveness is unaffected by the database.
    assert TestClient(app).get("/healthz").status_code == 200
