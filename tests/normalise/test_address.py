import pytest

from gateway.normalise.address import (
    hash_street,
    normalise_city,
    normalise_country,
    normalise_postal_code,
    normalise_region,
    normalise_street,
)
from gateway.normalise.errors import NormalisationError
from gateway.normalise.hashing import sha256_hex

STREET_CASES = [
    ("123 Main St", "123 main st"),
    ("  123 Main St.  ", "123 main st"),
    ("123 Main St., Apt #4", "123 main st apt 4"),
    ("123   Main\tSt", "123 main st"),  # internal whitespace collapsed, not removed
    ("12 Rue de l'Église", "12 rue de leglise"),
    ("1600 Amphitheatre Pkwy", "1600 amphitheatre pkwy"),
]


@pytest.mark.parametrize(("raw", "expected"), STREET_CASES)
def test_normalise_street(raw: str, expected: str) -> None:
    assert normalise_street(raw) == expected


def test_hash_street() -> None:
    assert hash_street("  123 Main St.  ") == sha256_hex("123 main st")


@pytest.mark.parametrize("raw", ["", "  ", "..."])
def test_street_rejects_empty(raw: str) -> None:
    with pytest.raises(NormalisationError) as exc_info:
        normalise_street(raw)
    assert exc_info.value.field == "street_address"
    assert exc_info.value.reason == "empty"


def test_city_and_region_are_lowercased_only() -> None:
    # Punctuation is kept: these are sent unhashed and the platform does its
    # own fuzzy matching on them.
    assert normalise_city("  San Francisco ") == "san francisco"
    assert normalise_city("St. Louis") == "st. louis"
    assert normalise_region("CA") == "ca"
    assert normalise_region(" New South Wales ") == "new south wales"


def test_postal_code_is_trimmed_only() -> None:
    assert normalise_postal_code(" 94043 ") == "94043"
    assert normalise_postal_code("SW1A 1AA") == "SW1A 1AA"  # case and space preserved


COUNTRY_CASES = [("us", "US"), (" GB ", "GB"), ("In", "IN")]


@pytest.mark.parametrize(("raw", "expected"), COUNTRY_CASES)
def test_country(raw: str, expected: str) -> None:
    assert normalise_country(raw) == expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [("", "empty"), ("USA", "not_alpha2"), ("U1", "not_alpha2"), ("United States", "not_alpha2")],
)
def test_country_rejects(raw: str, reason: str) -> None:
    with pytest.raises(NormalisationError) as exc_info:
        normalise_country(raw)
    assert exc_info.value.field == "country"
    assert exc_info.value.reason == reason


@pytest.mark.parametrize(
    ("fn", "field"),
    [
        (normalise_city, "city"),
        (normalise_region, "region"),
        (normalise_postal_code, "postal_code"),
    ],
)
def test_unhashed_fields_reject_empty(fn, field: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(NormalisationError) as exc_info:
        fn("   ")
    assert exc_info.value.field == field
    assert exc_info.value.reason == "empty"
