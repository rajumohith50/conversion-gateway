"""MemoryQueue honours the QueueConsumer contract."""

import threading
from datetime import UTC, datetime

from gateway.normalise import RawIdentifiers
from gateway.queue import MemoryQueue, QueueMessage

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _msg(event_id: str = "salesforce:1") -> QueueMessage:
    return QueueMessage(
        event_id=event_id,
        identifiers=RawIdentifiers(email="a@b.com", country="US"),
        enqueued_at=NOW,
    )


def test_publish_then_receive_round_trips_through_bytes() -> None:
    q = MemoryQueue()
    q.publish(_msg())
    [delivery] = q.receive(max_messages=10, timeout_seconds=0)
    assert delivery.message == _msg()
    assert delivery.message is not _msg()  # a copy, deserialised from bytes
    assert delivery.attempt == 1


def test_receive_on_empty_queue_returns_empty_after_timeout() -> None:
    q = MemoryQueue()
    assert q.receive(max_messages=10, timeout_seconds=0.01) == []


def test_receive_respects_max_messages_and_preserves_order() -> None:
    q = MemoryQueue()
    for i in range(5):
        q.publish(_msg(f"e:{i}"))
    first = q.receive(max_messages=2, timeout_seconds=0)
    rest = q.receive(max_messages=10, timeout_seconds=0)
    assert [d.message.event_id for d in first] == ["e:0", "e:1"]
    assert [d.message.event_id for d in rest] == ["e:2", "e:3", "e:4"]


def test_ack_removes_message_for_good() -> None:
    q = MemoryQueue()
    q.publish(_msg())
    [delivery] = q.receive(10, 0)
    assert q.inflight_count() == 1
    q.ack(delivery)
    assert q.inflight_count() == 0
    assert q.ready_count() == 0
    assert q.receive(10, 0) == []


def test_nack_redelivers_with_incremented_attempt_at_the_back() -> None:
    q = MemoryQueue()
    q.publish(_msg("e:first"))
    q.publish(_msg("e:second"))
    [first] = q.receive(1, 0)
    q.nack(first)
    deliveries = q.receive(10, 0)
    assert [d.message.event_id for d in deliveries] == ["e:second", "e:first"]
    assert [d.attempt for d in deliveries] == [1, 2]


def test_double_ack_and_ack_after_nack_are_harmless() -> None:
    q = MemoryQueue()
    q.publish(_msg())
    [d] = q.receive(1, 0)
    q.ack(d)
    q.ack(d)
    q.nack(d)  # already acked: nothing to redeliver
    assert q.ready_count() == 0


def test_receive_wakes_when_a_message_is_published() -> None:
    q = MemoryQueue()
    got: list[str] = []

    def consumer() -> None:
        got.extend(d.message.event_id for d in q.receive(1, timeout_seconds=5))

    t = threading.Thread(target=consumer)
    t.start()
    q.publish(_msg("e:late"))
    t.join(timeout=2)
    assert got == ["e:late"]


def test_message_serialisation_is_stable() -> None:
    data = _msg().to_bytes()
    assert QueueMessage.from_bytes(data) == _msg()
    assert b'"event_id":"salesforce:1"' in data
