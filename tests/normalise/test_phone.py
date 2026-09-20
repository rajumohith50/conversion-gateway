import pytest

from gateway.normalise.errors import NormalisationError
from gateway.normalise.phone import hash_phone, normalise_phone

# (input, default_region, expected E.164)
NORMALISE_CASES = [
    # National formats resolve through the region.
    ("415 555 2671", "US", "+14155552671"),
    ("(415) 555-2671", "US", "+14155552671"),
    ("415.555.2671", "us", "+14155552671"),  # region is case-insensitive
    ("  415-555-2671  ", "US", "+14155552671"),
    # Already E.164: region is irrelevant.
    ("+14155552671", "US", "+14155552671"),
    ("+14155552671", "GB", "+14155552671"),
    # "00" international prefix (as dialled from the UK) is understood.
    ("0014155552671", "GB", "+14155552671"),
    # UK national format with the trunk "0" removed in E.164.
    ("020 7946 0958", "GB", "+442079460958"),
    ("+44 20 7946 0958", "US", "+442079460958"),
    # India mobile.
    ("098765 43210", "IN", "+919876543210"),
]

REJECT_CASES = [
    ("", "US", "empty"),
    ("   ", "US", "empty"),
    ("hello", "US", "unparseable"),
    # Too short to be a US number: parseable but not valid.
    ("123", "US", "invalid"),
    ("415 555", "US", "invalid"),
    # Valid-looking US number with a bogus area code (555 is reserved for
    # fiction but 415-555-2671 is in the assigned test range; 999 is not).
    ("999 555 2671", "US", "invalid"),
]


@pytest.mark.parametrize(("raw", "region", "expected"), NORMALISE_CASES)
def test_normalise(raw: str, region: str, expected: str) -> None:
    assert normalise_phone(raw, region) == expected


@pytest.mark.parametrize(("raw", "region", "reason"), REJECT_CASES)
def test_reject(raw: str, region: str, reason: str) -> None:
    with pytest.raises(NormalisationError) as exc_info:
        normalise_phone(raw, region)
    assert exc_info.value.field == "phone"
    assert exc_info.value.reason == reason


def test_hash_includes_leading_plus() -> None:
    from gateway.normalise.hashing import sha256_hex

    assert hash_phone("(415) 555-2671", "US") == sha256_hex("+14155552671")
    assert hash_phone("(415) 555-2671", "US") != sha256_hex("14155552671")


def test_same_number_different_spellings_hash_equal() -> None:
    assert hash_phone("415-555-2671", "US") == hash_phone("+1 415 555 2671", "GB")
