"""Assemble the full hashed identifier set from one CRM record.

This is the boundary where raw PII stops existing. The processor calls
build_user_identifiers() with a RawIdentifiers built from the webhook payload
and gets back a HashedIdentifiers that is safe to persist and log, plus the
list of every field that could not be normalised. Nothing below this
function ever sees a raw email again.
"""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from gateway.normalise.address import (
    normalise_city,
    normalise_country,
    normalise_postal_code,
    normalise_region,
    normalise_street,
)
from gateway.normalise.email import normalise_email
from gateway.normalise.hashing import sha256_hex
from gateway.normalise.name import normalise_name
from gateway.normalise.phone import normalise_phone
from gateway.normalise.rejection import FieldRejection, RejectionReason


class RawIdentifiers(BaseModel):
    """Identifier fields exactly as they came off the CRM record.

    Every field is optional. A field that is None is simply not part of the
    match key set; it is only reported as a rejection when something else
    depended on it (a national-format phone needs country).

    frozen=True so a RawIdentifiers cannot be mutated after construction and
    accidentally kept around; the intent is that it lives for exactly one
    call to build_user_identifiers().
    """

    model_config = ConfigDict(frozen=True)

    email: str | None = None
    phone: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    street_address: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None


class HashedIdentifiers(BaseModel):
    """What gets persisted to the ledger's `hashed_identifiers` column and
    sent to the upload API.

    Field names carry the `hashed_` prefix for a reason: anyone reading a log
    line or a DB row can tell at a glance that the value is a digest. The
    unprefixed fields (city, region, postal_code, country) are the ones the
    design says are sent in the clear.
    """

    model_config = ConfigDict(frozen=True)

    hashed_email: str | None = None
    hashed_phone: str | None = None
    hashed_given_name: str | None = None
    hashed_family_name: str | None = None
    hashed_street_address: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None

    def has_match_key(self) -> bool:
        """The platform needs at least one usable key: an email, a phone, or
        a full address (both names + postal code + country). A record with
        only a city is not matchable and should be rejected before upload,
        not after the API says so."""
        full_address = (
            self.hashed_given_name is not None
            and self.hashed_family_name is not None
            and self.postal_code is not None
            and self.country is not None
        )
        return self.hashed_email is not None or self.hashed_phone is not None or full_address


@dataclass(frozen=True)
class IdentifierBuildResult:
    """Both halves of the outcome. `identifiers` holds every field that
    normalised cleanly; `rejections` names every field that did not.

    `ok` is the processor's go/no-go: no rejections at all. Section 5 says
    a normalisation failure rejects the record, so a partially-good
    identifier set is returned for the ledger's benefit (so an operator can
    see what *would* have been sent) but is not meant to be uploaded."""

    identifiers: HashedIdentifiers
    rejections: tuple[FieldRejection, ...]

    @property
    def ok(self) -> bool:
        return not self.rejections


def _hash_or_none(value: str | RejectionReason) -> str | None:
    return None if isinstance(value, RejectionReason) else sha256_hex(value)


def _plain_or_none(value: str | RejectionReason) -> str | None:
    return None if isinstance(value, RejectionReason) else value


def build_user_identifiers(raw: RawIdentifiers) -> IdentifierBuildResult:
    """Normalise and hash every present field on the record.

    Every field is attempted even after an earlier one fails, so a record
    with a bad email AND a bad phone reports both. An operator fixing a
    client's data mapping wants the whole list, not one problem per retry.

    Country is normalised first because phone parsing depends on it.
    """
    rejections: list[FieldRejection] = []

    # Track outcome per field so the rejections list is built in one place.
    # Absent fields (None) are skipped entirely: they are not rejections,
    # they just do not contribute a key.
    outcomes: dict[str, str | RejectionReason] = {}

    country = normalise_country(raw.country)
    if raw.country is not None:
        outcomes["country"] = country

    if raw.email is not None:
        outcomes["email"] = normalise_email(raw.email)
    if raw.phone is not None:
        region = None if isinstance(country, RejectionReason) else country
        outcomes["phone"] = normalise_phone(raw.phone, region)
    if raw.given_name is not None:
        outcomes["given_name"] = normalise_name(raw.given_name)
    if raw.family_name is not None:
        outcomes["family_name"] = normalise_name(raw.family_name)
    if raw.street_address is not None:
        outcomes["street_address"] = normalise_street(raw.street_address)
    if raw.city is not None:
        outcomes["city"] = normalise_city(raw.city)
    if raw.region is not None:
        outcomes["region"] = normalise_region(raw.region)
    if raw.postal_code is not None:
        outcomes["postal_code"] = normalise_postal_code(raw.postal_code)

    for field, outcome in outcomes.items():
        if isinstance(outcome, RejectionReason):
            rejections.append(FieldRejection(field, outcome))

    absent = RejectionReason.MISSING
    identifiers = HashedIdentifiers(
        hashed_email=_hash_or_none(outcomes.get("email", absent)),
        hashed_phone=_hash_or_none(outcomes.get("phone", absent)),
        hashed_given_name=_hash_or_none(outcomes.get("given_name", absent)),
        hashed_family_name=_hash_or_none(outcomes.get("family_name", absent)),
        hashed_street_address=_hash_or_none(outcomes.get("street_address", absent)),
        city=_plain_or_none(outcomes.get("city", absent)),
        region=_plain_or_none(outcomes.get("region", absent)),
        postal_code=_plain_or_none(outcomes.get("postal_code", absent)),
        country=_plain_or_none(outcomes.get("country", absent)),
    )

    if not identifiers.has_match_key():
        rejections.append(FieldRejection("identifiers", RejectionReason.NO_MATCH_KEY))

    return IdentifierBuildResult(identifiers=identifiers, rejections=tuple(rejections))
