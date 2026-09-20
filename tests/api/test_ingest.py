"""POST /webhooks/crm/{source} through the real app against real Postgres,
asserting the HTTP response AND the ledger state after each request."""

import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from gateway import ledger
from gateway.models.ledger import Event
from tests.api.payloads import hubspot_payload, salesforce_payload
from tests.conftest import HUBSPOT_SECRET, SALESFORCE_SECRET, signed_headers


def _count_events(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return session.execute(select(func.count()).select_from(Event)).scalar_one()


def _post(client: TestClient, source: str, body: bytes, secret: str, ts: int | None = None):  # type: ignore[no-untyped-def]
    return client.post(
        f"/webhooks/crm/{source}", content=body, headers=signed_headers(secret, body, ts)
    )


# --- Happy paths -------------------------------------------------------------


def test_salesforce_valid_payload(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    body = json.dumps(salesforce_payload()).encode()
    resp = _post(client, "salesforce", body, SALESFORCE_SECRET)

    assert resp.status_code == 202
    assert resp.json() == {"event_id": "salesforce:e-sf-0001", "status": "VALIDATED"}

    with session_factory() as session:
        row = ledger.get_event(session, "salesforce:e-sf-0001")
        assert row is not None
        assert row.status == "VALIDATED"
        assert row.source == "salesforce"
        assert row.conversion_action == "closed_won"
        assert row.match_key_type == "click_id"
        assert row.consent_ad_user_data == "GRANTED"
        assert str(row.conversion_value) == "1200.500000"
        assert [t.to_status for t in ledger.list_transitions(session, row.event_id)] == [
            "RECEIVED",
            "VALIDATED",
        ]


def test_hubspot_valid_payload(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    body = json.dumps(hubspot_payload()).encode()
    resp = _post(client, "hubspot", body, HUBSPOT_SECRET)

    assert resp.status_code == 202
    assert resp.json() == {"event_id": "hubspot:987654321", "status": "VALIDATED"}

    with session_factory() as session:
        row = ledger.get_event(session, "hubspot:987654321")
        assert row is not None
        assert row.status == "VALIDATED"
        assert row.source == "hubspot"
        assert row.currency == "USD"


def test_secrets_are_per_source(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    # A HubSpot payload signed with the Salesforce secret is not authentic.
    body = json.dumps(hubspot_payload()).encode()
    resp = _post(client, "hubspot", body, SALESFORCE_SECRET)
    assert resp.status_code == 401
    assert _count_events(session_factory) == 0


# --- Authentication failures: 401, nothing written ---------------------------


def test_bad_signature(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    body = json.dumps(salesforce_payload()).encode()
    headers = signed_headers(SALESFORCE_SECRET, body)
    headers["X-Webhook-Signature"] = "sha256=" + "0" * 64
    resp = client.post("/webhooks/crm/salesforce", content=body, headers=headers)

    assert resp.status_code == 401
    assert resp.json() == {"detail": {"reason": "signature_mismatch"}}
    assert _count_events(session_factory) == 0


def test_missing_headers(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    body = json.dumps(salesforce_payload()).encode()
    resp = client.post("/webhooks/crm/salesforce", content=body)
    assert resp.status_code == 401
    assert resp.json() == {"detail": {"reason": "missing_timestamp"}}
    assert _count_events(session_factory) == 0


def test_expired_timestamp(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    body = json.dumps(salesforce_payload()).encode()
    # Correctly signed, but 10 minutes old against a 5 minute window.
    resp = _post(client, "salesforce", body, SALESFORCE_SECRET, ts=int(time.time()) - 600)

    assert resp.status_code == 401
    assert resp.json() == {"detail": {"reason": "timestamp_out_of_window"}}
    assert _count_events(session_factory) == 0


def test_replayed_request_with_bumped_timestamp_is_rejected(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    # Capture a valid request, then resend it with a fresh timestamp header
    # but the original signature. The signature covered the old timestamp.
    body = json.dumps(salesforce_payload()).encode()
    headers = signed_headers(SALESFORCE_SECRET, body, timestamp=int(time.time()) - 200)
    headers["X-Webhook-Timestamp"] = str(int(time.time()))
    resp = client.post("/webhooks/crm/salesforce", content=body, headers=headers)
    assert resp.status_code == 401
    assert resp.json() == {"detail": {"reason": "signature_mismatch"}}
    assert _count_events(session_factory) == 0


def test_unknown_source_is_404(client: TestClient) -> None:
    body = b"{}"
    resp = _post(client, "pipedrive", body, SALESFORCE_SECRET)
    assert resp.status_code == 404


# --- Validation failures: 422, REJECTED row -----------------------------------


def test_malformed_json_body(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    body = b"{not json"
    resp = _post(client, "salesforce", body, SALESFORCE_SECRET)

    assert resp.status_code == 422
    data = resp.json()
    assert data["status"] == "REJECTED"
    assert data["errors"] == [{"field": "body", "reason": "malformed_json"}]
    assert data["event_id"].startswith("salesforce:unparsed:")

    with session_factory() as session:
        row = ledger.get_event(session, data["event_id"])
        assert row is not None
        assert row.status == "REJECTED"
        assert row.status_reason == "malformed_json:body"


def test_malformed_body_redelivered_makes_one_row(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    body = b"{not json"
    ids = {
        _post(client, "salesforce", body, SALESFORCE_SECRET).json()["event_id"] for _ in range(3)
    }
    assert len(ids) == 1
    assert _count_events(session_factory) == 1


def test_schema_invalid_payload_is_recorded_under_its_own_id(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    payload = salesforce_payload()
    del payload["Lead"]["Id"]
    payload["Lead"]["Consent_Ad_User_Data__c"] = "yes"
    body = json.dumps(payload).encode()
    resp = _post(client, "salesforce", body, SALESFORCE_SECRET)

    assert resp.status_code == 422
    data = resp.json()
    assert data["event_id"] == "salesforce:e-sf-0001"  # the CRM's id survived
    assert data["status"] == "REJECTED"
    assert {"field": "Lead.Id", "reason": "missing"} in data["errors"]
    assert {"field": "Lead.Consent_Ad_User_Data__c", "reason": "enum"} in data["errors"]

    with session_factory() as session:
        row = ledger.get_event(session, "salesforce:e-sf-0001")
        assert row is not None
        assert row.status == "REJECTED"
        assert row.status_reason is not None
        assert "missing:Lead.Id" in row.status_reason
        assert "enum:Lead.Consent_Ad_User_Data__c" in row.status_reason
        assert [t.to_status for t in ledger.list_transitions(session, row.event_id)] == [
            "RECEIVED",
            "REJECTED",
        ]


def test_rejection_response_and_ledger_contain_no_pii(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    payload = hubspot_payload()
    payload["properties"]["ad_user_data_consent"] = "definitely-yes"
    payload["properties"]["email"] = "leaky.person@example.com"
    body = json.dumps(payload).encode()
    resp = _post(client, "hubspot", body, HUBSPOT_SECRET)

    assert resp.status_code == 422
    assert "leaky" not in resp.text
    assert "definitely-yes" not in resp.text
    with session_factory() as session:
        row = ledger.get_event(session, "hubspot:987654321")
        assert row is not None
        assert "leaky" not in (row.status_reason or "")


def test_rejected_id_then_valid_delivery_does_not_overwrite(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    # Once an event id is REJECTED, a later valid delivery under the same
    # id is a duplicate: the ledger keeps the first outcome. This is the
    # consequence of event_id being the idempotency key, and is asserted
    # here so it is a documented behaviour, not an accident.
    bad = salesforce_payload()
    del bad["Lead"]["Id"]
    _post(client, "salesforce", json.dumps(bad).encode(), SALESFORCE_SECRET)
    resp = _post(client, "salesforce", json.dumps(salesforce_payload()).encode(), SALESFORCE_SECRET)
    assert resp.status_code == 200
    assert resp.json()["status"] == "REJECTED"
    assert _count_events(session_factory) == 1


# --- Idempotency ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "payload_fn", "secret", "expected_id"),
    [
        ("salesforce", salesforce_payload, SALESFORCE_SECRET, "salesforce:e-sf-0001"),
        ("hubspot", hubspot_payload, HUBSPOT_SECRET, "hubspot:987654321"),
    ],
)
def test_duplicate_delivery_three_times_produces_one_row(
    client: TestClient,
    session_factory: sessionmaker[Session],
    source: str,
    payload_fn,  # type: ignore[no-untyped-def]
    secret: str,
    expected_id: str,
) -> None:
    body = json.dumps(payload_fn()).encode()

    first = _post(client, source, body, secret)
    second = _post(client, source, body, secret)
    third = _post(client, source, body, secret)

    # First is accepted; retries acknowledge with 200 and the same id.
    assert first.status_code == 202
    assert second.status_code == 200
    assert third.status_code == 200
    assert {r.json()["event_id"] for r in (first, second, third)} == {expected_id}

    assert _count_events(session_factory) == 1
    with session_factory() as session:
        # And the audit trail was not appended to by the retries.
        assert len(ledger.list_transitions(session, expected_id)) == 2


def test_duplicate_with_changed_content_is_still_a_duplicate(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    # Same event id, different amount. The id is the key; first write wins.
    _post(client, "salesforce", json.dumps(salesforce_payload()).encode(), SALESFORCE_SECRET)
    resp = _post(
        client, "salesforce", json.dumps(salesforce_payload(Amount=1)).encode(), SALESFORCE_SECRET
    )
    assert resp.status_code == 200
    with session_factory() as session:
        row = ledger.get_event(session, "salesforce:e-sf-0001")
        assert row is not None
        assert str(row.conversion_value) == "1200.500000"
