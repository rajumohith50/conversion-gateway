"""Compose the per-field normalisers into one record-level function.

This is the boundary where raw PII stops existing. The processor calls
normalise_identifiers() with a RawIdentifiers built from the webhook payload
and gets back a HashedIdentifiers that is safe to persist and log. Nothing
below this function ever sees a raw email again.
"""

from pydantic import BaseModel, ConfigDict

from gateway.normalise.address import (
    hash_street,
    normalise_city,
    normalise_country,
    normalise_postal_code,
    normalise_region,
)
from gateway.normalise.email import hash_email
from gateway.normalise.errors import NormalisationError
from gateway.normalise.name import hash_name
from gateway.normalise.phone import hash_phone


class RawIdentifiers(BaseModel):
    """Identifier fields exactly as they came off the CRM record.

    Every field is optional except country. Country is required because it is
    the default region for phone parsing; without it a national-format number
    cannot be resolved to E.164 and we would rather reject than guess.

    frozen=True so that a RawIdentifiers cannot be mutated after construction
    and accidentally kept around; the intent is that it lives for exactly one
    call to normalise_identifiers().
    """

    model_config = ConfigDict(frozen=True)

    country: str
    email: str | None = None
    phone: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    street_address: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None


class HashedIdentifiers(BaseModel):
    """What gets persisted to the ledger's `hashed_identifiers` column and
    sent to the upload API.

    Field names carry the `hashed_` prefix for a reason: anyone reading a log
    line or a DB row can tell at a glance that the value is a digest. The
    unprefixed fields (city, region, postal_code, country) are the ones the
    design says are sent in the clear.
    """

    model_config = ConfigDict(frozen=True)

    country: str
    hashed_email: str | None = None
    hashed_phone: str | None = None
    hashed_given_name: str | None = None
    hashed_family_name: str | None = None
    hashed_street_address: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None

    def has_match_key(self) -> bool:
        """The platform needs at least one usable key: an email, a phone, or
        a full address (both names + postal code + country). A record with
        only a city is not matchable and should be rejected before upload,
        not after the API says so."""
        full_address = (
            self.hashed_given_name is not None
            and self.hashed_family_name is not None
            and self.postal_code is not None
        )
        return self.hashed_email is not None or self.hashed_phone is not None or full_address


def normalise_identifiers(raw: RawIdentifiers) -> HashedIdentifiers:
    """Normalise and hash every present field. Any single failure rejects the
    whole record (section 5: rejections, not best-effort), and the raised
    NormalisationError names which field.

    Country is normalised first because phone parsing depends on it.
    """
    country = normalise_country(raw.country)

    hashed = HashedIdentifiers(
        country=country,
        hashed_email=hash_email(raw.email) if raw.email is not None else None,
        hashed_phone=hash_phone(raw.phone, country) if raw.phone is not None else None,
        hashed_given_name=(
            hash_name(raw.given_name, "given_name") if raw.given_name is not None else None
        ),
        hashed_family_name=(
            hash_name(raw.family_name, "family_name") if raw.family_name is not None else None
        ),
        hashed_street_address=(
            hash_street(raw.street_address) if raw.street_address is not None else None
        ),
        city=normalise_city(raw.city) if raw.city is not None else None,
        region=normalise_region(raw.region) if raw.region is not None else None,
        postal_code=normalise_postal_code(raw.postal_code) if raw.postal_code is not None else None,
    )

    if not hashed.has_match_key():
        raise NormalisationError("identifiers", "no_match_key")

    return hashed
