"""Dead-letter records and replay, end to end through the real pipeline:
upload fails -> DLQ row -> replay -> QUEUED -> worker reuses digests ->
PROCESSED -> second upload succeeds."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from gateway import ledger, worker
from gateway.dlq import store as dlq
from gateway.observability import metrics
from gateway.queue import MemoryQueue
from gateway.upload.errors import PermanentUploadError, TransientUploadError
from gateway.upload.fake import FakeUploadClient, with_failures
from gateway.upload.uploader import CycleReport
from tests.upload.test_uploader import make_uploader, processed_events, statuses, transitions

T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def test_partial_failure_writes_a_self_contained_dlq_record(
    session_factory: sessionmaker[Session],
) -> None:
    ids = processed_events(session_factory, 2)
    client = FakeUploadClient([with_failures(2, {1: "CONVERSION_ACTION_NOT_FOUND"})])
    make_uploader(session_factory, client).run_once(T0)

    with session_factory() as session:
        [row] = dlq.list_dead_letters(session, dlq.DlqFilter())
        assert row.event_id == ids[1]
        assert row.source == "salesforce"
        assert row.conversion_action == "closed_won"
        assert row.failure_class == "partial"
        assert row.reason == "permanent_row:CONVERSION_ACTION_NOT_FOUND"
        assert row.platform_error_code == "CONVERSION_ACTION_NOT_FOUND"
        assert row.platform_error_message == "row 1"
        assert row.replayed_at is None and row.replay_count == 0
        # The payload is the exact request row: digests, never raw values.
        assert row.payload["orderId"] == ids[1]
        assert row.payload["conversionAction"] == "customers/42/conversionActions/closed_won"
        assert "userIdentifiers" in row.payload
        assert "mohith" not in str(row.payload).lower()
        # Attempt history is the full transition log up to dead-lettering.
        assert [h["status"] for h in row.attempt_history] == [
            "RECEIVED",
            "VALIDATED",
            "QUEUED",
            "PROCESSED",
            "UPLOADING",
            "DEAD_LETTERED",
        ]
        assert row.attempt_history[-1]["reason"] == "permanent_row:CONVERSION_ACTION_NOT_FOUND"


def test_poison_and_permanent_record_their_class_and_platform_error(
    session_factory: sessionmaker[Session],
) -> None:
    ids = processed_events(session_factory, 2)
    client = FakeUploadClient([TransientUploadError("http_503")] * 5)
    make_uploader(session_factory, client).run_once(T0)
    with session_factory() as session:
        rows = dlq.list_dead_letters(session, dlq.DlqFilter(failure_class="poison"))
        assert {r.event_id for r in rows} == set(ids)
        assert rows[0].platform_error_code == "http_503"
        assert "after 3 attempts" in (rows[0].platform_error_message or "")
        # Each transient attempt is in the history.
        assert [
            h["reason"] for h in rows[0].attempt_history if h["status"] == "FAILED_RETRYABLE"
        ] == [
            "transient:http_503",
            "transient:http_503",
        ]


def test_replay_moves_event_to_queued_and_second_upload_succeeds(
    session_factory: sessionmaker[Session],
) -> None:
    ids = processed_events(session_factory, 1)
    event_id = ids[0]
    # First upload: the conversion action mapping is wrong -> dead-lettered.
    client = FakeUploadClient([with_failures(1, {0: "CONVERSION_ACTION_NOT_FOUND"})])
    uploader = make_uploader(session_factory, client)
    uploader.run_once(T0)
    assert statuses(session_factory, ids)[event_id][0] == "DEAD_LETTERED"
    with session_factory() as session:
        digests_before = ledger.get_event(session, event_id).hashed_identifiers  # type: ignore[union-attr]

    # Operator fixes the mapping and replays.
    queue = MemoryQueue()
    with session_factory.begin() as session:
        [row] = dlq.list_dead_letters(session, dlq.DlqFilter())
        dlq_id = row.id
        assert dlq.replay(session, row, queue, T0 + timedelta(minutes=5)) is True

    got = statuses(session_factory, ids)[event_id]
    assert got == ("QUEUED", f"replayed:dlq:{dlq_id}", 0)  # attempt budget reset
    assert queue.ready_count() == 1
    with session_factory() as session:
        [row] = dlq.list_dead_letters(session, dlq.DlqFilter(include_replayed=True))
        assert row.replayed_at is not None and row.replay_count == 1
        assert dlq.list_dead_letters(session, dlq.DlqFilter()) == []  # no longer open

    # The worker processes the replay message. It carries no identifiers;
    # the digests already on the row are kept, not wiped.
    [delivery] = queue.receive(1, 0)
    assert delivery.message.identifiers.email is None
    worker.handle_delivery(queue, session_factory, delivery)
    with session_factory() as session:
        event = ledger.get_event(session, event_id)
        assert event is not None
        assert event.status == "PROCESSED"
        assert event.hashed_identifiers == digests_before

    # Second upload attempt succeeds.
    assert uploader.run_once(T0 + timedelta(minutes=6)) == CycleReport(uploaded=1)
    assert statuses(session_factory, ids)[event_id] == ("UPLOADED", None, 1)
    assert transitions(session_factory, event_id)[-6:] == [
        ("UPLOADING", None),
        ("DEAD_LETTERED", "permanent_row:CONVERSION_ACTION_NOT_FOUND"),
        ("QUEUED", f"replayed:dlq:{dlq_id}"),
        ("PROCESSED", None),
        ("UPLOADING", None),
        ("UPLOADED", None),
    ]
    # The client saw the same order id twice: once failed, once accepted.
    assert client.sent_order_ids() == [[event_id], [event_id]]


def test_replay_is_idempotent_and_refuses_moved_events(
    session_factory: sessionmaker[Session],
) -> None:
    ids = processed_events(session_factory, 1)
    make_uploader(session_factory, FakeUploadClient([PermanentUploadError("http_401")])).run_once(
        T0
    )
    queue = MemoryQueue()
    with session_factory.begin() as session:
        [row] = dlq.list_dead_letters(session, dlq.DlqFilter())
        assert dlq.replay(session, row, queue, T0) is True
        assert dlq.replay(session, row, queue, T0) is False  # already replayed
    assert queue.ready_count() == 1
    assert statuses(session_factory, ids)[ids[0]][0] == "QUEUED"


def test_filters(session_factory: sessionmaker[Session]) -> None:
    ids = processed_events(session_factory, 3)
    client = FakeUploadClient(
        [with_failures(3, {0: "CONVERSION_ACTION_NOT_FOUND", 1: "INVALID_USER_IDENTIFIER"})]
    )
    make_uploader(session_factory, client).run_once(T0)
    with session_factory() as session:
        f = dlq.DlqFilter
        assert len(dlq.list_dead_letters(session, f())) == 2
        assert [
            r.event_id
            for r in dlq.list_dead_letters(session, f(reason_prefix="permanent_row:INVALID"))
        ] == [ids[1]]
        assert len(dlq.list_dead_letters(session, f(reason_prefix="permanent_row:"))) == 2
        assert dlq.list_dead_letters(session, f(source="hubspot")) == []
        assert (
            len(dlq.list_dead_letters(session, f(source="salesforce", failure_class="partial")))
            == 2
        )
        assert dlq.list_dead_letters(session, f(since=T0 + timedelta(seconds=1))) == []
        assert (
            len(dlq.list_dead_letters(session, f(since=T0, until=T0 + timedelta(seconds=1)))) == 2
        )
        assert [r.event_id for r in dlq.list_dead_letters(session, f(event_id=ids[0]))] == [ids[0]]
        assert dlq.open_depth_by_source(session) == {"salesforce": 2}


def test_dlq_depth_gauge_tracks_open_records(session_factory: sessionmaker[Session]) -> None:
    processed_events(session_factory, 2)
    client = FakeUploadClient([with_failures(2, {0: "EXPIRED_CLICK", 1: "EXPIRED_CLICK"})])
    make_uploader(session_factory, client).run_once(T0)
    assert metrics.read_gauge(metrics.dlq_depth, source="salesforce") == 2
