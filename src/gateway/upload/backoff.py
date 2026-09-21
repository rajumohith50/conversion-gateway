"""Retry policy: exponential backoff with full jitter, bounded by both an
attempt count and a wall-clock ceiling. Design section 7.

Full jitter (wait uniformly in [0, min(cap, base * 2**n)]) rather than
plain exponential: when many clients back off from the same 429 at once,
jitter spreads their retries out instead of having them all return
together and trigger the next 429.

Built on tenacity so the loop, the stop conditions, and the wait strategy
are the library's well-tested ones; this module only chooses them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from tenacity import (
    RetryCallState,
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    stop_after_delay,
    wait_random_exponential,
)

from gateway.upload.errors import PoisonUploadError, TransientUploadError

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 6
    max_elapsed_seconds: float = 600.0
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0

    def wait(self) -> wait_random_exponential:
        return wait_random_exponential(
            multiplier=self.backoff_base_seconds, max=self.backoff_max_seconds
        )

    def retrying(self, on_retry: Callable[[TransientUploadError, int], None]) -> Retrying:
        def before_sleep(state: RetryCallState) -> None:
            # Only called when another attempt WILL follow, so the callback
            # can record "attempt n failed, waiting" without ever being
            # told about the final failure (which raises instead).
            assert state.outcome is not None
            exc = state.outcome.exception()
            assert isinstance(exc, TransientUploadError)
            on_retry(exc, state.attempt_number)

        return Retrying(
            retry=retry_if_exception_type(TransientUploadError),
            wait=self.wait(),
            # Whichever ceiling is hit first ends the retrying.
            stop=stop_after_attempt(self.max_attempts) | stop_after_delay(self.max_elapsed_seconds),
            before_sleep=before_sleep,
            reraise=False,
        )


def call_with_retry(
    policy: RetryPolicy,
    fn: Callable[[], T],
    on_retry: Callable[[TransientUploadError, int], None],
) -> T:
    """Run fn() under the policy. Transient errors are retried; a permanent
    error propagates immediately; exhausting the policy raises
    PoisonUploadError carrying the last transient reason."""
    try:
        return policy.retrying(on_retry)(fn)
    except RetryError as exc:
        last = exc.last_attempt.exception()
        assert isinstance(last, TransientUploadError)
        raise PoisonUploadError(last.reason, attempts=exc.last_attempt.attempt_number) from last
