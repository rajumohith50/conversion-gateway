"""`gateway seed` end to end, in process: TestClient API, worker thread,
uploader thread, HTTP upload client against the mock in a uvicorn thread.
The same code path as `make seed`, without Docker."""

import io
import threading
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from gateway import worker
from gateway.api.app import create_app
from gateway.config import Settings
from gateway.queue import MemoryQueue
from gateway.seed import seed
from gateway.upload import uploader as upload_loop
from gateway.upload.backoff import RetryPolicy
from gateway.upload.client import HttpUploadClient
from gateway.upload.uploader import Uploader
from tests.conftest import HUBSPOT_SECRET, SALESFORCE_SECRET
from tests.upload.test_http_client import mock_url  # noqa: F401 - fixture


def test_seed_reaches_every_expected_state(
    settings: Settings,
    session_factory: sessionmaker[Session],
    mock_url: str,  # noqa: F811
) -> None:
    queue = MemoryQueue()
    api = TestClient(create_app(settings, session_factory, queue))
    stop = threading.Event()

    uploader = Uploader(
        session_factory,
        HttpUploadClient(mock_url, token="", timeout_seconds=5),
        RetryPolicy(
            max_attempts=3,
            max_elapsed_seconds=10,
            backoff_base_seconds=0.01,
            backoff_max_seconds=0.05,
        ),
        customer_id="1234567890",
        batch_size=10,
        batch_wait_seconds=0.2,
        row_retry_after=timedelta(seconds=60),
        stale_after=timedelta(seconds=600),
        max_row_attempts=3,
    )
    threads = [
        threading.Thread(
            target=worker.run,
            args=(queue, session_factory, stop),
            kwargs={"batch_size": 10, "poll_timeout_seconds": 0.05},
            daemon=True,
        ),
        threading.Thread(target=upload_loop.run, args=(uploader, stop, 0.05), daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        out = io.StringIO()
        ok = seed(
            "unused",
            SALESFORCE_SECRET,
            HUBSPOT_SECRET,
            out,
            wait_seconds=30,
            run_id="t1",
            client=api,
        )
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)

    report = out.getvalue()
    assert ok, report
    assert report.count("ok ") == 7
    assert "UPLOADED       UPLOADED" in report
    assert "SUPPRESSED     SUPPRESSED" in report
    assert "REJECTED       REJECTED       unparseable:phone" in report
    assert "DEAD_LETTERED  DEAD_LETTERED  permanent_row:CONVERSION_ACTION_NOT_FOUND" in report
    assert "http 200: duplicate" in report
    assert "http 401: nothing written" in report


def test_seed_reports_mismatch_when_nothing_processes(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    # No worker, no uploader: events stay QUEUED and the seed says so.
    api = TestClient(create_app(settings, session_factory, MemoryQueue()))
    out = io.StringIO()
    ok = seed(
        "unused", SALESFORCE_SECRET, HUBSPOT_SECRET, out, wait_seconds=0.5, run_id="t2", client=api
    )
    assert ok is False
    assert "MISMATCH" in out.getvalue()
    assert "!! " in out.getvalue()
