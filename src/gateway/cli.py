"""Operator commands. `gateway --help` after `uv sync`.

    worker                 run the processing loop until SIGINT/SIGTERM
    uploader               run the upload loop until SIGINT/SIGTERM
    reconcile              list events stuck in QUEUED; --republish the
                           recoverable ones
    queue-init             create the Pub/Sub topic and subscription
    dlq list               dead-lettered events, filterable
    dlq show ID            one dead-letter record in full
    dlq replay             re-enqueue by --id or by the same filters as list

Every command body is a plain function taking its dependencies as
arguments so tests call the function; the typer layer only parses
arguments and builds objects.
"""

import json
import signal
import sys
import threading
from datetime import UTC, datetime, timedelta
from typing import Annotated, TextIO

import typer
from sqlalchemy.orm import Session, sessionmaker

from gateway import ledger, worker
from gateway.config import Settings
from gateway.db import make_engine, make_session_factory
from gateway.dlq import store as dlq_store
from gateway.models.status import MatchKeyType
from gateway.normalise import RawIdentifiers
from gateway.observability import configure_logging, metrics
from gateway.queue import QueueConsumer, QueueMessage, QueuePublisher
from gateway.upload import uploader as upload_loop
from gateway.upload.backoff import RetryPolicy
from gateway.upload.client import HttpUploadClient
from gateway.upload.uploader import Uploader
from gateway.wiring import make_consumer, make_publisher

app = typer.Typer(add_completion=False, no_args_is_help=True)
dlq_app = typer.Typer(no_args_is_help=True, help="Inspect and replay dead-lettered events.")
app.add_typer(dlq_app, name="dlq")


# --- Long-running processes ---------------------------------------------------


def _stop_on_signal() -> threading.Event:
    stop = threading.Event()
    # Either signal sets the flag; the loop notices within one poll and
    # exits after finishing the batch it is on. Nothing is lost: unacked
    # messages redeliver, and claimed rows are re-picked as stale.
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    return stop


def run_worker(
    consumer: QueueConsumer, session_factory: sessionmaker[Session], settings: Settings
) -> None:
    metrics.serve_metrics(settings.metrics_port)
    worker.run(
        consumer,
        session_factory,
        _stop_on_signal(),
        batch_size=settings.worker_batch_size,
        poll_timeout_seconds=settings.worker_poll_timeout_seconds,
    )


def build_uploader(session_factory: sessionmaker[Session], settings: Settings) -> Uploader:
    client = HttpUploadClient(
        settings.ads_api_base_url, settings.ads_api_token, settings.ads_api_timeout_seconds
    )
    policy = RetryPolicy(
        max_attempts=settings.upload_max_attempts,
        max_elapsed_seconds=settings.upload_max_elapsed_seconds,
        backoff_base_seconds=settings.upload_backoff_base_seconds,
        backoff_max_seconds=settings.upload_backoff_max_seconds,
    )
    return Uploader(
        session_factory,
        client,
        policy,
        customer_id=settings.ads_customer_id,
        batch_size=settings.upload_batch_size,
        batch_wait_seconds=settings.upload_batch_wait_seconds,
        row_retry_after=timedelta(seconds=settings.upload_row_retry_after_seconds),
        stale_after=timedelta(seconds=settings.upload_stale_after_seconds),
        max_row_attempts=settings.upload_max_attempts,
    )


def run_uploader(session_factory: sessionmaker[Session], settings: Settings) -> None:
    metrics.serve_metrics(settings.metrics_port)
    upload_loop.run(
        build_uploader(session_factory, settings),
        _stop_on_signal(),
        settings.upload_poll_interval_seconds,
    )


# --- reconcile ----------------------------------------------------------------


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
                e.match_key_type == MatchKeyType.CLICK_ID.value or e.hashed_identifiers is not None,
                e.correlation_id,
            )
            for e in stuck
        ]

    if not rows:
        out.write(f"no events stuck in QUEUED older than {older_than_seconds}s\n")
        return 0

    out.write(f"{'event_id':<40} {'source':<11} {'age_s':>7}  action\n")
    republished = 0
    for event_id, source, age, recoverable, correlation_id in rows:
        if recoverable and republish:
            publisher.publish(
                QueueMessage(
                    event_id=event_id,
                    identifiers=RawIdentifiers(),
                    enqueued_at=now,
                    correlation_id=correlation_id,
                )
            )
            action = "republished"
            republished += 1
        elif recoverable:
            action = "recoverable; rerun with --republish"
        else:
            action = "needs CRM resend (identifiers not retained)"
        out.write(f"{event_id:<40} {source:<11} {age:>7}  {action}\n")
    out.write(f"{len(rows)} stuck, {republished} republished\n")
    return len(rows)


# --- dlq ----------------------------------------------------------------------


def dlq_list(session_factory: sessionmaker[Session], flt: dlq_store.DlqFilter, out: TextIO) -> int:
    with session_factory() as session:
        rows = dlq_store.list_dead_letters(session, flt)
        if not rows:
            out.write("no dead-lettered events match\n")
            return 0
        out.write(
            f"{'id':>6}  {'dead_lettered_at':<25} {'source':<11} {'class':<9} "
            f"{'event_id':<32} reason\n"
        )
        for r in rows:
            replayed = " (replayed)" if r.replayed_at else ""
            out.write(
                f"{r.id:>6}  {r.dead_lettered_at.isoformat(timespec='seconds'):<25} "
                f"{r.source:<11} {r.failure_class:<9} {r.event_id:<32} {r.reason}{replayed}\n"
            )
        out.write(f"{len(rows)} shown\n")
        return len(rows)


def dlq_show(session_factory: sessionmaker[Session], dlq_id: int, out: TextIO) -> bool:
    with session_factory() as session:
        row = dlq_store.get_dead_letter(session, dlq_id)
        if row is None:
            out.write(f"no dead-letter record {dlq_id}\n")
            return False
        record = {
            "id": row.id,
            "event_id": row.event_id,
            "source": row.source,
            "conversion_action": row.conversion_action,
            "dead_lettered_at": row.dead_lettered_at.isoformat(),
            "failure_class": row.failure_class,
            "reason": row.reason,
            "platform_error_code": row.platform_error_code,
            "platform_error_message": row.platform_error_message,
            "replayed_at": row.replayed_at.isoformat() if row.replayed_at else None,
            "replay_count": row.replay_count,
            "payload": row.payload,
            "attempt_history": row.attempt_history,
        }
        out.write(json.dumps(record, indent=2) + "\n")
        return True


def dlq_replay(
    session_factory: sessionmaker[Session],
    publisher: QueuePublisher,
    flt: dlq_store.DlqFilter,
    dlq_id: int | None,
    now: datetime,
    out: TextIO,
) -> int:
    """Replay one record by id, or every open record matching the filter.
    Returns how many were replayed. Each replay is its own transaction so
    a publish failure on one does not roll back the others."""
    with session_factory() as session:
        if dlq_id is not None:
            row = dlq_store.get_dead_letter(session, dlq_id)
            candidates = [row.id] if row else []
        else:
            candidates = [r.id for r in dlq_store.list_dead_letters(session, flt, limit=1000)]

    replayed = 0
    for candidate in candidates:
        with session_factory.begin() as session:
            row = dlq_store.get_dead_letter(session, candidate)
            assert row is not None
            if dlq_store.replay(session, row, publisher, now):
                out.write(f"replayed {row.id} {row.event_id}\n")
                replayed += 1
            else:
                out.write(
                    f"skipped  {row.id} {row.event_id} (already replayed or not dead-lettered)\n"
                )
    out.write(f"{replayed} replayed of {len(candidates)} candidates\n")
    return replayed


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


# --- typer layer --------------------------------------------------------------


def _settings() -> Settings:
    settings = Settings()
    configure_logging(settings.log_level)
    return settings


def _session_factory(settings: Settings) -> sessionmaker[Session]:
    return make_session_factory(make_engine(settings.database_url))


def _parse_when(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@app.command("worker")
def worker_cmd() -> None:
    """Consume the queue and process events."""
    settings = _settings()
    run_worker(make_consumer(settings), _session_factory(settings), settings)


@app.command("uploader")
def uploader_cmd() -> None:
    """Upload PROCESSED events to the ad platform."""
    settings = _settings()
    run_uploader(_session_factory(settings), settings)


@app.command("reconcile")
def reconcile_cmd(
    older_than: Annotated[int | None, typer.Option(help="Seconds; default from settings")] = None,
    republish: Annotated[bool, typer.Option(help="Re-enqueue recoverable events")] = False,
) -> None:
    """Find events stuck in QUEUED. Exit 1 if any."""
    settings = _settings()
    found = reconcile(
        _session_factory(settings),
        make_publisher(settings),
        older_than or settings.reconcile_stuck_after_seconds,
        republish,
        datetime.now(UTC),
        sys.stdout,
    )
    raise typer.Exit(code=1 if found else 0)


@app.command("seed")
def seed_cmd(
    api_url: Annotated[str, typer.Option(envvar="SEED_API_URL")] = "http://localhost:8080",
    wait: Annotated[float, typer.Option(help="Seconds to wait for terminal states")] = 60.0,
) -> None:
    """Post a demo mix of webhooks and report where each one ended up."""
    from gateway.seed import seed

    settings = Settings()
    ok = seed(
        api_url,
        settings.webhook_secret_salesforce,
        settings.webhook_secret_hubspot,
        sys.stdout,
        wait_seconds=wait,
    )
    raise typer.Exit(code=0 if ok else 1)


@app.command("queue-init")
def queue_init_cmd() -> None:
    """Create the Pub/Sub topic and subscription."""
    queue_init(_settings(), sys.stdout)


def _filter_options(
    source: str | None,
    failure_class: str | None,
    reason: str | None,
    since: str | None,
    until: str | None,
    include_replayed: bool,
) -> dlq_store.DlqFilter:
    return dlq_store.DlqFilter(
        source=source,
        failure_class=failure_class,
        reason_prefix=reason,
        since=_parse_when(since),
        until=_parse_when(until),
        include_replayed=include_replayed,
    )


@dlq_app.command("list")
def dlq_list_cmd(
    source: Annotated[str | None, typer.Option()] = None,
    failure_class: Annotated[str | None, typer.Option(help="partial | permanent | poison")] = None,
    reason: Annotated[str | None, typer.Option(help="Prefix match, e.g. permanent_row:")] = None,
    since: Annotated[str | None, typer.Option(help="ISO 8601")] = None,
    until: Annotated[str | None, typer.Option(help="ISO 8601")] = None,
    include_replayed: Annotated[bool, typer.Option()] = False,
) -> None:
    """List dead-lettered events, newest first."""
    settings = _settings()
    flt = _filter_options(source, failure_class, reason, since, until, include_replayed)
    dlq_list(_session_factory(settings), flt, sys.stdout)


@dlq_app.command("show")
def dlq_show_cmd(dlq_id: int) -> None:
    """Show one dead-letter record as JSON."""
    settings = _settings()
    if not dlq_show(_session_factory(settings), dlq_id, sys.stdout):
        raise typer.Exit(code=1)


@dlq_app.command("replay")
def dlq_replay_cmd(
    id: Annotated[int | None, typer.Option("--id", help="One record by id")] = None,  # noqa: A002
    source: Annotated[str | None, typer.Option()] = None,
    failure_class: Annotated[str | None, typer.Option()] = None,
    reason: Annotated[str | None, typer.Option(help="Prefix match")] = None,
    since: Annotated[str | None, typer.Option()] = None,
    until: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Re-enqueue dead-lettered events, by --id or by filter."""
    if id is None and not any((source, failure_class, reason, since, until)):
        # Replaying everything by accident is the one mistake this command
        # must not allow.
        typer.echo("refusing to replay with no --id and no filter", err=True)
        raise typer.Exit(code=2)
    settings = _settings()
    flt = _filter_options(source, failure_class, reason, since, until, include_replayed=False)
    dlq_replay(
        _session_factory(settings),
        make_publisher(settings),
        flt,
        id,
        datetime.now(UTC),
        sys.stdout,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
