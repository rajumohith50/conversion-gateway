"""Build the platform's conversion row from a ledger Event.

Pure: Event in, dict out, in the exact shape the upload endpoint takes.
The only place that knows the platform's field names.
"""

from datetime import datetime
from typing import Any

from gateway.models.ledger import Event


def format_conversion_time(when: datetime) -> str:
    """'yyyy-mm-dd hh:mm:ss+hh:mm', the format the API requires. Anything
    else (ISO 'T', 'Z', fractional seconds) is INVALID_CONVERSION_DATE_TIME."""
    return when.replace(microsecond=0).isoformat(sep=" ")


def conversion_action_resource(customer_id: str, conversion_action: str) -> str:
    return f"customers/{customer_id}/conversionActions/{conversion_action}"


def build_conversion(event: Event, customer_id: str) -> dict[str, Any]:
    assert event.conversion_action is not None
    assert event.conversion_time is not None

    row: dict[str, Any] = {
        "conversionAction": conversion_action_resource(customer_id, event.conversion_action),
        "conversionDateTime": format_conversion_time(event.conversion_time),
        # The platform dedupes on orderId. Setting it to our event id means
        # a re-sent batch (after a malformed response, say) cannot create
        # a second conversion. This is the "idempotency key on the
        # platform side" from design section 10.
        "orderId": event.event_id,
    }
    if event.conversion_value is not None:
        row["conversionValue"] = float(event.conversion_value)
    if event.currency:
        row["currencyCode"] = event.currency
    if event.click_id:
        row["gclid"] = event.click_id

    identifiers = _user_identifiers(event.hashed_identifiers or {})
    if identifiers:
        row["userIdentifiers"] = identifiers
        row["consent"] = {
            "adUserData": event.consent_ad_user_data,
            "adPersonalization": event.consent_ad_personalization,
        }
    return row


def _user_identifiers(hashed: dict[str, Any]) -> list[dict[str, Any]]:
    """Each identifier is its own object in the list, per the API. Only
    fields that are present are sent; a None would be rejected."""
    out: list[dict[str, Any]] = []
    if hashed.get("hashed_email"):
        out.append({"hashedEmail": hashed["hashed_email"]})
    if hashed.get("hashed_phone"):
        out.append({"hashedPhoneNumber": hashed["hashed_phone"]})

    address: dict[str, Any] = {}
    for ours, theirs in (
        ("hashed_given_name", "hashedFirstName"),
        ("hashed_family_name", "hashedLastName"),
        ("hashed_street_address", "hashedStreetAddress"),
        ("city", "city"),
        ("region", "state"),
        ("postal_code", "postalCode"),
        ("country", "countryCode"),
    ):
        if hashed.get(ours):
            address[theirs] = hashed[ours]
    # An address block is only a usable identifier with both names, a
    # postal code and a country; a lone city is noise the API rejects.
    if all(
        k in address for k in ("hashedFirstName", "hashedLastName", "postalCode", "countryCode")
    ):
        out.append({"addressInfo": address})
    return out
