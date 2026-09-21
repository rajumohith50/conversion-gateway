"""parse_batch_response maps results and errors back to rows by index."""

import pytest

from gateway.upload.client import parse_batch_response


def _error(index: int, code: str) -> dict:  # type: ignore[type-arg]
    return {
        "errorCode": {"conversionUploadError": code},
        "message": f"row {index}: {code}",
        "location": {"fieldPathElements": [{"fieldName": "conversions", "index": index}]},
    }


def test_all_ok() -> None:
    result = parse_batch_response({"results": [{"gclid": "a"}, {"gclid": "b"}]}, expected_rows=2)
    assert [r.ok for r in result.rows] == [True, True]
    assert result.failed == []


def test_partial_failure_by_index() -> None:
    body = {
        "results": [{"gclid": "a"}, {}, {"gclid": "c"}, {}],
        "partialFailureError": {
            "code": 3,
            "details": [
                {"errors": [_error(1, "INVALID_USER_IDENTIFIER"), _error(3, "EXPIRED_CLICK")]}
            ],
        },
    }
    result = parse_batch_response(body, expected_rows=4)
    assert [r.ok for r in result.rows] == [True, False, True, False]
    assert [(r.index, r.error_code) for r in result.failed] == [
        (1, "INVALID_USER_IDENTIFIER"),
        (3, "EXPIRED_CLICK"),
    ]
    assert result.rows[1].message == "row 1: INVALID_USER_IDENTIFIER"


def test_failed_row_without_detail_is_unknown() -> None:
    result = parse_batch_response({"results": [{}]}, expected_rows=1)
    assert result.rows[0].error_code == "UNKNOWN"


def test_error_without_row_location_is_ignored() -> None:
    body = {
        "results": [{}],
        "partialFailureError": {
            "details": [{"errors": [{"errorCode": {"x": "Y"}, "message": "no location"}]}]
        },
    }
    assert parse_batch_response(body, expected_rows=1).rows[0].error_code == "UNKNOWN"


@pytest.mark.parametrize(
    "body",
    [
        {},  # no results key
        {"results": "nope"},
        {"results": [{}]},  # wrong count for expected_rows=2
        {"results": [{}, {}, {}]},
    ],
)
def test_malformed_bodies_raise(body: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises((ValueError, KeyError)):
        parse_batch_response(body, expected_rows=2)
