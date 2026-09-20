"""The worker process: receive, process, ack. Nothing else.

Each message is processed in its own transaction. If the transaction
raises (database down, unexpected bug) the message is nacked and comes
back later; the ledger row is untouched because nothing committed. If it
commits, the message is acked. That ordering — commit, then ack — is what
makes delivery at-least-once: a crash between the two redelivers a message
whose row is already PROCESSED, and process_message() skips it.
"""

import logging
import threading
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from gateway.processor import ProcessOutcome, process_message
from gateway.queue import Delivery, QueueConsumer

log = logging.getLogger(__name__)


def handle_delivery(
    consumer: QueueConsumer, session_factory: sessionmaker[Session], delivery: Delivery
) -> ProcessOutcome | None:
    """Process one delivery. Returns the outcome, or None if processing
    raised and the message was nacked."""
    try:
        with session_factory.begin() as session:
            outcome = process_message(session, delivery.message, datetime.now(UTC))
    except Exception:
        log.exception(
            "processing failed, nacking",
            extra={"event_id": delivery.message.event_id, "attempt": delivery.attempt},
        )
        consumer.nack(delivery)
        return None

    consumer.ack(delivery)
    log.info("processed", extra={"event_id": delivery.message.event_id, "outcome": outcome.value})
    return outcome


def run(
    consumer: QueueConsumer,
    session_factory: sessionmaker[Session],
    stop: threading.Event,
    batch_size: int = 10,
    poll_timeout_seconds: float = 5.0,
) -> None:
    """Loop until `stop` is set. Each iteration is one receive() call, so
    a stop request is honoured within poll_timeout_seconds."""
    log.info("worker started")
    while not stop.is_set():
        for delivery in consumer.receive(batch_size, poll_timeout_seconds):
            handle_delivery(consumer, session_factory, delivery)
    log.info("worker stopped")
