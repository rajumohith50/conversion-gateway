"""build_conversion: ledger row -> platform request row."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from gateway.models.ledger import Event
from gateway.upload.conversion import build_conversion, format_conversion_time

HEX = "b" * 64


def _event(**overrides: object) -> Event:
    fields: dict[str, object] = {
        "event_id": "salesforce:e-1",
        "source": "salesforce",
        "source_event_id": "e-1",
        "received_at": datetime(2026, 9, 20, 12, tzinfo=UTC),
        "conversion_action": "closed_won",
        "conversion_time": datetime(2026, 9, 20, 10, 15, 0, 123456, tzinfo=UTC),
        "conversion_value": Decimal("1200.500000"),
        "currency": "USD",
        "click_id": "Cj0KCQjw-example",
        "hashed_identifiers": {
            "hashed_email": HEX,
            "hashed_phone": HEX,
            "hashed_given_name": HEX,
            "hashed_family_name": HEX,
            "hashed_street_address": HEX,
            "city": "san francisco",
            "region": "ca",
            "postal_code": "94103",
            "country": "US",
        },
        "consent_ad_user_data": "GRANTED",
        "consent_ad_personalization": "GRANTED",
        "status": "PROCESSED",
        "attempt_count": 0,
    }
    fields.update(overrides)
    return Event(**fields)


def test_full_row() -> None:
    row = build_conversion(_event(), customer_id="42")
    assert row == {
        "conversionAction": "customers/42/conversionActions/closed_won",
        "conversionDateTime": "2026-09-20 10:15:00+00:00",
        "orderId": "salesforce:e-1",
        "conversionValue": 1200.5,
        "currencyCode": "USD",
        "gclid": "Cj0KCQjw-example",
        "userIdentifiers": [
            {"hashedEmail": HEX},
            {"hashedPhoneNumber": HEX},
            {
                "addressInfo": {
                    "hashedFirstName": HEX,
                    "hashedLastName": HEX,
                    "hashedStreetAddress": HEX,
                    "city": "san francisco",
                    "state": "ca",
                    "postalCode": "94103",
                    "countryCode": "US",
                }
            },
        ],
        "consent": {"adUserData": "GRANTED", "adPersonalization": "GRANTED"},
    }


def test_click_only_row_has_no_identifiers_or_consent() -> None:
    row = build_conversion(
        _event(hashed_identifiers=None, conversion_value=None, currency=None), "42"
    )
    assert "userIdentifiers" not in row
    assert "consent" not in row
    assert "conversionValue" not in row
    assert row["gclid"] == "Cj0KCQjw-example"


def test_partial_address_is_not_sent() -> None:
    hashed = {"hashed_email": HEX, "city": "sf", "hashed_given_name": HEX}
    row = build_conversion(_event(hashed_identifiers=hashed, click_id=None), "42")
    assert row["userIdentifiers"] == [{"hashedEmail": HEX}]
    assert "gclid" not in row


def test_conversion_time_format() -> None:
    assert (
        format_conversion_time(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
        == "2026-01-02 03:04:05+00:00"
    )
    ist = timezone(timedelta(hours=5, minutes=30))
    assert (
        format_conversion_time(datetime(2026, 1, 2, 3, 4, 5, 999, tzinfo=ist))
        == "2026-01-02 03:04:05+05:30"
    )
