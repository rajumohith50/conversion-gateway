"""Ledger operations against a real database: idempotent insert and the
state machine."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from gateway import ledger
from gateway.api import mappers
from gateway.ledger import IllegalTransition
from gateway.models.status import ALLOWED_TRANSITIONS, EventStatus, Source
from tests.api.payloads import salesforce_payload

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _event():  # type: ignore[no-untyped-def]
    return mappers.parse_and_map(Source.SALESFORCE, salesforce_payload())


def test_record_validated_writes_row_and_two_transitions(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        row = ledger.record_validated(session, _event(), NOW)
        assert row is not None

    with session_factory() as session:
        row = ledger.get_event(session, "salesforce:e-sf-0001")
        assert row is not None
        assert row.status == "VALIDATED"
        assert row.status_reason is None
        assert row.hashed_identifiers is None  # phase 3 fills this
        assert row.click_id == "Cj0KCQjw-example"
        assert row.consent_ad_user_data == "GRANTED"
        assert row.received_at == NOW
        transitions = ledger.list_transitions(session, row.event_id)
        assert [(t.from_status, t.to_status) for t in transitions] == [
            (None, "RECEIVED"),
            ("RECEIVED", "VALIDATED"),
        ]


def test_record_validated_never_persists_raw_identifiers(
    session_factory: sessionmaker[Session],
) -> None:
    # Dump every column of the row and every transition to text and look
    # for the raw values. Cheap, and catches a future "helpful" column.
    with session_factory.begin() as session:
        ledger.record_validated(session, _event(), NOW)
    with session_factory() as session:
        row = ledger.get_event(session, "salesforce:e-sf-0001")
        assert row is not None
        dumped = " ".join(str(getattr(row, c.name)) for c in row.__table__.columns).lower()
    for raw in ("mohith", "gmail", "415", "main st", "raju"):
        assert raw not in dumped


def test_duplicate_insert_returns_none_and_leaves_one_row(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        assert ledger.record_validated(session, _event(), NOW) is not None
    with session_factory.begin() as session:
        assert ledger.record_validated(session, _event(), NOW) is None
    with session_factory() as session:
        assert len(ledger.list_transitions(session, "salesforce:e-sf-0001")) == 2


def test_record_rejected(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        row = ledger.record_rejected(
            session, "hubspot:1", "hubspot", "1", "missing:properties", NOW
        )
        assert row is not None
    with session_factory() as session:
        row = ledger.get_event(session, "hubspot:1")
        assert row is not None
        assert row.status == "REJECTED"
        assert row.status_reason == "missing:properties"
        assert row.conversion_action is None
        transitions = ledger.list_transitions(session, "hubspot:1")
        assert [(t.to_status, t.reason) for t in transitions] == [
            ("RECEIVED", None),
            ("REJECTED", "missing:properties"),
        ]
        # Idempotent like the happy path.
        assert ledger.record_rejected(session, "hubspot:1", "hubspot", "1", "x", NOW) is None


def test_illegal_transition_is_refused(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        row = ledger.record_validated(session, _event(), NOW)
        assert row is not None
        with pytest.raises(IllegalTransition, match="VALIDATED -> UPLOADED"):
            ledger.transition(session, row, EventStatus.UPLOADED, None, NOW)
        # Nothing was changed by the refused call.
        assert row.status == "VALIDATED"


def test_legal_transition_records_reason(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        row = ledger.record_validated(session, _event(), NOW)
        assert row is not None
        ledger.transition(session, row, EventStatus.SUPPRESSED, "ad_user_data_denied", NOW)
    with session_factory() as session:
        row = ledger.get_event(session, "salesforce:e-sf-0001")
        assert row is not None
        assert row.status == "SUPPRESSED"
        assert row.status_reason == "ad_user_data_denied"
        assert ledger.list_transitions(session, row.event_id)[-1].reason == "ad_user_data_denied"


def test_state_machine_covers_every_status_and_terminal_states_have_no_exits() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(EventStatus)
    assert ALLOWED_TRANSITIONS[EventStatus.UPLOADED] == frozenset()
    assert ALLOWED_TRANSITIONS[EventStatus.REJECTED] == frozenset()
    # Replayable terminal states (design sections 6 and 7) can re-enter the queue.
    assert EventStatus.QUEUED in ALLOWED_TRANSITIONS[EventStatus.SUPPRESSED]
    assert EventStatus.QUEUED in ALLOWED_TRANSITIONS[EventStatus.DEAD_LETTERED]
