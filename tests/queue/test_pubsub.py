"""PubSubPublisher / PubSubConsumer against the emulator.

Runs only when PUBSUB_EMULATOR_HOST is set (see `make test`); otherwise
these are skipped, not failed, since the memory implementation covers the
processing logic and this file only proves the transport adapter.
"""

import os
import uuid
from datetime import UTC, datetime

import pytest

from gateway.normalise import RawIdentifiers
from gateway.queue import QueueMessage

pytestmark = pytest.mark.skipif(
    not os.environ.get("PUBSUB_EMULATOR_HOST"),
    reason="PUBSUB_EMULATOR_HOST not set; Pub/Sub emulator tests skipped",
)

PROJECT = "local-project"


@pytest.fixture
def topic_and_subscription() -> tuple[str, str]:
    # Fresh names per test so tests cannot see each other's messages.
    from gateway.queue.pubsub import ensure_topic_and_subscription

    suffix = uuid.uuid4().hex[:8]
    topic, sub = f"t-{suffix}", f"s-{suffix}"
    ensure_topic_and_subscription(PROJECT, topic, sub)
    # Idempotent: second call must not raise.
    ensure_topic_and_subscription(PROJECT, topic, sub)
    return topic, sub


def _msg(event_id: str) -> QueueMessage:
    return QueueMessage(
        event_id=event_id,
        identifiers=RawIdentifiers(email="a@b.com", country="US"),
        enqueued_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
    )


def test_publish_receive_ack(topic_and_subscription: tuple[str, str]) -> None:
    from gateway.queue.pubsub import PubSubConsumer, PubSubPublisher

    topic, sub = topic_and_subscription
    PubSubPublisher(PROJECT, topic).publish(_msg("hubspot:1"))

    consumer = PubSubConsumer(PROJECT, sub)
    [delivery] = consumer.receive(max_messages=10, timeout_seconds=5)
    assert delivery.message == _msg("hubspot:1")
    consumer.ack(delivery)
    assert consumer.receive(max_messages=10, timeout_seconds=1) == []


def test_nack_redelivers(topic_and_subscription: tuple[str, str]) -> None:
    from gateway.queue.pubsub import PubSubConsumer, PubSubPublisher

    topic, sub = topic_and_subscription
    PubSubPublisher(PROJECT, topic).publish(_msg("hubspot:2"))

    consumer = PubSubConsumer(PROJECT, sub)
    [first] = consumer.receive(10, 5)
    consumer.nack(first)
    [again] = consumer.receive(10, 5)
    assert again.message.event_id == "hubspot:2"
    consumer.ack(again)


def test_empty_subscription_times_out_cleanly(topic_and_subscription: tuple[str, str]) -> None:
    from gateway.queue.pubsub import PubSubConsumer

    _, sub = topic_and_subscription
    assert PubSubConsumer(PROJECT, sub).receive(10, timeout_seconds=1) == []


def test_wiring_builds_pubsub_objects_and_queue_init_creates_them() -> None:
    import io

    from gateway import cli
    from gateway.config import Settings
    from gateway.queue.pubsub import PubSubConsumer, PubSubPublisher
    from gateway.wiring import make_consumer, make_publisher

    suffix = uuid.uuid4().hex[:8]
    settings = Settings(
        queue_backend="pubsub",
        pubsub_project_id=PROJECT,
        pubsub_topic=f"t-{suffix}",
        pubsub_subscription=f"s-{suffix}",
    )
    out = io.StringIO()
    cli.queue_init(settings, out)
    assert "ready" in out.getvalue()

    publisher = make_publisher(settings)
    consumer = make_consumer(settings)
    assert isinstance(publisher, PubSubPublisher)
    assert isinstance(consumer, PubSubConsumer)
    publisher.publish(_msg("wired:1"))
    [delivery] = consumer.receive(1, 5)
    assert delivery.message.event_id == "wired:1"
    consumer.ack(delivery)


def test_wiring_memory_backend_returns_one_shared_object() -> None:
    from gateway.config import Settings
    from gateway.wiring import make_consumer, make_publisher

    settings = Settings(queue_backend="memory")
    assert make_publisher(settings) is make_consumer(settings)
