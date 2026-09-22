"""reconcile and the argument parser."""

import io
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from gateway import cli, ledger, worker
from gateway.api import mappers
from gateway.config import Settings
from gateway.models.status import Source
from gateway.queue import MemoryQueue
from tests.api.payloads import salesforce_payload

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _stuck(session_factory: sessionmaker[Session], event_id: str, age: int, **lead: str) -> None:
    payload = salesforce_payload(EventId=event_id)
    payload["Lead"].update(lead)
    event = mappers.parse_and_map(Source.SALESFORCE, payload)
    with session_factory.begin() as session:
        ledger.record_validated(session, event, NOW - timedelta(seconds=age))
    # Deliberately NOT published: this is the failure mode reconcile exists for.


def test_reconcile_reports_nothing_when_clean(session_factory: sessionmaker[Session]) -> None:
    out = io.StringIO()
    found = cli.reconcile(session_factory, MemoryQueue(), 300, False, NOW, out)
    assert found == 0
    assert "no events stuck" in out.getvalue()


def test_reconcile_lists_only_events_older_than_threshold(
    session_factory: sessionmaker[Session],
) -> None:
    _stuck(session_factory, "old", age=600)
    _stuck(session_factory, "fresh", age=10)
    out = io.StringIO()
    found = cli.reconcile(session_factory, MemoryQueue(), 300, False, NOW, out)
    assert found == 1
    text = out.getvalue()
    assert "salesforce:old" in text
    assert "salesforce:fresh" not in text
    assert "recoverable; rerun with --republish" in text


def test_reconcile_distinguishes_recoverable_from_needs_resend(
    session_factory: sessionmaker[Session],
) -> None:
    _stuck(session_factory, "with-click", age=600)
    _stuck(session_factory, "no-click", age=600, GCLID__c="")
    out = io.StringIO()
    cli.reconcile(session_factory, MemoryQueue(), 300, False, NOW, out)
    lines = out.getvalue().splitlines()
    assert any("with-click" in line and "recoverable" in line for line in lines)
    assert any("no-click" in line and "needs CRM resend" in line for line in lines)


def test_reconcile_republish_recovers_click_id_events_end_to_end(
    session_factory: sessionmaker[Session],
) -> None:
    _stuck(session_factory, "with-click", age=600)
    _stuck(session_factory, "no-click", age=600, GCLID__c="")
    queue = MemoryQueue()
    out = io.StringIO()
    found = cli.reconcile(session_factory, queue, 300, True, NOW, out)
    assert found == 2
    assert "2 stuck, 1 republished" in out.getvalue()
    assert queue.ready_count() == 1

    # The republished message has no identifiers; the worker processes it
    # on the click id alone.
    [delivery] = queue.receive(1, 0)
    worker.handle_delivery(queue, session_factory, delivery)
    with session_factory() as session:
        recovered = ledger.get_event(session, "salesforce:with-click")
        left = ledger.get_event(session, "salesforce:no-click")
        assert recovered is not None and left is not None
        assert recovered.status == "PROCESSED"
        assert left.status == "QUEUED"


def test_queue_init_is_a_noop_for_memory_backend() -> None:
    out = io.StringIO()
    cli.queue_init(Settings(queue_backend="memory"), out)
    assert "nothing to create" in out.getvalue()


def test_reconcile_command_exit_code(
    session_factory: sessionmaker[Session],
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    from typer.testing import CliRunner

    from tests.conftest import TEST_URL

    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("QUEUE_BACKEND", "memory")
    runner = CliRunner()
    assert runner.invoke(cli.app, ["reconcile"]).exit_code == 0
    _stuck(session_factory, "old", age=100_000)
    result = runner.invoke(cli.app, ["reconcile", "--older-than", "60"])
    assert result.exit_code == 1
    assert "salesforce:old" in result.output
