"""Address component normalisation. Design section 5, last paragraph.

Only the street line is hashed. City and region are lowercased and sent in
the clear; postal code and country are sent as-is. This mirrors the platform's
matching rules: the coarse fields are used to narrow candidates, the street
digest is what actually has to match.
"""

from gateway.normalise.errors import NormalisationError
from gateway.normalise.hashing import sha256_hex
from gateway.normalise.name import _strip_accents, _strip_punctuation


def normalise_street(raw: str) -> str:
    """'  123 Main St., Apt #4 ' -> '123 main st apt 4'.

    Unlike names, internal whitespace is kept (collapsed to single spaces)
    because "12 3 main" and "123 main" are different addresses. Punctuation
    and accents are removed the same way as for names.
    """
    value = _strip_punctuation(_strip_accents(raw.strip().lower()))
    tokens = value.split()
    if not tokens:
        raise NormalisationError("street_address", "empty")
    return " ".join(tokens)


def hash_street(raw: str) -> str:
    return sha256_hex(normalise_street(raw))


def normalise_city(raw: str) -> str:
    value = " ".join(raw.strip().lower().split())
    if not value:
        raise NormalisationError("city", "empty")
    return value


def normalise_region(raw: str) -> str:
    value = " ".join(raw.strip().lower().split())
    if not value:
        raise NormalisationError("region", "empty")
    return value


def normalise_postal_code(raw: str) -> str:
    """Trimmed only. Postal formats vary too much across countries for any
    stricter rule to be safe, and the platform matches this field verbatim."""
    value = raw.strip()
    if not value:
        raise NormalisationError("postal_code", "empty")
    return value


def normalise_country(raw: str) -> str:
    """ISO 3166-1 alpha-2, upper-cased. This is the one field with a strict
    shape check, because it also drives phone parsing: a bad country here
    means every phone number on the record is parsed against the wrong plan."""
    value = raw.strip().upper()
    if not value:
        raise NormalisationError("country", "empty")
    if len(value) != 2 or not value.isalpha():
        raise NormalisationError("country", "not_alpha2")
    return value
