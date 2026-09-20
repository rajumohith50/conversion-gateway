"""Canonical valid payloads for each source, used across the API tests.
Functions return fresh dicts so a test can mutate its copy freely."""

from typing import Any


def salesforce_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "EventId": "e-sf-0001",
        "EventTime": "2026-09-20T10:15:00Z",
        "Conversion_Action__c": "closed_won",
        "Amount": 1200.50,
        "CurrencyIsoCode": "USD",
        "Lead": {
            "Id": "00Q5e00000ABCDE",
            "Email": "M.Ohith+leads@Gmail.com",
            "Phone": "(415) 555-2671",
            "FirstName": "Mohith",
            "LastName": "Raju",
            "Street": "123 Main St",
            "City": "San Francisco",
            "StateCode": "CA",
            "PostalCode": "94103",
            "CountryCode": "US",
            "GCLID__c": "Cj0KCQjw-example",
            "Consent_Ad_User_Data__c": "GRANTED",
            "Consent_Ad_Personalization__c": "GRANTED",
            "SomeFieldWeIgnore": "x",
        },
    }
    payload.update(overrides)
    return payload


def hubspot_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eventId": 987654321,
        "objectId": 12345,
        "occurredAt": 1789899300000,  # 2026-09-20T10:15:00Z in epoch ms
        "conversionAction": "closed_won",
        "subscriptionType": "deal.propertyChange",
        "properties": {
            "email": "M.Ohith+leads@Gmail.com",
            "phone": "(415) 555-2671",
            "firstname": "Mohith",
            "lastname": "Raju",
            "address": "123 Main St",
            "city": "San Francisco",
            "state": "CA",
            "zip": "94103",
            "country": "US",
            "hs_google_click_id": "Cj0KCQjw-example",
            "amount": "1200.50",
            "deal_currency_code": "USD",
            "ad_user_data_consent": "GRANTED",
            "ad_personalization_consent": "GRANTED",
            "hs_object_id": "12345",
        },
    }
    payload.update(overrides)
    return payload
