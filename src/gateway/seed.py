"""Post a realistic mix of webhooks and report where each one ended up.

`make seed` runs this inside the compose network after `make up`. It is
also an end-to-end smoke test: every seeded event has an expected terminal
state, and the command exits non-zero if any event lands somewhere else.

The mix exercises every path in the pipeline:

  1. Salesforce lead, click id + identifiers, consent granted   -> UPLOADED
  2. HubSpot lead, identifiers only, consent granted            -> UPLOADED
  3. Salesforce lead, ad_user_data DENIED                       -> SUPPRESSED
  4. HubSpot lead, unparseable phone, no click id               -> REJECTED
  5. Salesforce lead, conversion action the platform rejects    -> DEAD_LETTERED
  6. Event 1 delivered again                                    -> 200, one row
  7. Event 1 with a bad signature                               -> 401, nothing
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Any, TextIO

import httpx

from gateway.api.signature import compute_signature

TERMINAL = {"UPLOADED", "SUPPRESSED", "REJECTED", "DEAD_LETTERED"}


@dataclass(frozen=True)
class Seeded:
    label: str
    source: str
    event_id: str | None
    http_status: int
    expected_status: str | None  # None: no ledger row expected
    why: str


def _salesforce(event_id: str, **lead: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "EventId": event_id,
        "EventTime": "2026-09-22T09:30:00Z",
        "Conversion_Action__c": "closed_won",
        "Amount": 4800.00,
        "CurrencyIsoCode": "USD",
        "Lead": {
            "Id": "00Q5e00000DEMO1",
            "Email": "Priya.Natarajan+demo@Gmail.com",
            "Phone": "(415) 555-0142",
            "FirstName": "Priya",
            "LastName": "Natarajan",
            "Street": "500 Howard St., Suite 850",
            "City": "San Francisco",
            "StateCode": "CA",
            "PostalCode": "94105",
            "CountryCode": "US",
            "GCLID__c": "Cj0KCQjw-demo-click-id",
            "Consent_Ad_User_Data__c": "GRANTED",
            "Consent_Ad_Personalization__c": "GRANTED",
        },
    }
    payload["Lead"].update(lead)
    return payload


def _hubspot(event_id: int, **props: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eventId": event_id,
        "objectId": 7701,
        "occurredAt": 1790069400000,
        "conversionAction": "qualified_lead",
        "properties": {
            "email": "tom.okafor@outlook.com",
            "phone": "+44 20 7946 0958",
            "firstname": "Tom",
            "lastname": "Okafor",
            "address": "14 Rue de l'Église",
            "city": "Lyon",
            "state": "ARA",
            "zip": "69001",
            "country": "FR",
            "amount": "950.00",
            "deal_currency_code": "EUR",
            "ad_user_data_consent": "GRANTED",
            "ad_personalization_consent": "GRANTED",
        },
    }
    payload["properties"].update(props)
    return payload


def _post(
    client: httpx.Client,
    source: str,
    secret: str,
    payload: dict[str, Any],
    *,
    bad_sig: bool = False,
) -> httpx.Response:
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = compute_signature("wrong-secret" if bad_sig else secret, ts, body)
    return client.post(
        f"/webhooks/crm/{source}",
        content=body,
        headers={
            "X-Webhook-Timestamp": ts,
            "X-Webhook-Signature": sig,
            "Content-Type": "application/json",
        },
    )


def seed(
    api_url: str,
    sf_secret: str,
    hs_secret: str,
    out: TextIO,
    wait_seconds: float = 60.0,
    run_id: str | None = None,
    client: httpx.Client | None = None,
) -> bool:
    """Post the mix, wait for terminal states, print the table. Returns
    True if every event landed where expected.

    `client` lets a test pass FastAPI's TestClient (an httpx.Client) so the
    same code runs in-process against the app."""
    run_id = run_id or str(int(time.time()))
    # HubSpot event ids are integers on the wire; derive them from the run
    # id so every run gets fresh ids for both sources.
    hs_base = int.from_bytes(run_id.encode(), "big") % 10**12 * 10
    client = client or httpx.Client(base_url=api_url, timeout=10)
    seeded: list[Seeded] = []

    def record(
        label: str, source: str, resp: httpx.Response, expected: str | None, why: str
    ) -> None:
        event_id = resp.json().get("event_id") if resp.status_code in (200, 202, 422) else None
        seeded.append(Seeded(label, source, event_id, resp.status_code, expected, why))

    r = _post(client, "salesforce", sf_secret, _salesforce(f"seed-{run_id}-1"))
    record(
        "1 salesforce, click id + identifiers", "salesforce", r, "UPLOADED", "matched on click id"
    )

    r = _post(client, "hubspot", hs_secret, _hubspot(hs_base + 2))
    record("2 hubspot, identifiers only", "hubspot", r, "UPLOADED", "matched on hashed identifiers")

    r = _post(
        client,
        "salesforce",
        sf_secret,
        _salesforce(f"seed-{run_id}-3", Consent_Ad_User_Data__c="DENIED"),
    )
    record("3 salesforce, consent denied", "salesforce", r, "SUPPRESSED", "ad_user_data_denied")

    r = _post(client, "hubspot", hs_secret, _hubspot(hs_base + 4, phone="call me maybe"))
    record("4 hubspot, unparseable phone", "hubspot", r, "REJECTED", "unparseable:phone")

    payload = _salesforce(f"seed-{run_id}-5")
    payload["Conversion_Action__c"] = "contract_renewal"
    r = _post(client, "salesforce", sf_secret, payload)
    record(
        "5 salesforce, unknown conversion action",
        "salesforce",
        r,
        "DEAD_LETTERED",
        "platform: CONVERSION_ACTION_NOT_FOUND",
    )

    r = _post(client, "salesforce", sf_secret, _salesforce(f"seed-{run_id}-1"))
    record(
        "6 event 1 delivered again",
        "salesforce",
        r,
        None,
        f"http {r.status_code}: duplicate, no new row",
    )

    r = _post(client, "salesforce", sf_secret, _salesforce(f"seed-{run_id}-1"), bad_sig=True)
    record(
        "7 event 1, bad signature", "salesforce", r, None, f"http {r.status_code}: nothing written"
    )

    # Wait for the worker and uploader to finish with the ones that have rows.
    tracked = [s for s in seeded if s.expected_status is not None and s.event_id]
    deadline = time.monotonic() + wait_seconds
    final: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        for s in tracked:
            assert s.event_id is not None
            final[s.event_id] = client.get(f"/events/{s.event_id}").json()
        if all(final[s.event_id]["status"] in TERMINAL for s in tracked if s.event_id):
            break
        time.sleep(0.5)

    ok = True
    out.write(f"\n{'event':<42} {'http':>4}  {'expected':<14} {'actual':<14} reason\n")
    for s in seeded:
        if s.expected_status is None:
            expected_http = 200 if s.label.startswith("6") else 401
            good = s.http_status == expected_http
            ok &= good
            mark = "ok " if good else "!! "
            out.write(f"{mark}{s.label:<39} {s.http_status:>4}  {'-':<14} {'-':<14} {s.why}\n")
            continue
        state = final.get(s.event_id or "", {})
        actual = state.get("status", "?")
        reason = state.get("status_reason") or ""
        good = actual == s.expected_status
        ok &= good
        mark = "ok " if good else "!! "
        out.write(
            f"{mark}{s.label:<39} {s.http_status:>4}  {s.expected_status:<14} "
            f"{actual:<14} {reason or s.why}\n"
        )

    out.write("\n")
    if ok:
        out.write("every seeded event reached its expected state.\n")
        out.write(
            "next: `make dlq` to see event 5, then `gateway dlq replay --id <id>` "
            "after fixing the mapping.\n"
        )
    else:
        out.write("MISMATCH: at least one event did not reach its expected state.\n")
    return ok


def main() -> int:
    import sys

    api_url = os.environ.get("SEED_API_URL", "http://localhost:8080")
    sf = os.environ.get("WEBHOOK_SECRET_SALESFORCE", "demo-salesforce-secret")
    hs = os.environ.get("WEBHOOK_SECRET_HUBSPOT", "demo-hubspot-secret")
    return 0 if seed(api_url, sf, hs, sys.stdout) else 1
