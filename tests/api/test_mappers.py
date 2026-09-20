"""Both mappers produce the same canonical event from equivalent payloads."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from gateway.api import mappers
from gateway.consent import ConsentStatus
from gateway.models.status import MatchKeyType, Source
from tests.api.payloads import hubspot_payload, salesforce_payload

EXPECTED_TIME = datetime(2026, 9, 20, 10, 15, tzinfo=UTC)


def test_salesforce_maps_every_field() -> None:
    event = mappers.parse_and_map(Source.SALESFORCE, salesforce_payload())

    assert event.event_id == "salesforce:e-sf-0001"
    assert event.source is Source.SALESFORCE
    assert event.source_event_id == "e-sf-0001"
    assert event.conversion_action == "closed_won"
    assert event.conversion_time == EXPECTED_TIME
    assert event.conversion_value == Decimal("1200.50")
    assert event.currency == "USD"
    assert event.click_id == "Cj0KCQjw-example"
    assert event.match_key_type is MatchKeyType.CLICK_ID
    assert event.identifiers.email == "M.Ohith+leads@Gmail.com"
    assert event.identifiers.phone == "(415) 555-2671"
    assert event.identifiers.given_name == "Mohith"
    assert event.identifiers.family_name == "Raju"
    assert event.identifiers.street_address == "123 Main St"
    assert event.identifiers.region == "CA"
    assert event.identifiers.postal_code == "94103"
    assert event.identifiers.country == "US"
    assert event.consent.ad_user_data is ConsentStatus.GRANTED
    assert event.consent.ad_personalization is ConsentStatus.GRANTED


def test_hubspot_maps_every_field() -> None:
    event = mappers.parse_and_map(Source.HUBSPOT, hubspot_payload())

    assert event.event_id == "hubspot:987654321"
    assert event.source is Source.HUBSPOT
    assert event.source_event_id == "987654321"
    assert event.conversion_action == "closed_won"
    # Epoch milliseconds became an aware UTC datetime.
    assert event.conversion_time == EXPECTED_TIME
    # String amount became an exact Decimal.
    assert event.conversion_value == Decimal("1200.50")
    assert event.currency == "USD"
    assert event.click_id == "Cj0KCQjw-example"
    assert event.identifiers.email == "M.Ohith+leads@Gmail.com"
    assert event.identifiers.region == "CA"
    assert event.identifiers.postal_code == "94103"
    assert event.consent.ad_personalization is ConsentStatus.GRANTED


def test_both_sources_produce_identical_identifiers_and_consent() -> None:
    sf = mappers.parse_and_map(Source.SALESFORCE, salesforce_payload())
    hs = mappers.parse_and_map(Source.HUBSPOT, hubspot_payload())
    assert sf.identifiers == hs.identifiers
    assert sf.consent == hs.consent
    assert sf.conversion_time == hs.conversion_time


def test_missing_click_id_means_user_identifiers_match() -> None:
    payload = salesforce_payload()
    payload["Lead"]["GCLID__c"] = ""
    event = mappers.parse_and_map(Source.SALESFORCE, payload)
    assert event.click_id is None
    assert event.match_key_type is MatchKeyType.USER_IDENTIFIERS


def test_absent_consent_maps_to_none_not_unspecified() -> None:
    payload = hubspot_payload()
    del payload["properties"]["ad_user_data_consent"]
    event = mappers.parse_and_map(Source.HUBSPOT, payload)
    assert event.consent.ad_user_data is None


def test_unknown_consent_value_is_a_schema_error() -> None:
    payload = salesforce_payload()
    payload["Lead"]["Consent_Ad_User_Data__c"] = "yes"
    with pytest.raises(ValidationError):
        mappers.parse_and_map(Source.SALESFORCE, payload)


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        mappers.parse_and_map(
            Source.SALESFORCE, salesforce_payload(EventTime="2026-09-20T10:15:00")
        )


def test_extra_fields_are_ignored() -> None:
    # Both payload fixtures carry a field we do not model; neither raises.
    mappers.parse_and_map(Source.SALESFORCE, salesforce_payload())
    mappers.parse_and_map(Source.HUBSPOT, hubspot_payload())


@pytest.mark.parametrize(
    ("source", "payload", "expected"),
    [
        (Source.SALESFORCE, {"EventId": "abc"}, "abc"),
        (Source.HUBSPOT, {"eventId": 42}, "42"),
        (Source.SALESFORCE, {"eventId": "wrong-case"}, None),
        (Source.SALESFORCE, {"EventId": ""}, None),
        (Source.SALESFORCE, {"EventId": True}, None),
        (Source.HUBSPOT, ["not", "a", "dict"], None),
        (Source.HUBSPOT, "string", None),
    ],
)
def test_extract_source_event_id(source: Source, payload: object, expected: str | None) -> None:
    assert mappers.extract_source_event_id(source, payload) == expected


def test_rejection_reasons_contain_locations_and_types_but_no_values() -> None:
    payload = salesforce_payload()
    del payload["Lead"]["Id"]
    payload["Lead"]["Consent_Ad_User_Data__c"] = "secret-value-yes"
    with pytest.raises(ValidationError) as exc_info:
        mappers.parse_and_map(Source.SALESFORCE, payload)

    reasons = mappers.rejection_reasons(exc_info.value)
    assert {"field": "Lead.Id", "reason": "missing"} in reasons
    assert any(
        r["field"] == "Lead.Consent_Ad_User_Data__c" and r["reason"] == "enum" for r in reasons
    )
    assert "secret-value-yes" not in str(reasons)
    assert "Gmail.com" not in str(reasons)


def test_blank_strings_are_absent_not_empty() -> None:
    # CRMs send "" for fields the user left blank. Absent (None) is the
    # normaliser's "not provided"; "" would be a rejection.
    payload = salesforce_payload()
    payload["Lead"]["Phone"] = ""
    payload["Lead"]["Street"] = "   "
    event = mappers.parse_and_map(Source.SALESFORCE, payload)
    assert event.identifiers.phone is None
    assert event.identifiers.street_address is None
