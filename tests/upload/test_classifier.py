import pytest

from gateway.upload.classifier import FailureClass, classify_http_status, classify_row_error


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_statuses(status: int) -> None:
    assert classify_http_status(status) is FailureClass.TRANSIENT


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_permanent_statuses(status: int) -> None:
    # Retrying any of these burns quota and delays valid traffic (section 7).
    assert classify_http_status(status) is FailureClass.PERMANENT


def test_row_errors() -> None:
    assert classify_row_error("TOO_RECENT_CONVERSION") is FailureClass.TRANSIENT
    for code in (
        "INVALID_USER_IDENTIFIER",
        "CONVERSION_ACTION_NOT_FOUND",
        "EXPIRED_CLICK",
        "UNKNOWN",
    ):
        assert classify_row_error(code) is FailureClass.PERMANENT
