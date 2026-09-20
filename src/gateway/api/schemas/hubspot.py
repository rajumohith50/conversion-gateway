"""HubSpot-shaped webhook payload.

HubSpot conventions: camelCase envelope, numeric ids, epoch-millisecond
timestamps, and a flat `properties` map of lowercase internal names whose
values are all strings on the wire (amounts included). Every one of those
differs from Salesforce, which is why there are two models and one mapper
each rather than one lenient model.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from gateway.consent import ConsentStatus


class HubSpotProperties(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str | None = None
    phone: str | None = None
    firstname: str | None = None
    lastname: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    country: str | None = None
    hs_google_click_id: str | None = None
    # Arrives as the string "1200.00"; Decimal parses it exactly, where a
    # float would not.
    amount: Decimal | None = None
    deal_currency_code: str | None = None
    ad_user_data_consent: ConsentStatus | None = None
    ad_personalization_consent: ConsentStatus | None = None


class HubSpotWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    event_id: int = Field(alias="eventId")
    object_id: int = Field(alias="objectId")
    # Epoch milliseconds, as HubSpot sends. Converted in the mapper.
    occurred_at: int = Field(alias="occurredAt", ge=0)
    conversion_action: str = Field(alias="conversionAction", min_length=1)
    properties: HubSpotProperties
