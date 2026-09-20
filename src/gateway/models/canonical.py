"""The one internal event shape every CRM payload is mapped into.

Everything downstream of the mappers (ledger, queue, processor, upload)
speaks CanonicalLeadEvent and never sees a Salesforce or HubSpot field name.
Adding a third CRM is one schema and one mapper.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from gateway.consent import ConsentSignals
from gateway.models.status import MatchKeyType, Source
from gateway.normalise import RawIdentifiers


class CanonicalLeadEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Namespaced by source ("salesforce:00Q...") so that two CRMs with
    # overlapping id schemes cannot collide, while staying deterministic
    # from the CRM's own id, which is what makes it an idempotency key.
    event_id: str
    source: Source
    source_event_id: str

    conversion_action: str
    # When the outcome happened in the CRM, not when we received it.
    # Attribution windows are computed from this.
    conversion_time: datetime
    conversion_value: Decimal | None = None
    currency: str | None = None

    # Click-ID path. Not PII: it identifies an ad click, not a person.
    click_id: str | None = None

    # Raw PII. Lives only as long as the request that carried it (design
    # section 4); the ledger persists digests, never this field.
    identifiers: RawIdentifiers
    consent: ConsentSignals

    @field_validator("conversion_time")
    @classmethod
    def must_be_timezone_aware(cls, value: datetime) -> datetime:
        # A naive timestamp is ambiguous by up to a day across the CRM's
        # possible timezones, and the platform will attribute against it.
        # Better to reject at the boundary than to guess UTC.
        if value.tzinfo is None:
            raise ValueError("conversion_time must include a timezone offset")
        return value

    @property
    def match_key_type(self) -> MatchKeyType:
        # The platform prefers the click id when present: it is an exact
        # match, whereas identifiers are probabilistic on their side.
        if self.click_id:
            return MatchKeyType.CLICK_ID
        return MatchKeyType.USER_IDENTIFIERS
