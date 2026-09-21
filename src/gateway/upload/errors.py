"""Whole-request upload failures, already classified.

The HTTP client raises one of these; nothing downstream inspects status
codes or exception types from httpx. Classification happens once, at the
edge, which is what section 7 means by "classification happens before the
retry decision, not after".
"""


class UploadError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class TransientUploadError(UploadError):
    """Retry with backoff: 429, 5xx, timeouts, connection failures, and a
    response we could not parse."""


class PermanentUploadError(UploadError):
    """Do not retry: the request itself is wrong (4xx other than 429).
    Retrying burns quota and delays valid traffic behind it."""


class PoisonUploadError(UploadError):
    """Transient failures past the attempt or time ceiling. Carries the
    last transient reason so the dead-letter record says what kept
    failing."""

    def __init__(self, reason: str, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(reason)
