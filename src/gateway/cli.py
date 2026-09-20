"""Operator commands. `gateway <command>` after `uv sync`.

    worker       run the processing loop until SIGINT/SIGTERM
    reconcile    list events stuck in QUEUED; optionally re-enqueue the
                 ones that can be
    queue-init   create the Pub/Sub topic and subscription

Each command is a plain function taking its dependencies as arguments, so
tests call the function; main() only parses arguments and builds objects.
"""

import argparse
import logging
import signal
import sys
import threading
from datetime import UTC, datetime, timedelta
from typing import TextIO

from sqlalchemy.orm import Session, sessionmaker

from gateway import ledger, worker
from gateway.config import Settings
from gateway.db import make_engine, make_session_factory
from gateway.models.status import MatchKeyType
from gateway.normalise import RawIdentifiers
from gateway.queue import QueueConsumer, QueueMessage, QueuePublisher
from gateway.wiring import make_consumer, make_publisher


def run_worker(
    consumer: QueueConsumer, session_factory: sessionmaker[Session], settings: Settings
) -> None:
    stop = threading.Event()
    # Either signal sets the flag; the loop notices within one poll timeout
    # and exits after finishing the message it is on. No message is lost:
    # unacked messages redeliver.
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    worker.run(
        consumer,
        session_factory,
        stop,
        batch_size=settings.worker_batch_size,
        poll_timeout_seconds=settings.worker_poll_timeout_seconds,
    )


def reconcile(
    session_factory: sessionmaker[Session],
    publisher: QueuePublisher,
    older_than_seconds: int,
    republish: bool,
    now: datetime,
    out: TextIO,
) -> int:
    """Report events stuck in QUEUED. Returns the number found.

    Only click-id events can be re-enqueued from the ledger: their match
    key is on the row. Identifier-matched events had their PII on the lost
    message and nowhere else, by design, so the honest action is to ask the
    CRM to resend them. Both kinds are listed; only the first kind moves.
    """
    threshold = now - timedelta(seconds=older_than_seconds)
    with session_factory() as session:
        stuck = ledger.find_stuck_queued(session, threshold)
        rows = [
            (
                e.event_id,
                e.source,
                int((now - e.received_at).total_seconds()),
                e.match_key_type == MatchKeyType.CLICK_ID.value,
            )
            for e in stuck
        ]

    if not rows:
        out.write(f"no events stuck in QUEUED older than {older_than_seconds}s\n")
        return 0

    out.write(f"{'event_id':<40} {'source':<11} {'age_s':>7}  action\n")
    republished = 0
    for event_id, source, age, recoverable in rows:
        if recoverable and republish:
            publisher.publish(
                QueueMessage(event_id=event_id, identifiers=RawIdentifiers(), enqueued_at=now)
            )
            action = "republished"
            republished += 1
        elif recoverable:
            action = "recoverable (click id); rerun with --republish"
        else:
            action = "needs CRM resend (identifiers not retained)"
        out.write(f"{event_id:<40} {source:<11} {age:>7}  {action}\n")
    out.write(f"{len(rows)} stuck, {republished} republished\n")
    return len(rows)


def queue_init(settings: Settings, out: TextIO) -> None:
    if settings.queue_backend != "pubsub":
        out.write("queue backend is memory; nothing to create\n")
        return
    from gateway.queue.pubsub import ensure_topic_and_subscription

    ensure_topic_and_subscription(
        settings.pubsub_project_id, settings.pubsub_topic, settings.pubsub_subscription
    )
    out.write(
        f"topic {settings.pubsub_topic!r} and subscription {settings.pubsub_subscription!r} ready\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("worker", help="consume the queue and process events")
    rec = sub.add_parser("reconcile", help="find events stuck in QUEUED")
    rec.add_argument("--older-than", type=int, default=None, metavar="SECONDS")
    rec.add_argument("--republish", action="store_true")
    sub.add_parser("queue-init", help="create the Pub/Sub topic and subscription")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    # Plain stdlib logging until phase 6 replaces it with structlog and the
    # PII redaction filter. Nothing logged today carries identifiers.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.command == "queue-init":
        queue_init(settings, sys.stdout)
        return 0

    session_factory = make_session_factory(make_engine(settings.database_url))
    if args.command == "worker":
        run_worker(make_consumer(settings), session_factory, settings)
        return 0

    older_than = args.older_than or settings.reconcile_stuck_after_seconds
    found = reconcile(
        session_factory,
        make_publisher(settings),
        older_than,
        args.republish,
        datetime.now(UTC),
        sys.stdout,
    )
    # Non-zero when something is stuck, so a cron job or CI check can alert.
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
