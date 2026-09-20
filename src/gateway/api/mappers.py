"""Map each CRM's payload into CanonicalLeadEvent.

Each mapper is a plain function from the source model to the canonical one.
Nothing here validates business rules (that is the normaliser's job in
phase 3); this is purely "which field goes where".
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from gateway.api.schemas.hubspot import HubSpotWebhook
from gateway.api.schemas.salesforce import SalesforceWebhook
from gateway.consent import ConsentSignals
from gateway.models.canonical import CanonicalLeadEvent
from gateway.models.status import Source
from gateway.normalise import RawIdentifiers


def make_event_id(source: Source, source_event_id: str) -> str:
    return f"{source.value}:{source_event_id}"


def _present(value: str | None) -> str | None:
    """CRMs send "" for a field the user left blank. That means "not
    provided", which the normaliser represents as None, not as an empty
    value to be rejected."""
    if value is None or not value.strip():
        return None
    return value


def map_salesforce(payload: SalesforceWebhook) -> CanonicalLeadEvent:
    lead = payload.lead
    return CanonicalLeadEvent(
        event_id=make_event_id(Source.SALESFORCE, payload.event_id),
        source=Source.SALESFORCE,
        source_event_id=payload.event_id,
        conversion_action=payload.conversion_action,
        conversion_time=payload.event_time,
        conversion_value=payload.amount,
        currency=payload.currency,
        click_id=lead.gclid or None,
        identifiers=RawIdentifiers(
            email=_present(lead.email),
            phone=_present(lead.phone),
            given_name=_present(lead.first_name),
            family_name=_present(lead.last_name),
            street_address=_present(lead.street),
            city=_present(lead.city),
            region=_present(lead.state_code),
            postal_code=_present(lead.postal_code),
            country=_present(lead.country_code),
        ),
        consent=ConsentSignals(
            ad_user_data=lead.consent_ad_user_data,
            ad_personalization=lead.consent_ad_personalization,
        ),
    )


def map_hubspot(payload: HubSpotWebhook) -> CanonicalLeadEvent:
    props = payload.properties
    return CanonicalLeadEvent(
        event_id=make_event_id(Source.HUBSPOT, str(payload.event_id)),
        source=Source.HUBSPOT,
        source_event_id=str(payload.event_id),
        conversion_action=payload.conversion_action,
        # Epoch ms -> aware UTC datetime. HubSpot timestamps are always UTC.
        conversion_time=datetime.fromtimestamp(payload.occurred_at / 1000, tz=UTC),
        conversion_value=props.amount,
        currency=props.deal_currency_code,
        click_id=props.hs_google_click_id or None,
        identifiers=RawIdentifiers(
            email=_present(props.email),
            phone=_present(props.phone),
            given_name=_present(props.firstname),
            family_name=_present(props.lastname),
            street_address=_present(props.address),
            city=_present(props.city),
            region=_present(props.state),
            postal_code=_present(props.zip),
            country=_present(props.country),
        ),
        consent=ConsentSignals(
            ad_user_data=props.ad_user_data_consent,
            ad_personalization=props.ad_personalization_consent,
        ),
    )


def parse_and_map(source: Source, payload: Any) -> CanonicalLeadEvent:
    """Validate a decoded JSON body against the source's schema and map it.
    Raises pydantic.ValidationError; the route turns that into a 422 and a
    REJECTED ledger row."""
    if source is Source.SALESFORCE:
        return map_salesforce(SalesforceWebhook.model_validate(payload))
    return map_hubspot(HubSpotWebhook.model_validate(payload))


def extract_source_event_id(source: Source, payload: Any) -> str | None:
    """Best-effort read of the CRM event id from a payload that may have
    failed full validation. Used so a REJECTED row can carry the real id
    when there is one, making the rejection findable by the id the client
    has in their CRM logs."""
    if not isinstance(payload, dict):
        return None
    key = "EventId" if source is Source.SALESFORCE else "eventId"
    value = payload.get(key)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | str) and str(value).strip():
        return str(value)
    return None


def rejection_reasons(error: ValidationError) -> list[dict[str, str]]:
    """Turn a pydantic error into [{field, reason}] with NO input values.

    pydantic's own error list includes the offending input, which for a
    lead payload is a raw email or phone number. That must not reach the
    HTTP response or the ledger (design section 9), so only the location
    and the error type survive.
    """
    return [
        {
            "field": ".".join(str(part) for part in err["loc"]) or "body",
            "reason": err["type"],
        }
        for err in error.errors()
    ]
