"""The queue contract.

Abstract base classes rather than typing.Protocol: an ABC fails loudly at
construction time if an implementation forgets a method, and the inheritance
is visible in the class line. Both are worth more here than structural
typing's flexibility.

The consumer is pull-based and synchronous. Pub/Sub's streaming-pull
callbacks would be faster at scale, but a loop that calls receive(), does
work, and calls ack() is something you can read top to bottom, and its
error handling has no hidden threads.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from gateway.normalise import RawIdentifiers


class QueueMessage(BaseModel):
    """What travels on the queue.

    Only the event id and the raw identifiers. Everything else about the
    event (consent, click id, conversion details) is already in the ledger,
    and the worker reads it from there so there is one source of truth.

    The identifiers are raw PII. They exist on the queue precisely because
    the ledger must not hold them (design section 4): this message is the
    only place they live between the webhook request ending and the worker
    hashing them.
    """

    model_config = ConfigDict(frozen=True)

    event_id: str
    identifiers: RawIdentifiers
    enqueued_at: datetime
    # Bound into the worker's log context so its lines join the API's.
    correlation_id: str | None = None

    def to_bytes(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "QueueMessage":
        return cls.model_validate_json(data)


@dataclass(frozen=True)
class Delivery:
    """One received message plus the handle needed to ack or nack it.
    `attempt` counts deliveries of this message where the transport
    exposes it, starting at 1."""

    ack_id: str
    message: QueueMessage
    attempt: int


class QueuePublisher(ABC):
    @abstractmethod
    def publish(self, message: QueueMessage) -> None:
        """Enqueue the message. Returns only once the transport has
        accepted it; raises if it did not. The caller decides what a
        failure means (the API logs it and leaves the event QUEUED for
        reconciliation)."""


class QueueConsumer(ABC):
    @abstractmethod
    def receive(self, max_messages: int, timeout_seconds: float) -> list[Delivery]:
        """Wait up to timeout_seconds for messages. Returns an empty list
        on timeout, never raises for "nothing there"."""

    @abstractmethod
    def ack(self, delivery: Delivery) -> None:
        """The message was handled; do not deliver it again."""

    @abstractmethod
    def nack(self, delivery: Delivery) -> None:
        """The message was not handled; deliver it again later."""
