"""process_message: consent gate, normalisation, and state transitions
against a real ledger row."""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from gateway import ledger
from gateway.api import mappers
from gateway.consent import ConsentStatus, SuppressionReason
from gateway.models.status import Source
from gateway.normalise import RawIdentifiers, sha256_hex
from gateway.processor import ProcessOutcome, process_message
from gateway.queue import QueueMessage
from tests.api.payloads import salesforce_payload

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
G, D, U = ConsentStatus.GRANTED, ConsentStatus.DENIED, ConsentStatus.UNSPECIFIED
R = SuppressionReason


def _queued_event(session_factory: sessionmaker[Session], **lead_overrides: Any) -> QueueMessage:
    """Ingest a payload as the API would and return the message the API
    would have published."""
    payload = salesforce_payload()
    payload["Lead"].update(lead_overrides)
    # None means "remove the field", matching a CRM that omits it.
    for key, value in list(payload["Lead"].items()):
        if value is None:
            del payload["Lead"][key]
    event = mappers.parse_and_map(Source.SALESFORCE, payload)
    with session_factory.begin() as session:
        assert ledger.record_validated(session, event, NOW) is not None
    return QueueMessage(event_id=event.event_id, identifiers=event.identifiers, enqueued_at=NOW)


def _run(session_factory: sessionmaker[Session], message: QueueMessage) -> ProcessOutcome:
    with session_factory.begin() as session:
        return process_message(session, message, NOW)


def _row(session_factory: sessionmaker[Session], event_id: str):  # type: ignore[no-untyped-def]
    with session_factory() as session:
        row = ledger.get_event(session, event_id)
        assert row is not None
        transitions = [(t.to_status, t.reason) for t in ledger.list_transitions(session, event_id)]
        return row, transitions


# --- Consent truth table -------------------------------------------------------
# (ad_user_data, ad_personalization, expected outcome, expected reason)
# None = field absent from the payload.
CONSENT_TABLE = [
    (G, G, ProcessOutcome.PROCESSED, None),
    (D, G, ProcessOutcome.SUPPRESSED, R.AD_USER_DATA_DENIED),
    (D, D, ProcessOutcome.SUPPRESSED, R.AD_USER_DATA_DENIED),
    (D, U, ProcessOutcome.SUPPRESSED, R.AD_USER_DATA_DENIED),
    (D, None, ProcessOutcome.SUPPRESSED, R.AD_USER_DATA_DENIED),
    (U, G, ProcessOutcome.SUPPRESSED, R.AD_USER_DATA_UNSPECIFIED),
    (U, D, ProcessOutcome.SUPPRESSED, R.AD_USER_DATA_UNSPECIFIED),
    (U, U, ProcessOutcome.SUPPRESSED, R.AD_USER_DATA_UNSPECIFIED),
    (U, None, ProcessOutcome.SUPPRESSED, R.AD_USER_DATA_UNSPECIFIED),
    (None, G, ProcessOutcome.SUPPRESSED, R.AD_USER_DATA_MISSING),
    (None, D, ProcessOutcome.SUPPRESSED, R.AD_USER_DATA_MISSING),
    (None, U, ProcessOutcome.SUPPRESSED, R.AD_USER_DATA_MISSING),
    (None, None, ProcessOutcome.SUPPRESSED, R.AD_USER_DATA_MISSING),
    (G, D, ProcessOutcome.SUPPRESSED, R.AD_PERSONALIZATION_DENIED),
    (G, U, ProcessOutcome.SUPPRESSED, R.AD_PERSONALIZATION_UNSPECIFIED),
    (G, None, ProcessOutcome.SUPPRESSED, R.AD_PERSONALIZATION_MISSING),
]


@pytest.mark.parametrize(("user_data", "personalization", "outcome", "reason"), CONSENT_TABLE)
def test_consent_truth_table(
    session_factory: sessionmaker[Session],
    user_data: ConsentStatus | None,
    personalization: ConsentStatus | None,
    outcome: ProcessOutcome,
    reason: SuppressionReason | None,
) -> None:
    message = _queued_event(
        session_factory,
        Consent_Ad_User_Data__c=user_data,
        Consent_Ad_Personalization__c=personalization,
    )
    assert _run(session_factory, message) is outcome

    row, transitions = _row(session_factory, message.event_id)
    if outcome is ProcessOutcome.SUPPRESSED:
        assert reason is not None
        assert row.status == "SUPPRESSED"
        assert row.status_reason == reason.value
        assert transitions[-1] == ("SUPPRESSED", reason.value)
        # Suppressed before touching identifiers: no digests written.
        assert row.hashed_identifiers is None
    else:
        assert row.status == "PROCESSED"
        assert row.hashed_identifiers is not None


def test_consent_table_is_exhaustive() -> None:
    states: list[ConsentStatus | None] = [*ConsentStatus, None]
    assert {(a, b) for a, b, _, _ in CONSENT_TABLE} == {(a, b) for a in states for b in states}


# --- Normalisation ------------------------------------------------------------


def test_processed_row_holds_correct_digests_and_no_raw_values(
    session_factory: sessionmaker[Session],
) -> None:
    message = _queued_event(session_factory)
    assert _run(session_factory, message) is ProcessOutcome.PROCESSED

    row, transitions = _row(session_factory, message.event_id)
    assert row.status == "PROCESSED"
    assert row.status_reason is None
    assert [t for t, _ in transitions] == ["RECEIVED", "VALIDATED", "QUEUED", "PROCESSED"]
    hashed = row.hashed_identifiers
    assert hashed == {
        "hashed_email": sha256_hex("mohith@gmail.com"),
        "hashed_phone": sha256_hex("+14155552671"),
        "hashed_given_name": sha256_hex("mohith"),
        "hashed_family_name": sha256_hex("raju"),
        "hashed_street_address": sha256_hex("123 main st"),
        "city": "san francisco",
        "region": "ca",
        "postal_code": "94103",
        "country": "US",
    }
    dumped = str(hashed).lower()
    for raw in ("mohith@", "gmail", "415", "main st"):
        assert raw not in dumped


def test_normalisation_rejection_is_terminal_with_field_reasons(
    session_factory: sessionmaker[Session],
) -> None:
    # No click id, so identifiers are the match key; two of them are bad.
    message = _queued_event(session_factory, GCLID__c="", Phone="not a phone", Email="nope")
    assert _run(session_factory, message) is ProcessOutcome.REJECTED

    row, transitions = _row(session_factory, message.event_id)
    assert row.status == "REJECTED"
    assert row.status_reason == "malformed:email;unparseable:phone"
    assert transitions[-1] == ("REJECTED", "malformed:email;unparseable:phone")
    assert row.hashed_identifiers is None
    # Terminal: a second delivery of the same message does nothing.
    assert _run(session_factory, message) is ProcessOutcome.SKIPPED_NOT_QUEUED


def test_no_usable_identifiers_without_click_id_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    message = _queued_event(
        session_factory,
        GCLID__c="",
        Email=None,
        Phone=None,
        FirstName=None,
        LastName=None,
        Street=None,
    )
    assert _run(session_factory, message) is ProcessOutcome.REJECTED
    row, _ = _row(session_factory, message.event_id)
    assert row.status_reason == "no_match_key:identifiers"


def test_click_id_event_with_no_identifiers_is_processed(
    session_factory: sessionmaker[Session],
) -> None:
    # The click id is a complete match key on its own.
    message = _queued_event(
        session_factory, Email=None, Phone=None, FirstName=None, LastName=None, Street=None
    )
    assert _run(session_factory, message) is ProcessOutcome.PROCESSED
    row, _ = _row(session_factory, message.event_id)
    assert row.click_id == "Cj0KCQjw-example"
    assert row.hashed_identifiers is not None
    assert row.hashed_identifiers["hashed_email"] is None


def test_click_id_event_with_a_bad_identifier_is_still_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    # A click id excuses *missing* identifiers, not *malformed* ones.
    message = _queued_event(session_factory, Phone="garbage")
    assert _run(session_factory, message) is ProcessOutcome.REJECTED
    row, _ = _row(session_factory, message.event_id)
    assert row.status_reason == "unparseable:phone"


# --- At-least-once edge cases --------------------------------------------------


def test_second_delivery_of_processed_event_is_skipped(
    session_factory: sessionmaker[Session],
) -> None:
    message = _queued_event(session_factory)
    assert _run(session_factory, message) is ProcessOutcome.PROCESSED
    assert _run(session_factory, message) is ProcessOutcome.SKIPPED_NOT_QUEUED
    _, transitions = _row(session_factory, message.event_id)
    assert len(transitions) == 4  # nothing appended by the redelivery


def test_message_without_a_ledger_row_is_skipped(session_factory: sessionmaker[Session]) -> None:
    orphan = QueueMessage(
        event_id="salesforce:ghost", identifiers=RawIdentifiers(email="a@b.com"), enqueued_at=NOW
    )
    assert _run(session_factory, orphan) is ProcessOutcome.SKIPPED_UNKNOWN_EVENT
