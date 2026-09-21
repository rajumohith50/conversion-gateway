"""The mock's contract: shapes, row validation, and failure injection."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from mock_ads_api.app import create_app, validate_row

HEX = "a" * 64
ACTION = "customers/123/conversionActions/closed_won"


def good_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "gclid": "Cj0KCQjw-example",
        "conversionAction": ACTION,
        "conversionDateTime": "2026-09-20 10:15:00+00:00",
        "conversionValue": 1200.5,
        "currencyCode": "USD",
        "orderId": "salesforce:e-1",
        "userIdentifiers": [{"hashedEmail": HEX}, {"hashedPhoneNumber": HEX}],
        "consent": {"adUserData": "GRANTED", "adPersonalization": "GRANTED"},
    }
    row.update(overrides)
    return row


@pytest.fixture
def mock() -> TestClient:
    return TestClient(create_app())


def upload(client: TestClient, rows: list[dict[str, Any]]) -> Any:
    return client.post("/v1/conversions:upload", json={"conversions": rows, "partialFailure": True})


def test_all_good_rows_are_accepted(mock: TestClient) -> None:
    resp = upload(mock, [good_row(), good_row(orderId="salesforce:e-2")])
    assert resp.status_code == 200
    data = resp.json()
    assert "partialFailureError" not in data
    assert len(data["results"]) == 2
    assert data["results"][0]["orderId"] == "salesforce:e-1"
    assert mock.get("/uploads").json()["count"] == 2


def test_partial_failure_maps_errors_to_row_index(mock: TestClient) -> None:
    rows = [
        good_row(orderId="a"),
        good_row(orderId="b", conversionAction="bogus"),
        good_row(orderId="c"),
    ]
    data = upload(mock, rows).json()

    # One results entry per input row; failed rows are empty objects.
    assert [bool(r) for r in data["results"]] == [True, False, True]
    errors = data["partialFailureError"]["details"][0]["errors"]
    assert len(errors) == 1
    assert errors[0]["errorCode"] == {"conversionUploadError": "INVALID_CONVERSION_ACTION"}
    assert errors[0]["location"]["fieldPathElements"] == [{"fieldName": "conversions", "index": 1}]
    # Only the good rows were recorded.
    assert [u["orderId"] for u in mock.get("/uploads").json()["uploads"]] == ["a", "c"]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"conversionAction": "closed_won"}, "INVALID_CONVERSION_ACTION"),
        ({"conversionAction": "customers/1/conversionActions/nope"}, "CONVERSION_ACTION_NOT_FOUND"),
        ({"conversionDateTime": "2026-09-20T10:15:00Z"}, "INVALID_CONVERSION_DATE_TIME"),
        ({"gclid": None, "userIdentifiers": []}, "USER_IDENTIFIER_REQUIRED"),
        # The check that catches our own hashing bugs.
        ({"userIdentifiers": [{"hashedEmail": HEX.upper()}]}, "INVALID_USER_IDENTIFIER"),
        ({"userIdentifiers": [{"hashedEmail": HEX[:63]}]}, "INVALID_USER_IDENTIFIER"),
        ({"userIdentifiers": [{"hashedEmail": "mohith@gmail.com"}]}, "INVALID_USER_IDENTIFIER"),
        (
            {"userIdentifiers": [{"addressInfo": {"hashedFirstName": "x" * 64}}]},
            "INVALID_USER_IDENTIFIER",
        ),
        ({"consent": {"adUserData": "DENIED"}}, "CONSENT_REQUIRED_FOR_USER_IDENTIFIERS"),
        ({"consent": None}, "CONSENT_REQUIRED_FOR_USER_IDENTIFIERS"),
    ],
)
def test_row_validation(overrides: dict[str, Any], code: str) -> None:
    assert validate_row(good_row(**overrides), {"closed_won"}) == code


def test_click_only_row_needs_no_consent_or_identifiers() -> None:
    assert validate_row(good_row(userIdentifiers=[], consent=None), {"closed_won"}) is None


def test_injected_row_failure_overrides_validation(mock: TestClient) -> None:
    mock.post("/control", json={"fail_rows": [{"index": 0, "error_code": "CLICK_NOT_FOUND"}]})
    data = upload(mock, [good_row(), good_row()]).json()
    errors = data["partialFailureError"]["details"][0]["errors"]
    assert errors[0]["errorCode"]["conversionUploadError"] == "CLICK_NOT_FOUND"
    assert data["results"][1] != {}


@pytest.mark.parametrize("mode", ["http_401", "http_429", "http_500", "http_503"])
def test_injected_http_errors(mock: TestClient, mode: str) -> None:
    mock.post("/control", json={"mode": mode})
    resp = upload(mock, [good_row()])
    assert resp.status_code == int(mode.removeprefix("http_"))
    assert resp.json()["error"]["code"] == resp.status_code
    assert mock.get("/uploads").json()["count"] == 0


def test_malformed_mode_returns_non_json(mock: TestClient) -> None:
    mock.post("/control", json={"mode": "malformed"})
    resp = upload(mock, [good_row()])
    assert resp.status_code == 200
    with pytest.raises(ValueError):
        resp.json()


def test_times_limits_how_many_requests_are_affected(mock: TestClient) -> None:
    mock.post("/control", json={"mode": "http_429", "times": 2})
    assert upload(mock, [good_row()]).status_code == 429
    assert upload(mock, [good_row()]).status_code == 429
    assert upload(mock, [good_row()]).status_code == 200
    assert mock.get("/control").json()["mode"] == "ok"


def test_reset_and_clear(mock: TestClient) -> None:
    mock.post("/control", json={"mode": "http_500"})
    mock.delete("/control")
    assert upload(mock, [good_row()]).status_code == 200
    assert mock.get("/uploads").json()["count"] == 1
    mock.delete("/uploads")
    assert mock.get("/uploads").json()["count"] == 0


def test_bad_envelope_is_400(mock: TestClient) -> None:
    resp = mock.post("/v1/conversions:upload", json={"conversions": "nope"})
    assert resp.status_code == 400


def test_unknown_control_field_is_rejected(mock: TestClient) -> None:
    assert mock.post("/control", json={"mdoe": "http_500"}).status_code == 422


def test_env_factory(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from mock_ads_api.app import create_app_from_env

    monkeypatch.setenv("MOCK_KNOWN_CONVERSION_ACTIONS", "a,b")
    monkeypatch.setenv("MOCK_MODE", "http_503")
    app = create_app_from_env()
    assert app.state.mock.known_actions == {"a", "b"}
    assert app.state.mock.control.mode == "http_503"
