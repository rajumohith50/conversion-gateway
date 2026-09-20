"""Webhook authentication. Design section 9.

Pure functions over bytes and strings, no HTTP objects, so the rules can be
unit-tested without a running app.

Scheme:
    X-Webhook-Timestamp: <unix seconds>
    X-Webhook-Signature: sha256=<hex of HMAC-SHA256(secret, timestamp + "." + body)>

The timestamp is part of the signed string. If it were only a header, an
attacker holding a captured request could bump the timestamp to stay inside
the tolerance window and replay indefinitely. Signing it means any change
to the timestamp invalidates the signature.
"""

import hashlib
import hmac
from enum import Enum


class SignatureFailure(Enum):
    MISSING_TIMESTAMP = "missing_timestamp"
    MISSING_SIGNATURE = "missing_signature"
    MALFORMED_TIMESTAMP = "malformed_timestamp"
    MALFORMED_SIGNATURE = "malformed_signature"
    TIMESTAMP_OUT_OF_WINDOW = "timestamp_out_of_window"
    SIGNATURE_MISMATCH = "signature_mismatch"
    SOURCE_NOT_CONFIGURED = "source_not_configured"


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    """The value a correctly-behaving sender puts in the signature header."""
    message = timestamp.encode("ascii") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(
    *,
    secret: str,
    timestamp_header: str | None,
    signature_header: str | None,
    body: bytes,
    now: float,
    tolerance_seconds: int,
) -> SignatureFailure | None:
    """Return None if the request is authentic and fresh, else why not.

    Order matters slightly: cheap structural checks first, then the window,
    then the HMAC. All failures map to the same 401 at the HTTP layer; the
    distinct reasons exist for logs and metrics, so a spike in
    TIMESTAMP_OUT_OF_WINDOW (clock skew at the client) can be told apart
    from SIGNATURE_MISMATCH (wrong secret).
    """
    if not secret:
        return SignatureFailure.SOURCE_NOT_CONFIGURED
    if timestamp_header is None:
        return SignatureFailure.MISSING_TIMESTAMP
    if signature_header is None:
        return SignatureFailure.MISSING_SIGNATURE

    try:
        sent_at = int(timestamp_header)
    except ValueError:
        return SignatureFailure.MALFORMED_TIMESTAMP

    if not signature_header.startswith("sha256=") or len(signature_header) != len("sha256=") + 64:
        return SignatureFailure.MALFORMED_SIGNATURE

    # abs(): a timestamp far in the future is as suspicious as one in the
    # past, and clock skew runs both ways.
    if abs(now - sent_at) > tolerance_seconds:
        return SignatureFailure.TIMESTAMP_OUT_OF_WINDOW

    expected = compute_signature(secret, timestamp_header, body)
    # compare_digest runs in time independent of where the strings first
    # differ, so an attacker cannot learn the correct signature one byte at
    # a time from response latency.
    if not hmac.compare_digest(expected, signature_header.lower()):
        return SignatureFailure.SIGNATURE_MISMATCH

    return None
