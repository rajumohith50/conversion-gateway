"""Consent gate. Design section 6."""

from gateway.consent.gate import (
    ConsentDecision,
    ConsentSignals,
    ConsentStatus,
    SuppressionReason,
    evaluate_consent,
)

__all__ = [
    "ConsentDecision",
    "ConsentSignals",
    "ConsentStatus",
    "SuppressionReason",
    "evaluate_consent",
]
