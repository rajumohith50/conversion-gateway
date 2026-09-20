"""In-process queue. One object is both publisher and consumer.

Messages round-trip through bytes on publish, exactly as they would on a
real transport. That way a message that is not serialisable fails in the
unit tests rather than the first time it hits Pub/Sub.
"""

import threading
import uuid
from collections import deque

from gateway.queue.base import Delivery, QueueConsumer, QueueMessage, QueuePublisher


class MemoryQueue(QueuePublisher, QueueConsumer):
    def __init__(self) -> None:
        self._ready: deque[tuple[bytes, int]] = deque()
        self._inflight: dict[str, tuple[bytes, int]] = {}
        # A Condition rather than a bare Lock so receive() can block until
        # something is published instead of busy-polling.
        self._cond = threading.Condition()

    def publish(self, message: QueueMessage) -> None:
        with self._cond:
            self._ready.append((message.to_bytes(), 1))
            self._cond.notify()

    def receive(self, max_messages: int, timeout_seconds: float) -> list[Delivery]:
        with self._cond:
            if not self._ready:
                self._cond.wait(timeout=timeout_seconds)
            deliveries: list[Delivery] = []
            while self._ready and len(deliveries) < max_messages:
                data, attempt = self._ready.popleft()
                ack_id = uuid.uuid4().hex
                self._inflight[ack_id] = (data, attempt)
                deliveries.append(
                    Delivery(ack_id=ack_id, message=QueueMessage.from_bytes(data), attempt=attempt)
                )
            return deliveries

    def ack(self, delivery: Delivery) -> None:
        with self._cond:
            self._inflight.pop(delivery.ack_id, None)

    def nack(self, delivery: Delivery) -> None:
        # Back of the queue, attempt incremented: a poison message does not
        # starve everything behind it.
        with self._cond:
            entry = self._inflight.pop(delivery.ack_id, None)
            if entry is not None:
                data, attempt = entry
                self._ready.append((data, attempt + 1))
                self._cond.notify()

    # Introspection for tests and the reconcile command.
    def ready_count(self) -> int:
        with self._cond:
            return len(self._ready)

    def inflight_count(self) -> int:
        with self._cond:
            return len(self._inflight)
