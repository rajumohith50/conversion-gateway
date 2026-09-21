"""Error classification. Design section 7, "Error classification".

Two tables: HTTP status for whole-request failures, and per-row error codes
for partial failures. Both are data so the mapping can be read, argued
about, and extended without touching control flow.
"""

from enum import StrEnum


class FailureClass(StrEnum):
    TRANSIENT = "transient"
    PARTIAL = "partial"
    PERMANENT = "permanent"
    POISON = "poison"


# Whole-request HTTP statuses that mean "try again later". Everything else
# in 4xx is our fault (bad request, bad credentials, wrong URL) and
# retrying will not change the answer.
_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def classify_http_status(status_code: int) -> FailureClass:
    if status_code in _TRANSIENT_STATUSES:
        return FailureClass.TRANSIENT
    return FailureClass.PERMANENT


# Per-row error codes that can resolve on their own. TOO_RECENT_CONVERSION
# is the platform saying the click is not yet attributable (the conversion
# arrived within minutes of the click); the same row succeeds later.
# Everything else is a property of the row itself: a bad hash, an unknown
# action, an expired click. No amount of retrying fixes those, so they are
# dead-lettered individually and the rest of the batch is untouched.
_RETRYABLE_ROW_ERRORS = frozenset({"TOO_RECENT_CONVERSION"})


def classify_row_error(error_code: str) -> FailureClass:
    if error_code in _RETRYABLE_ROW_ERRORS:
        return FailureClass.TRANSIENT
    return FailureClass.PERMANENT
