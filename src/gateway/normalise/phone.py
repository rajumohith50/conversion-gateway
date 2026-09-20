"""Phone normalisation. Design section 5, "Phone"."""

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

from gateway.normalise.rejection import RejectionReason


def normalise_phone(raw: str | None, default_region: str | None) -> str | RejectionReason:
    """Return the number in E.164 form, e.g. "+14155552671", or why it was rejected.

    `default_region` is the ISO 3166-1 alpha-2 country from the CRM record.
    It is only consulted when the input has no international prefix:
    "415 555 2671" is ambiguous on its own, but with region "US" it is not.
    It may be None if the record's country was itself rejected; in that case
    only numbers that carry their own "+CC" prefix can be normalised.

    We use the phonenumbers library rather than regex digit-stripping because
    "is this a real, dialable number for this country" is a genuinely hard
    question (variable lengths, trunk prefixes, mobile vs. fixed-line ranges)
    and getting it wrong produces a confident-looking digest that matches
    nothing.
    """
    if raw is None:
        return RejectionReason.MISSING

    value = raw.strip()
    if not value:
        return RejectionReason.EMPTY

    region = default_region.upper() if default_region is not None else None
    if region is None and not value.startswith("+"):
        return RejectionReason.NO_DEFAULT_REGION

    try:
        parsed = phonenumbers.parse(value, region)
    except NumberParseException:
        return RejectionReason.UNPARSEABLE

    # parse() is lenient: it will happily build an object from "123". is_valid
    # checks the number against the region's actual numbering plan.
    if not phonenumbers.is_valid_number(parsed):
        return RejectionReason.INVALID

    # E.164 includes the leading "+". Hashing "14155552671" instead of
    # "+14155552671" is a common silent mismatch, so the "+" stays.
    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
