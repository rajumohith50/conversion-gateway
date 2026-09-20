"""handle_delivery ack/nack semantics and the run() loop."""

import threading
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from gateway import ledger, worker
from gateway.api import mappers
from gateway.models.status import Source
from gateway.normalise import RawIdentifiers
from gateway.processor import ProcessOutcome
from gateway.queue import MemoryQueue, QueueMessage
from tests.api.payloads import salesforce_payload

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _enqueue_valid(session_factory: sessionmaker[Session], queue: MemoryQueue) -> str:
    event = mappers.parse_and_map(Source.SALESFORCE, salesforce_payload())
    with session_factory.begin() as session:
        ledger.record_validated(session, event, NOW)
    queue.publish(
        QueueMessage(event_id=event.event_id, identifiers=event.identifiers, enqueued_at=NOW)
    )
    return event.event_id


def test_successful_processing_acks(session_factory: sessionmaker[Session]) -> None:
    queue = MemoryQueue()
    _enqueue_valid(session_factory, queue)
    [delivery] = queue.receive(1, 0)

    assert worker.handle_delivery(queue, session_factory, delivery) is ProcessOutcome.PROCESSED
    assert queue.inflight_count() == 0
    assert queue.ready_count() == 0


def test_processing_exception_nacks_and_leaves_row_untouched(
    session_factory: sessionmaker[Session],
) -> None:
    queue = MemoryQueue()
    event_id = _enqueue_valid(session_factory, queue)
    [delivery] = queue.receive(1, 0)

    # A factory whose sessions blow up simulates the database going away
    # mid-batch.
    class Broken:
        def begin(self):  # type: ignore[no-untyped-def]
            raise ConnectionError("db gone")

    assert worker.handle_delivery(queue, Broken(), delivery) is None  # type: ignore[arg-type]
    assert queue.inflight_count() == 0
    assert queue.ready_count() == 1  # back on the queue
    [redelivered] = queue.receive(1, 0)
    assert redelivered.attempt == 2
    with session_factory() as session:
        row = ledger.get_event(session, event_id)
        assert row is not None
        assert row.status == "QUEUED"


def test_orphan_message_is_acked_not_retried(session_factory: sessionmaker[Session]) -> None:
    # Retrying a message with no row would loop forever.
    queue = MemoryQueue()
    queue.publish(
        QueueMessage(event_id="hubspot:ghost", identifiers=RawIdentifiers(), enqueued_at=NOW)
    )
    [delivery] = queue.receive(1, 0)
    assert (
        worker.handle_delivery(queue, session_factory, delivery)
        is ProcessOutcome.SKIPPED_UNKNOWN_EVENT
    )
    assert queue.ready_count() == 0


def test_run_loop_drains_queue_and_stops(session_factory: sessionmaker[Session]) -> None:
    queue = MemoryQueue()
    event_id = _enqueue_valid(session_factory, queue)
    stop = threading.Event()

    t = threading.Thread(
        target=worker.run,
        args=(queue, session_factory, stop),
        kwargs={"batch_size": 5, "poll_timeout_seconds": 0.05},
    )
    t.start()
    # Wait for the message to be consumed, then ask the loop to exit.
    for _ in range(100):
        if queue.ready_count() == 0 and queue.inflight_count() == 0:
            break
        threading.Event().wait(0.01)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()

    with session_factory() as session:
        row = ledger.get_event(session, event_id)
        assert row is not None
        assert row.status == "PROCESSED"
