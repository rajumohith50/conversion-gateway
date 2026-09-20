"""Queue adapter interface and implementations. Design section 12.

The API publishes, the worker consumes, and neither knows which transport is
underneath. Two implementations:

  MemoryQueue      in-process, for tests and single-process demos
  PubSubPublisher  Google Cloud Pub/Sub, against the emulator locally
  PubSubConsumer

Swapping transport is a Settings change (QUEUE_BACKEND) and nothing else.
"""

from gateway.queue.base import Delivery, QueueConsumer, QueueMessage, QueuePublisher
from gateway.queue.memory import MemoryQueue

__all__ = [
    "Delivery",
    "MemoryQueue",
    "QueueConsumer",
    "QueueMessage",
    "QueuePublisher",
]
