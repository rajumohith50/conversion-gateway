"""dlq list / show / replay through both the functions and the typer app."""

import io
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from gateway import cli
from gateway.dlq import store as dlq
from gateway.queue import MemoryQueue
from gateway.upload.fake import FakeUploadClient, with_failures
from tests.upload.test_uploader import make_uploader, processed_events, statuses

T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _dead_letter_two(session_factory: sessionmaker[Session]) -> list[str]:
    ids = processed_events(session_factory, 3)
    client = FakeUploadClient(
        [with_failures(3, {0: "CONVERSION_ACTION_NOT_FOUND", 2: "INVALID_USER_IDENTIFIER"})]
    )
    make_uploader(session_factory, client).run_once(T0)
    return ids


def test_list_show_replay_functions(session_factory: sessionmaker[Session]) -> None:
    ids = _dead_letter_two(session_factory)
    out = io.StringIO()
    assert cli.dlq_list(session_factory, dlq.DlqFilter(), out) == 2
    text = out.getvalue()
    assert ids[0] in text and ids[2] in text and ids[1] not in text
    assert "permanent_row:CONVERSION_ACTION_NOT_FOUND" in text

    out = io.StringIO()
    assert (
        cli.dlq_list(session_factory, dlq.DlqFilter(reason_prefix="permanent_row:INVALID"), out)
        == 1
    )

    with session_factory() as session:
        row_id = dlq.list_dead_letters(session, dlq.DlqFilter(event_id=ids[0]))[0].id
    out = io.StringIO()
    assert cli.dlq_show(session_factory, row_id, out) is True
    shown = out.getvalue()
    assert '"failure_class": "partial"' in shown
    assert '"orderId"' in shown and "attempt_history" in shown
    assert cli.dlq_show(session_factory, 999_999, io.StringIO()) is False

    # Replay by id: only that one moves.
    queue = MemoryQueue()
    out = io.StringIO()
    assert cli.dlq_replay(session_factory, queue, dlq.DlqFilter(), row_id, T0, out) == 1
    assert statuses(session_factory, ids)[ids[0]][0] == "QUEUED"
    assert statuses(session_factory, ids)[ids[2]][0] == "DEAD_LETTERED"
    assert queue.ready_count() == 1

    # Replay by filter: the remaining one. Replaying again finds nothing open.
    out = io.StringIO()
    assert (
        cli.dlq_replay(
            session_factory, queue, dlq.DlqFilter(reason_prefix="permanent_row:"), None, T0, out
        )
        == 1
    )
    assert (
        cli.dlq_replay(
            session_factory, queue, dlq.DlqFilter(reason_prefix="permanent_row:"), None, T0, out
        )
        == 0
    )
    assert queue.ready_count() == 2


def test_empty_list(session_factory: sessionmaker[Session]) -> None:
    out = io.StringIO()
    assert cli.dlq_list(session_factory, dlq.DlqFilter(), out) == 0
    assert "no dead-lettered events" in out.getvalue()


def test_typer_commands(session_factory: sessionmaker[Session], monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import TEST_URL

    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("QUEUE_BACKEND", "memory")
    runner = CliRunner()
    ids = _dead_letter_two(session_factory)

    result = runner.invoke(cli.app, ["dlq", "list", "--failure-class", "partial"])
    assert result.exit_code == 0
    assert ids[0] in result.output and "2 shown" in result.output

    since = (T0 - timedelta(days=1)).isoformat()
    result = runner.invoke(cli.app, ["dlq", "list", "--since", since, "--until", "2026-09-21"])
    assert "no dead-lettered events" in result.output  # until is exclusive at midnight

    result = runner.invoke(cli.app, ["dlq", "show", "1"])
    assert result.exit_code == 0 and '"event_id"' in result.output
    assert runner.invoke(cli.app, ["dlq", "show", "4242"]).exit_code == 1

    # Safety: no filter and no id is refused.
    result = runner.invoke(cli.app, ["dlq", "replay"])
    assert result.exit_code == 2

    result = runner.invoke(cli.app, ["dlq", "replay", "--reason", "permanent_row:INVALID"])
    assert result.exit_code == 0
    assert "1 replayed of 1 candidates" in result.output
    assert statuses(session_factory, ids)[ids[2]][0] == "QUEUED"

    result = runner.invoke(cli.app, ["dlq", "replay", "--id", "1"])
    assert "1 replayed of 1" in result.output
    result = runner.invoke(cli.app, ["dlq", "replay", "--id", "1"])
    assert "skipped" in result.output and "0 replayed" in result.output


def test_queue_init_memory_noop() -> None:
    out = io.StringIO()
    from gateway.config import Settings

    cli.queue_init(Settings(queue_backend="memory"), out)
    assert "nothing to create" in out.getvalue()
