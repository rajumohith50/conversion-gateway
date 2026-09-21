"""RetryPolicy: wait bounds, stop conditions, and error mapping."""

import time

import pytest
from tenacity import RetryCallState

from gateway.upload.backoff import RetryPolicy, call_with_retry
from gateway.upload.errors import PermanentUploadError, PoisonUploadError, TransientUploadError

FAST = RetryPolicy(
    max_attempts=4, max_elapsed_seconds=10, backoff_base_seconds=0.001, backoff_max_seconds=0.002
)


def _state(attempt: int) -> RetryCallState:
    state = RetryCallState(None, None, (), {})
    state.attempt_number = attempt
    return state


@pytest.mark.parametrize("attempt", [1, 2, 3, 4, 5, 10])
def test_wait_is_full_jitter_within_exponential_cap(attempt: int) -> None:
    policy = RetryPolicy(backoff_base_seconds=1.0, backoff_max_seconds=30.0)
    wait = policy.wait()
    # Full jitter: uniform in [0, min(cap, base * 2**attempt)]. Sample
    # many times; every sample must be inside the bound.
    bound = min(30.0, 1.0 * 2**attempt)
    samples = [wait(_state(attempt)) for _ in range(200)]
    assert all(0 <= s <= bound for s in samples)
    # And it really is random, not the bound every time.
    assert len({round(s, 6) for s in samples}) > 10


def test_transient_errors_are_retried_then_succeed() -> None:
    calls = 0
    retries: list[tuple[str, int]] = []

    def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TransientUploadError(f"http_5{calls}9")
        return "ok"

    result = call_with_retry(FAST, fn, lambda exc, n: retries.append((exc.reason, n)))
    assert result == "ok"
    assert calls == 3
    # on_retry saw each failed attempt that was followed by another.
    assert retries == [("http_519", 1), ("http_529", 2)]


def test_exhausting_attempts_raises_poison_with_last_reason() -> None:
    calls = 0

    def fn() -> None:
        nonlocal calls
        calls += 1
        raise TransientUploadError("http_429")

    retries: list[int] = []
    with pytest.raises(PoisonUploadError) as exc_info:
        call_with_retry(FAST, fn, lambda _, n: retries.append(n))
    assert calls == 4
    assert exc_info.value.attempts == 4
    assert exc_info.value.reason == "http_429"
    # on_retry is NOT called for the final failure.
    assert retries == [1, 2, 3]


def test_permanent_error_is_not_retried() -> None:
    calls = 0

    def fn() -> None:
        nonlocal calls
        calls += 1
        raise PermanentUploadError("http_401")

    with pytest.raises(PermanentUploadError):
        call_with_retry(FAST, fn, lambda *_: None)
    assert calls == 1


def test_elapsed_ceiling_stops_before_attempt_ceiling() -> None:
    policy = RetryPolicy(
        max_attempts=100,
        max_elapsed_seconds=0.05,
        backoff_base_seconds=0.02,
        backoff_max_seconds=0.02,
    )

    def fn() -> None:
        raise TransientUploadError("timeout")

    started = time.monotonic()
    with pytest.raises(PoisonUploadError) as exc_info:
        call_with_retry(policy, fn, lambda *_: None)
    assert time.monotonic() - started < 1.0
    assert exc_info.value.attempts < 100
