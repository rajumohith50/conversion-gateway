"""HttpUploadClient against the mock ads API over real HTTP.

The mock runs in a uvicorn thread on a free port for the session, so
timeouts, status codes and body parsing are exercised for real, not via an
in-process transport.
"""

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn

from gateway.upload.client import HttpUploadClient
from gateway.upload.errors import PermanentUploadError, TransientUploadError
from mock_ads_api.app import create_app
from tests.mock_ads_api.test_mock import good_row


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def mock_url() -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/healthz", timeout=0.2).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.05)
    yield url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def control(mock_url: str) -> httpx.Client:
    c = httpx.Client(base_url=mock_url)
    c.delete("/control")
    c.delete("/uploads")
    return c


@pytest.fixture
def client(mock_url: str) -> HttpUploadClient:
    return HttpUploadClient(mock_url, token="test-token", timeout_seconds=0.5)


def _rows(n: int) -> list[dict[str, Any]]:
    return [good_row(orderId=f"e-{i}") for i in range(n)]


def test_success(client: HttpUploadClient, control: httpx.Client) -> None:
    result = client.upload(_rows(2))
    assert [r.ok for r in result.rows] == [True, True]
    assert control.get("/uploads").json()["count"] == 2


def test_partial_failure_by_index(client: HttpUploadClient, control: httpx.Client) -> None:
    control.post("/control", json={"fail_rows": [{"index": 1, "error_code": "EXPIRED_CLICK"}]})
    result = client.upload(_rows(3))
    assert [r.ok for r in result.rows] == [True, False, True]
    assert result.rows[1].error_code == "EXPIRED_CLICK"
    assert [u["orderId"] for u in control.get("/uploads").json()["uploads"]] == ["e-0", "e-2"]


def test_mock_catches_a_bad_hash(client: HttpUploadClient, control: httpx.Client) -> None:
    rows = _rows(2)
    rows[0]["userIdentifiers"] = [{"hashedEmail": "A" * 64}]  # uppercase: our bug
    result = client.upload(rows)
    assert result.rows[0].error_code == "INVALID_USER_IDENTIFIER"
    assert result.rows[1].ok


@pytest.mark.parametrize("mode", ["http_429", "http_500", "http_503"])
def test_transient_statuses(client: HttpUploadClient, control: httpx.Client, mode: str) -> None:
    control.post("/control", json={"mode": mode})
    with pytest.raises(TransientUploadError) as exc_info:
        client.upload(_rows(1))
    assert exc_info.value.reason == mode
    assert control.get("/uploads").json()["count"] == 0


def test_permanent_status(client: HttpUploadClient, control: httpx.Client) -> None:
    control.post("/control", json={"mode": "http_401"})
    with pytest.raises(PermanentUploadError) as exc_info:
        client.upload(_rows(1))
    assert exc_info.value.reason == "http_401"


def test_timeout(client: HttpUploadClient, control: httpx.Client) -> None:
    control.post("/control", json={"mode": "timeout", "timeout_seconds": 2})
    started = time.monotonic()
    with pytest.raises(TransientUploadError) as exc_info:
        client.upload(_rows(1))
    assert exc_info.value.reason == "timeout"
    assert time.monotonic() - started < 1.5  # the client's 0.5s timeout, not the server's 2s


def test_malformed_response(client: HttpUploadClient, control: httpx.Client) -> None:
    control.post("/control", json={"mode": "malformed"})
    with pytest.raises(TransientUploadError) as exc_info:
        client.upload(_rows(1))
    assert exc_info.value.reason == "malformed_response"


def test_connection_refused_is_transient() -> None:
    client = HttpUploadClient(f"http://127.0.0.1:{_free_port()}", token="", timeout_seconds=0.5)
    with pytest.raises(TransientUploadError) as exc_info:
        client.upload(_rows(1))
    assert exc_info.value.reason.startswith("connection:")
    client.close()


def test_transient_then_ok_end_to_end_with_retry(
    client: HttpUploadClient, control: httpx.Client
) -> None:
    # The real retry policy over real HTTP: two 429s, then success.
    from gateway.upload.backoff import RetryPolicy, call_with_retry

    control.post("/control", json={"mode": "http_429", "times": 2})
    policy = RetryPolicy(
        max_attempts=5,
        max_elapsed_seconds=10,
        backoff_base_seconds=0.001,
        backoff_max_seconds=0.002,
    )
    seen: list[str] = []
    result = call_with_retry(
        policy, lambda: client.upload(_rows(2)), lambda exc, _: seen.append(exc.reason)
    )
    assert seen == ["http_429", "http_429"]
    assert all(r.ok for r in result.rows)
    assert control.get("/uploads").json() == control.get("/uploads").json()
    assert control.get("/uploads").json()["requests"] == 3
    assert control.get("/uploads").json()["count"] == 2  # accepted exactly once
