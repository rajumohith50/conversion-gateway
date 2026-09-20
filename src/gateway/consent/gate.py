"""Decide whether an event may be uploaded, based on its consent signals.

The gate is a pure function and fails closed. Design section 6:

  - Both GRANTED: upload with identifiers.
  - ad_user_data DENIED or UNSPECIFIED: suppress, record the reason.
  - Fields absent entirely: treat as UNSPECIFIED, suppress, and count it.

The truth table in tests/consent/test_gate.py is the specification.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ConsentStatus(StrEnum):
    """Mirrors the platform's consent enum. StrEnum so the value serialises
    to the ledger and to JSON as the plain string, e.g. "GRANTED"."""

    GRANTED = "GRANTED"
    DENIED = "DENIED"
    UNSPECIFIED = "UNSPECIFIED"


class SuppressionReason(StrEnum):
    """Why an event was not uploaded. Doubles as the label on the
    events_suppressed_total metric, so values are short and stable.

    Distinguishing DENIED from UNSPECIFIED from absent matters operationally:
    a spike in DENIED is users opting out (nothing to fix); a spike in
    *_MISSING is the client's tag not sending the field at all (a bug).
    """

    AD_USER_DATA_DENIED = "ad_user_data_denied"
    AD_USER_DATA_UNSPECIFIED = "ad_user_data_unspecified"
    AD_USER_DATA_MISSING = "ad_user_data_missing"
    AD_PERSONALIZATION_DENIED = "ad_personalization_denied"
    AD_PERSONALIZATION_UNSPECIFIED = "ad_personalization_unspecified"
    AD_PERSONALIZATION_MISSING = "ad_personalization_missing"


class ConsentSignals(BaseModel):
    """The two consent fields as they arrived on the event. `None` means the
    field was absent from the payload, which is a different situation from
    an explicit UNSPECIFIED and gets its own suppression reason."""

    model_config = ConfigDict(frozen=True)

    ad_user_data: ConsentStatus | None = None
    ad_personalization: ConsentStatus | None = None


class ConsentDecision(BaseModel):
    """`permitted` is the yes/no; `reason` is None when permitted and
    otherwise says which signal blocked the upload, for the ledger's
    status_reason column."""

    model_config = ConfigDict(frozen=True)

    permitted: bool
    reason: SuppressionReason | None = None


def _blocking_reason(
    status: ConsentStatus | None,
    denied: SuppressionReason,
    unspecified: SuppressionReason,
    missing: SuppressionReason,
) -> SuppressionReason | None:
    """Return why this one signal blocks upload, or None if it does not.
    Anything that is not an explicit GRANTED blocks; that is the
    fail-closed rule."""
    if status is ConsentStatus.GRANTED:
        return None
    if status is None:
        return missing
    if status is ConsentStatus.DENIED:
        return denied
    return unspecified


def evaluate_consent(signals: ConsentSignals) -> ConsentDecision:
    """Fail closed: upload only when both signals are explicitly GRANTED.

    ad_user_data is checked first because it is the signal that governs
    sending identifiers at all; if it blocks, the ad_personalization state
    is irrelevant and reporting it would be noise.
    """
    reason = _blocking_reason(
        signals.ad_user_data,
        denied=SuppressionReason.AD_USER_DATA_DENIED,
        unspecified=SuppressionReason.AD_USER_DATA_UNSPECIFIED,
        missing=SuppressionReason.AD_USER_DATA_MISSING,
    )
    if reason is not None:
        return ConsentDecision(permitted=False, reason=reason)

    reason = _blocking_reason(
        signals.ad_personalization,
        denied=SuppressionReason.AD_PERSONALIZATION_DENIED,
        unspecified=SuppressionReason.AD_PERSONALIZATION_UNSPECIFIED,
        missing=SuppressionReason.AD_PERSONALIZATION_MISSING,
    )
    if reason is not None:
        return ConsentDecision(permitted=False, reason=reason)

    return ConsentDecision(permitted=True)
