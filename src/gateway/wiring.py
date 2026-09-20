"""Build concrete queue objects from Settings.

The one place that knows both transports exist. The API asks for a
publisher, the worker for a consumer, and neither imports pubsub.py or
memory.py directly.
"""

from gateway.config import Settings
from gateway.queue import MemoryQueue, QueueConsumer, QueuePublisher

# With the memory backend, publisher and consumer must be the same object
# or messages go nowhere. One module-level instance per process is the
# only way to make that true; it is never used outside tests and demos.
_memory_queue = MemoryQueue()


def make_publisher(settings: Settings) -> QueuePublisher:
    if settings.queue_backend == "memory":
        return _memory_queue
    from gateway.queue.pubsub import PubSubPublisher

    return PubSubPublisher(settings.pubsub_project_id, settings.pubsub_topic)


def make_consumer(settings: Settings) -> QueueConsumer:
    if settings.queue_backend == "memory":
        return _memory_queue
    from gateway.queue.pubsub import PubSubConsumer

    return PubSubConsumer(settings.pubsub_project_id, settings.pubsub_subscription)
