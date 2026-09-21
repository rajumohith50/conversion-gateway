"""Every failure mode through the Uploader with a scripted client, asserting
exactly which events end up UPLOADED, FAILED_RETRYABLE or DEAD_LETTERED,
and that no event is ever sent twice after it succeeded."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from gateway import ledger
from gateway.api import mappers
from gateway.models.status import Source
from gateway.processor import process_message
from gateway.queue import QueueMessage
from gateway.upload.backoff import RetryPolicy
from gateway.upload.errors import PermanentUploadError, TransientUploadError
from gateway.upload.fake import FakeUploadClient, all_ok, with_failures
from gateway.upload.uploader import CycleReport, Uploader
from tests.api.payloads import salesforce_payload

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
FAST = RetryPolicy(
    max_attempts=3, max_elapsed_seconds=10, backoff_base_seconds=0.001, backoff_max_seconds=0.002
)


def processed_events(session_factory: sessionmaker[Session], n: int) -> list[str]:
    """n events taken all the way to PROCESSED, as the worker would."""
    ids = []
    for i in range(n):
        event = mappers.parse_and_map(Source.SALESFORCE, salesforce_payload(EventId=f"e-{i}"))
        with session_factory.begin() as session:
            ledger.record_validated(session, event, T0 - timedelta(seconds=n - i))
            msg = QueueMessage(
                event_id=event.event_id, identifiers=event.identifiers, enqueued_at=T0
            )
            process_message(session, msg, T0)
        ids.append(event.event_id)
    return ids


def make_uploader(
    session_factory: sessionmaker[Session], client: FakeUploadClient, **overrides: object
) -> Uploader:
    kwargs: dict[str, object] = dict(
        customer_id="42",
        batch_size=10,
        batch_wait_seconds=0,
        row_retry_after=timedelta(seconds=60),
        stale_after=timedelta(seconds=600),
        max_row_attempts=3,
    )
    kwargs.update(overrides)
    return Uploader(session_factory, client, FAST, **kwargs)  # type: ignore[arg-type]


def statuses(
    session_factory: sessionmaker[Session], ids: list[str]
) -> dict[str, tuple[str, str | None, int]]:
    with session_factory() as session:
        out = {}
        for event_id in ids:
            row = ledger.get_event(session, event_id)
            assert row is not None
            out[event_id] = (row.status, row.status_reason, row.attempt_count)
        return out


def transitions(
    session_factory: sessionmaker[Session], event_id: str
) -> list[tuple[str, str | None]]:
    with session_factory() as session:
        return [(t.to_status, t.reason) for t in ledger.list_transitions(session, event_id)]


# --- Happy path ------------------------------------------------------------------


def test_all_rows_uploaded(session_factory: sessionmaker[Session]) -> None:
    ids = processed_events(session_factory, 3)
    client = FakeUploadClient()
    report = make_uploader(session_factory, client).run_once(T0)

    assert report == CycleReport(uploaded=3)
    assert client.sent_order_ids() == [ids]
    for status, reason, attempts in statuses(session_factory, ids).values():
        assert (status, reason, attempts) == ("UPLOADED", None, 1)
    assert transitions(session_factory, ids[0])[-2:] == [("UPLOADING", None), ("UPLOADED", None)]


def test_nothing_to_do(session_factory: sessionmaker[Session]) -> None:
    client = FakeUploadClient()
    assert make_uploader(session_factory, client).run_once(T0) == CycleReport()
    assert client.calls == []


# --- Partial failure: the bug this phase exists to get right -----------------------


def test_partial_failure_acts_per_row_and_never_resends_successes(
    session_factory: sessionmaker[Session],
) -> None:
    ids = processed_events(session_factory, 4)
    # Row 1: permanent row error. Row 3: retryable row error. 0 and 2 fine.
    client = FakeUploadClient(
        [with_failures(4, {1: "INVALID_USER_IDENTIFIER", 3: "TOO_RECENT_CONVERSION"})]
    )
    uploader = make_uploader(session_factory, client)
    report = uploader.run_once(T0)

    assert report == CycleReport(uploaded=2, retry_later=1, dead_lettered=1)
    got = statuses(session_factory, ids)
    assert got[ids[0]] == ("UPLOADED", None, 1)
    assert got[ids[1]] == ("DEAD_LETTERED", "permanent_row:INVALID_USER_IDENTIFIER", 1)
    assert got[ids[2]] == ("UPLOADED", None, 1)
    assert got[ids[3]] == ("FAILED_RETRYABLE", "retryable_row:TOO_RECENT_CONVERSION", 1)

    # Exactly one request was made. The batch was NOT retried.
    assert client.sent_order_ids() == [ids]

    # Later, the retryable row is re-claimed alone. Rows 0 and 2 are never
    # sent again: that is the no-double-upload guarantee.
    later = T0 + timedelta(seconds=61)
    report = uploader.run_once(later)
    assert report == CycleReport(uploaded=1)
    assert client.sent_order_ids() == [ids, [ids[3]]]
    assert statuses(session_factory, [ids[3]])[ids[3]] == ("UPLOADED", None, 2)
    # And the full audit trail on the retried row.
    assert transitions(session_factory, ids[3])[-4:] == [
        ("UPLOADING", None),
        ("FAILED_RETRYABLE", "retryable_row:TOO_RECENT_CONVERSION"),
        ("UPLOADING", None),
        ("UPLOADED", None),
    ]


def test_retryable_row_is_not_reclaimed_before_retry_after(
    session_factory: sessionmaker[Session],
) -> None:
    ids = processed_events(session_factory, 1)
    client = FakeUploadClient([with_failures(1, {0: "TOO_RECENT_CONVERSION"})])
    uploader = make_uploader(session_factory, client)
    uploader.run_once(T0)
    assert uploader.run_once(T0 + timedelta(seconds=30)) == CycleReport()
    assert len(client.calls) == 1
    assert uploader.run_once(T0 + timedelta(seconds=60)) == CycleReport(uploaded=1)
    assert statuses(session_factory, ids)[ids[0]][0] == "UPLOADED"


def test_retryable_row_hits_attempt_ceiling_and_is_dead_lettered(
    session_factory: sessionmaker[Session],
) -> None:
    ids = processed_events(session_factory, 1)
    client = FakeUploadClient([with_failures(1, {0: "TOO_RECENT_CONVERSION"})] * 10)
    uploader = make_uploader(session_factory, client, max_row_attempts=3)
    now = T0
    for _ in range(3):
        uploader.run_once(now)
        now += timedelta(seconds=61)
    assert statuses(session_factory, ids)[ids[0]] == (
        "FAILED_RETRYABLE",
        "retryable_row:TOO_RECENT_CONVERSION",
        3,
    )
    # Fourth claim exceeds the ceiling: dead-lettered without a request.
    report = uploader.run_once(now)
    assert report == CycleReport()
    assert statuses(session_factory, ids)[ids[0]] == (
        "DEAD_LETTERED",
        "poison:row_attempt_ceiling:3",
        4,
    )
    assert len(client.calls) == 3


# --- Whole-request failures ---------------------------------------------------------


def test_transient_then_success_retries_whole_batch_and_records_each_attempt(
    session_factory: sessionmaker[Session],
) -> None:
    ids = processed_events(session_factory, 2)
    client = FakeUploadClient(
        [TransientUploadError("http_429"), TransientUploadError("timeout"), all_ok(2)]
    )
    report = make_uploader(session_factory, client).run_once(T0)

    assert report == CycleReport(uploaded=2)
    # Three requests, all with the full batch: nothing had been accepted.
    assert client.sent_order_ids() == [ids, ids, ids]
    assert statuses(session_factory, ids)[ids[0]] == ("UPLOADED", None, 3)
    assert transitions(session_factory, ids[0])[-6:] == [
        ("UPLOADING", None),
        ("FAILED_RETRYABLE", "transient:http_429"),
        ("UPLOADING", None),
        ("FAILED_RETRYABLE", "transient:timeout"),
        ("UPLOADING", None),
        ("UPLOADED", None),
    ]


def test_poison_dead_letters_batch_with_last_error(session_factory: sessionmaker[Session]) -> None:
    ids = processed_events(session_factory, 2)
    client = FakeUploadClient([TransientUploadError("http_503")] * 5)
    report = make_uploader(session_factory, client).run_once(T0)

    assert report == CycleReport(dead_lettered=2)
    assert len(client.calls) == 3  # FAST.max_attempts
    for status, reason, attempts in statuses(session_factory, ids).values():
        assert (status, reason, attempts) == (
            "DEAD_LETTERED",
            "poison:http_503:after_3_attempts",
            3,
        )


def test_permanent_dead_letters_immediately_without_retry(
    session_factory: sessionmaker[Session],
) -> None:
    ids = processed_events(session_factory, 2)
    client = FakeUploadClient([PermanentUploadError("http_401")])
    report = make_uploader(session_factory, client).run_once(T0)

    assert report == CycleReport(dead_lettered=2)
    assert len(client.calls) == 1
    for status, reason, _ in statuses(session_factory, ids).values():
        assert (status, reason) == ("DEAD_LETTERED", "permanent:http_401")


# --- Batching and claiming ----------------------------------------------------------


def test_batch_size_splits_work_across_cycles(session_factory: sessionmaker[Session]) -> None:
    ids = processed_events(session_factory, 5)
    client = FakeUploadClient()
    uploader = make_uploader(session_factory, client, batch_size=2)
    assert uploader.run_once(T0) == CycleReport(uploaded=2)
    assert uploader.run_once(T0) == CycleReport(uploaded=2)
    assert uploader.run_once(T0) == CycleReport(uploaded=1)  # wait=0 flushes the remainder
    assert client.sent_order_ids() == [ids[:2], ids[2:4], ids[4:]]


def test_batch_waits_for_more_rows_until_time_limit(session_factory: sessionmaker[Session]) -> None:
    processed_events(session_factory, 1)
    client = FakeUploadClient()
    uploader = make_uploader(session_factory, client, batch_size=10, batch_wait_seconds=5)
    assert uploader.run_once(T0) == CycleReport()  # claimed, held
    assert client.calls == []
    assert uploader.run_once(T0 + timedelta(seconds=5)) == CycleReport(uploaded=1)


def test_stale_uploading_rows_are_reclaimed(session_factory: sessionmaker[Session]) -> None:
    # Simulate an uploader that claimed and died: rows sit in UPLOADING.
    ids = processed_events(session_factory, 1)
    dead_uploader = make_uploader(
        session_factory, FakeUploadClient(), batch_size=10, batch_wait_seconds=999
    )
    dead_uploader.run_once(T0)  # claims into its batcher, never uploads
    assert statuses(session_factory, ids)[ids[0]] == ("UPLOADING", None, 1)

    client = FakeUploadClient()
    live = make_uploader(session_factory, client)
    assert live.run_once(T0 + timedelta(seconds=599)) == CycleReport()  # not stale yet
    assert live.run_once(T0 + timedelta(seconds=600)) == CycleReport(uploaded=1)
    assert statuses(session_factory, ids)[ids[0]] == ("UPLOADED", None, 2)
    assert ("UPLOADING", "reclaimed_stale") in transitions(session_factory, ids[0])


def test_uploaded_and_dead_lettered_rows_are_never_reclaimed(
    session_factory: sessionmaker[Session],
) -> None:
    ids = processed_events(session_factory, 2)
    client = FakeUploadClient([with_failures(2, {1: "EXPIRED_CLICK"})])
    uploader = make_uploader(session_factory, client)
    uploader.run_once(T0)
    for _ in range(3):
        assert uploader.run_once(T0 + timedelta(hours=1)) == CycleReport()
    assert client.sent_order_ids() == [ids]


def test_run_loop_stops(session_factory: sessionmaker[Session]) -> None:
    import threading

    from gateway.upload import uploader as loop

    ids = processed_events(session_factory, 1)
    client = FakeUploadClient()
    stop = threading.Event()
    t = threading.Thread(target=loop.run, args=(make_uploader(session_factory, client), stop, 0.01))
    t.start()
    for _ in range(200):
        if client.calls:
            break
        threading.Event().wait(0.01)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
    assert statuses(session_factory, ids)[ids[0]][0] == "UPLOADED"


@pytest.mark.parametrize("n", [0, 1])
def test_report_equality_is_value_based(n: int) -> None:
    assert CycleReport(uploaded=n) == CycleReport(uploaded=n)
