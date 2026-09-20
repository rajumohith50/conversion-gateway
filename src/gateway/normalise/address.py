"""Address component normalisation. Design section 5, last paragraph.

Only the street line is hashed. City and region are lowercased and sent in
the clear; postal code and country are sent as-is. This mirrors the platform's
matching rules: the coarse fields are used to narrow candidates, the street
digest is what actually has to match.
"""

from gateway.normalise.name import strip_accents, strip_punctuation
from gateway.normalise.rejection import RejectionReason


def normalise_street(raw: str | None) -> str | RejectionReason:
    """'  123 Main St., Apt #4 ' -> '123 main st apt 4'.

    Unlike names, internal whitespace is kept (collapsed to single spaces)
    because "12 3 main" and "123 main" are different addresses. Punctuation
    and accents are removed the same way as for names.
    """
    if raw is None:
        return RejectionReason.MISSING
    value = strip_punctuation(strip_accents(raw.strip().lower()))
    tokens = value.split()
    if not tokens:
        return RejectionReason.EMPTY
    return " ".join(tokens)


def _lowercase_collapsed(raw: str | None) -> str | RejectionReason:
    if raw is None:
        return RejectionReason.MISSING
    value = " ".join(raw.strip().lower().split())
    if not value:
        return RejectionReason.EMPTY
    return value


def normalise_city(raw: str | None) -> str | RejectionReason:
    return _lowercase_collapsed(raw)


def normalise_region(raw: str | None) -> str | RejectionReason:
    return _lowercase_collapsed(raw)


def normalise_postal_code(raw: str | None) -> str | RejectionReason:
    """Trimmed only. Postal formats vary too much across countries for any
    stricter rule to be safe, and the platform matches this field verbatim."""
    if raw is None:
        return RejectionReason.MISSING
    value = raw.strip()
    if not value:
        return RejectionReason.EMPTY
    return value


def normalise_country(raw: str | None) -> str | RejectionReason:
    """ISO 3166-1 alpha-2, upper-cased. This is the one field with a strict
    shape check, because it also drives phone parsing: a bad country here
    means every phone number on the record is parsed against the wrong plan."""
    if raw is None:
        return RejectionReason.MISSING
    value = raw.strip().upper()
    if not value:
        return RejectionReason.EMPTY
    if len(value) != 2 or not value.isalpha():
        return RejectionReason.NOT_ALPHA2
    return value
