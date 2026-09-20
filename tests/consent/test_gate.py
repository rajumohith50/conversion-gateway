import pytest

from gateway.consent import (
    ConsentDecision,
    ConsentSignals,
    ConsentStatus,
    SuppressionReason,
    evaluate_consent,
)

G = ConsentStatus.GRANTED
D = ConsentStatus.DENIED
U = ConsentStatus.UNSPECIFIED
R = SuppressionReason

# Full truth table: 4 states (GRANTED, DENIED, UNSPECIFIED, absent) x 4 states.
# (ad_user_data, ad_personalization, permitted, reason)
TRUTH_TABLE = [
    # Only this row uploads.
    (G, G, True, None),
    # ad_user_data blocks regardless of ad_personalization, and is reported
    # in preference to it.
    (D, G, False, R.AD_USER_DATA_DENIED),
    (D, D, False, R.AD_USER_DATA_DENIED),
    (D, U, False, R.AD_USER_DATA_DENIED),
    (D, None, False, R.AD_USER_DATA_DENIED),
    (U, G, False, R.AD_USER_DATA_UNSPECIFIED),
    (U, D, False, R.AD_USER_DATA_UNSPECIFIED),
    (U, U, False, R.AD_USER_DATA_UNSPECIFIED),
    (U, None, False, R.AD_USER_DATA_UNSPECIFIED),
    (None, G, False, R.AD_USER_DATA_MISSING),
    (None, D, False, R.AD_USER_DATA_MISSING),
    (None, U, False, R.AD_USER_DATA_MISSING),
    (None, None, False, R.AD_USER_DATA_MISSING),
    # ad_user_data granted, ad_personalization blocks.
    (G, D, False, R.AD_PERSONALIZATION_DENIED),
    (G, U, False, R.AD_PERSONALIZATION_UNSPECIFIED),
    (G, None, False, R.AD_PERSONALIZATION_MISSING),
]


@pytest.mark.parametrize(("user_data", "personalization", "permitted", "reason"), TRUTH_TABLE)
def test_truth_table(
    user_data: ConsentStatus | None,
    personalization: ConsentStatus | None,
    permitted: bool,
    reason: SuppressionReason | None,
) -> None:
    decision = evaluate_consent(
        ConsentSignals(ad_user_data=user_data, ad_personalization=personalization)
    )
    assert decision == ConsentDecision(permitted=permitted, reason=reason)


def test_truth_table_is_exhaustive() -> None:
    # Guard against someone adding a ConsentStatus value and forgetting to
    # extend the table: every combination must be covered.
    states: list[ConsentStatus | None] = [*ConsentStatus, None]
    covered = {(row[0], row[1]) for row in TRUTH_TABLE}
    assert covered == {(a, b) for a in states for b in states}


def test_defaults_are_absent_not_unspecified() -> None:
    # A payload with no consent block at all constructs with both None and
    # reports MISSING, which is the signal that the client tag is broken.
    decision = evaluate_consent(ConsentSignals())
    assert decision.permitted is False
    assert decision.reason is R.AD_USER_DATA_MISSING


def test_permitted_decision_has_no_reason() -> None:
    decision = evaluate_consent(ConsentSignals(ad_user_data=G, ad_personalization=G))
    assert decision.reason is None


def test_signals_parse_from_strings() -> None:
    # The webhook layer will hand Pydantic plain strings; they must coerce.
    signals = ConsentSignals.model_validate(
        {"ad_user_data": "GRANTED", "ad_personalization": "DENIED"}
    )
    assert signals.ad_user_data is G
    assert signals.ad_personalization is D


def test_unknown_status_string_is_rejected() -> None:
    # Fail closed extends to the enum: "granted" (wrong case) or "yes" is a
    # validation error, not a silent UNSPECIFIED.
    with pytest.raises(ValueError):
        ConsentSignals.model_validate({"ad_user_data": "yes"})


def test_enums_serialise_as_plain_strings() -> None:
    decision = evaluate_consent(ConsentSignals(ad_user_data=D))
    assert decision.model_dump() == {"permitted": False, "reason": "ad_user_data_denied"}
