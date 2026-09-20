"""Salesforce-shaped webhook payload.

Salesforce conventions: PascalCase standard fields, custom fields suffixed
`__c`, ISO 8601 timestamps, a nested sObject. Field aliases keep the model
readable while accepting the wire names.

`extra="ignore"`: CRMs send many fields we do not use, and a new one
appearing must not start rejecting traffic.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from gateway.consent import ConsentStatus


class SalesforceLead(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str = Field(alias="Id")
    email: str | None = Field(default=None, alias="Email")
    phone: str | None = Field(default=None, alias="Phone")
    first_name: str | None = Field(default=None, alias="FirstName")
    last_name: str | None = Field(default=None, alias="LastName")
    street: str | None = Field(default=None, alias="Street")
    city: str | None = Field(default=None, alias="City")
    # StateCode / CountryCode are the ISO picklist fields; the free-text
    # State / Country fields hold names like "United States", which the
    # normaliser would rightly reject.
    state_code: str | None = Field(default=None, alias="StateCode")
    postal_code: str | None = Field(default=None, alias="PostalCode")
    country_code: str | None = Field(default=None, alias="CountryCode")
    gclid: str | None = Field(default=None, alias="GCLID__c")
    # Typed as the enum so "yes"/"true"/"granted" (wrong case) are schema
    # errors, not silently UNSPECIFIED. Fail closed at the boundary.
    consent_ad_user_data: ConsentStatus | None = Field(
        default=None, alias="Consent_Ad_User_Data__c"
    )
    consent_ad_personalization: ConsentStatus | None = Field(
        default=None, alias="Consent_Ad_Personalization__c"
    )


class SalesforceWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    event_id: str = Field(alias="EventId", min_length=1)
    event_time: datetime = Field(alias="EventTime")
    conversion_action: str = Field(alias="Conversion_Action__c", min_length=1)
    amount: Decimal | None = Field(default=None, alias="Amount")
    currency: str | None = Field(default=None, alias="CurrencyIsoCode")
    lead: SalesforceLead = Field(alias="Lead")
