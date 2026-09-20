"""Google Cloud Pub/Sub transport.

Locally this talks to the emulator: the google-cloud-pubsub client reads
PUBSUB_EMULATOR_HOST from the environment and, when set, connects there
without credentials. Nothing in this file knows whether it is talking to
the emulator or the real service.
"""

from google.api_core import exceptions as gexc
from google.cloud import pubsub_v1

from gateway.queue.base import Delivery, QueueConsumer, QueueMessage, QueuePublisher

PUBLISH_TIMEOUT_SECONDS = 10.0


class PubSubPublisher(QueuePublisher):
    def __init__(self, project_id: str, topic: str) -> None:
        self._client = pubsub_v1.PublisherClient()
        self._topic_path = self._client.topic_path(project_id, topic)

    def publish(self, message: QueueMessage) -> None:
        # The client batches publishes and returns a future. We wait on it
        # here so that "publish returned" means "Pub/Sub has the message",
        # which is what QueuePublisher promises. The API handler can then
        # decide, synchronously, whether the event is safely queued.
        future = self._client.publish(self._topic_path, data=message.to_bytes())
        future.result(timeout=PUBLISH_TIMEOUT_SECONDS)


class PubSubConsumer(QueueConsumer):
    def __init__(self, project_id: str, subscription: str) -> None:
        self._client = pubsub_v1.SubscriberClient()
        self._subscription_path = self._client.subscription_path(project_id, subscription)

    def receive(self, max_messages: int, timeout_seconds: float) -> list[Delivery]:
        try:
            response = self._client.pull(
                request={"subscription": self._subscription_path, "max_messages": max_messages},
                timeout=timeout_seconds,
            )
        except gexc.DeadlineExceeded:
            # The server held the request open for the timeout and nothing
            # arrived. That is the normal idle path, not an error.
            return []
        return [
            Delivery(
                ack_id=received.ack_id,
                message=QueueMessage.from_bytes(received.message.data),
                attempt=received.delivery_attempt or 1,
            )
            for received in response.received_messages
        ]

    def ack(self, delivery: Delivery) -> None:
        self._client.acknowledge(
            request={"subscription": self._subscription_path, "ack_ids": [delivery.ack_id]}
        )

    def nack(self, delivery: Delivery) -> None:
        # Pub/Sub has no explicit nack. Setting the ack deadline to zero
        # tells the server we are done holding the message, and it
        # redelivers immediately.
        self._client.modify_ack_deadline(
            request={
                "subscription": self._subscription_path,
                "ack_ids": [delivery.ack_id],
                "ack_deadline_seconds": 0,
            }
        )


def ensure_topic_and_subscription(project_id: str, topic: str, subscription: str) -> None:
    """Create the topic and subscription if they do not exist. Idempotent.
    The emulator forgets everything on restart, so `make queue-init` runs
    this; in production it is a one-time provisioning step."""
    publisher = pubsub_v1.PublisherClient()
    subscriber = pubsub_v1.SubscriberClient()
    topic_path = publisher.topic_path(project_id, topic)
    subscription_path = subscriber.subscription_path(project_id, subscription)

    try:
        publisher.create_topic(request={"name": topic_path})
    except gexc.AlreadyExists:
        pass
    try:
        subscriber.create_subscription(
            request={
                "name": subscription_path,
                "topic": topic_path,
                # How long the worker has to ack before redelivery. Long
                # enough for a database round trip with retries, short
                # enough that a crashed worker's messages come back soon.
                "ack_deadline_seconds": 60,
            }
        )
    except gexc.AlreadyExists:
        pass
