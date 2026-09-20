"""verify() is pure, so every rule gets a direct test without HTTP."""

import pytest

from gateway.api.signature import SignatureFailure, compute_signature, verify

SECRET = "s3cret"
BODY = b'{"hello": "world"}'
NOW = 1_700_000_000.0


def _good_headers(ts: int = int(NOW)) -> tuple[str, str]:
    return str(ts), compute_signature(SECRET, str(ts), BODY)


def _verify(ts: str | None, sig: str | None, secret: str = SECRET, now: float = NOW):  # type: ignore[no-untyped-def]
    return verify(
        secret=secret,
        timestamp_header=ts,
        signature_header=sig,
        body=BODY,
        now=now,
        tolerance_seconds=300,
    )


def test_valid_request_passes() -> None:
    ts, sig = _good_headers()
    assert _verify(ts, sig) is None


def test_signature_header_is_case_insensitive_hex() -> None:
    ts, sig = _good_headers()
    assert _verify(ts, sig.upper().replace("SHA256=", "sha256=")) is None


def test_compute_signature_is_deterministic_and_prefixed() -> None:
    a = compute_signature(SECRET, "1", BODY)
    assert a == compute_signature(SECRET, "1", BODY)
    assert a.startswith("sha256=") and len(a) == 7 + 64


def test_timestamp_is_part_of_signed_string() -> None:
    # Same body, different timestamp: signature must change. This is what
    # stops a replay with a bumped timestamp.
    assert compute_signature(SECRET, "1", BODY) != compute_signature(SECRET, "2", BODY)


@pytest.mark.parametrize(
    ("ts", "sig", "expected"),
    [
        (None, _good_headers()[1], SignatureFailure.MISSING_TIMESTAMP),
        (_good_headers()[0], None, SignatureFailure.MISSING_SIGNATURE),
        ("not-a-number", _good_headers()[1], SignatureFailure.MALFORMED_TIMESTAMP),
        (_good_headers()[0], "md5=abc", SignatureFailure.MALFORMED_SIGNATURE),
        (_good_headers()[0], "sha256=tooshort", SignatureFailure.MALFORMED_SIGNATURE),
    ],
)
def test_structural_failures(ts: str | None, sig: str | None, expected: SignatureFailure) -> None:
    assert _verify(ts, sig) is expected


def test_wrong_secret_is_a_mismatch() -> None:
    ts, sig = _good_headers()
    assert _verify(ts, sig, secret="other") is SignatureFailure.SIGNATURE_MISMATCH


def test_tampered_body_is_a_mismatch() -> None:
    ts, sig = _good_headers()
    result = verify(
        secret=SECRET,
        timestamp_header=ts,
        signature_header=sig,
        body=BODY + b" ",
        now=NOW,
        tolerance_seconds=300,
    )
    assert result is SignatureFailure.SIGNATURE_MISMATCH


@pytest.mark.parametrize("skew", [-301, 301, -100_000, 100_000])
def test_timestamp_outside_window_is_rejected(skew: int) -> None:
    ts, sig = _good_headers(int(NOW) + skew)
    assert _verify(ts, sig) is SignatureFailure.TIMESTAMP_OUT_OF_WINDOW


@pytest.mark.parametrize("skew", [-300, 0, 300])
def test_timestamp_at_window_edge_is_accepted(skew: int) -> None:
    ts, sig = _good_headers(int(NOW) + skew)
    assert _verify(ts, sig) is None


def test_window_is_checked_before_signature() -> None:
    # An expired request with a bad signature reports the window, so the
    # cheaper check runs first and the metric names the more likely cause.
    ts = str(int(NOW) - 1000)
    assert _verify(ts, "sha256=" + "0" * 64) is SignatureFailure.TIMESTAMP_OUT_OF_WINDOW


def test_unconfigured_source_rejects_everything() -> None:
    ts, sig = _good_headers()
    assert _verify(ts, sig, secret="") is SignatureFailure.SOURCE_NOT_CONFIGURED
