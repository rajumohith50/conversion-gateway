"""Phone normalisation. Design section 5, "Phone"."""

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

from gateway.normalise.errors import NormalisationError
from gateway.normalise.hashing import sha256_hex


def normalise_phone(raw: str, default_region: str) -> str:
    """Return the number in E.164 form, e.g. "+14155552671".

    `default_region` is the ISO 3166-1 alpha-2 country from the CRM record.
    It is only consulted when the input has no international prefix:
    "415 555 2671" is ambiguous on its own, but with region "US" it is not.

    We use the phonenumbers library rather than regex digit-stripping because
    "is this a real, dialable number for this country" is a genuinely hard
    question (variable lengths, trunk prefixes, mobile vs. fixed-line ranges)
    and getting it wrong produces a confident-looking digest that matches
    nothing.
    """
    value = raw.strip()
    if not value:
        raise NormalisationError("phone", "empty")

    try:
        parsed = phonenumbers.parse(value, default_region.upper())
    except NumberParseException:
        raise NormalisationError("phone", "unparseable") from None

    # parse() is lenient: it will happily build an object from "123". is_valid
    # checks the number against the region's actual numbering plan.
    if not phonenumbers.is_valid_number(parsed):
        raise NormalisationError("phone", "invalid")

    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


def hash_phone(raw: str, default_region: str) -> str:
    # The digest covers the leading "+" (section 5). Hashing "14155552671"
    # instead of "+14155552671" is a common silent mismatch.
    return sha256_hex(normalise_phone(raw, default_region))
