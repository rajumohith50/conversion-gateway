"""Typed rejection reasons for normalisation.

Design section 5: normalisation failures are rejections, not best-effort
passes. Each normaliser returns either the normalised string or one of these
reasons. Returning a value rather than raising is deliberate: the reason has
to travel to the ledger's status_reason column and the events_rejected_total
metric, and build_user_identifiers() needs to report every failing field on
a record, not just the first one it hit.
"""

from dataclasses import dataclass
from enum import Enum


class RejectionReason(Enum):
    """A plain Enum, not StrEnum, on purpose. Normalisers return
    `str | RejectionReason`, and callers branch with
    isinstance(result, RejectionReason). If this were a StrEnum every member
    would also be an instance of str and that check would be ambiguous.
    Use `.value` when writing to the ledger or a metric label."""

    # The field was absent (None) on the record.
    MISSING = "missing"
    # Present but nothing left after trimming, e.g. "" or "   ", or a name
    # that was nothing but titles ("Dr.").
    EMPTY = "empty"
    # Email is not shaped like local@domain.
    MALFORMED = "malformed"
    # Phone could not be parsed at all.
    UNPARSEABLE = "unparseable"
    # Phone parsed but is not a valid number for its region.
    INVALID = "invalid"
    # Phone was given in national format but there is no usable country on
    # the record to resolve it against.
    NO_DEFAULT_REGION = "no_default_region"
    # Country is not an ISO 3166-1 alpha-2 code.
    NOT_ALPHA2 = "not_alpha2"
    # Record-level: nothing on it is usable as a match key.
    NO_MATCH_KEY = "no_match_key"


@dataclass(frozen=True)
class FieldRejection:
    """A rejection attached to the field it came from, as reported by
    build_user_identifiers(). Carries the field NAME, never its value:
    this object ends up in logs and the ledger (section 9)."""

    field: str
    reason: RejectionReason
