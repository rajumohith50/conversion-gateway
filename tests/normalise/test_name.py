import pytest

from gateway.normalise.errors import NormalisationError
from gateway.normalise.name import hash_name, normalise_name

NORMALISE_CASES = [
    ("Smith", "smith"),
    ("  Smith  ", "smith"),
    ("SMITH", "smith"),
    # Accents fold to base letters.
    ("José", "jose"),
    ("Müller", "muller"),
    ("Łukasz", "łukasz"),  # Ł has no decomposition; NFKD leaves it alone
    ("Nguyễn", "nguyen"),
    # Punctuation is removed, not replaced with a space.
    ("O'Brien", "obrien"),
    ("Smith-Jones", "smithjones"),
    ("St. John", "stjohn"),
    # Titles and suffixes are dropped wherever they appear as whole tokens.
    ("Dr. Smith", "smith"),
    ("Mr Smith", "smith"),
    ("Smith Jr.", "smith"),
    ("Smith, III", "smith"),
    ("Prof. Dr. Smith PhD", "smith"),
    # Multi-part names collapse to one token.
    ("Mary Ann", "maryann"),
    ("van der Berg", "vanderberg"),
    # A strip token embedded in a longer word is not touched.
    ("Drake", "drake"),
    ("Mrsmith", "mrsmith"),
    # A surname that is also a title-ish word is NOT in the list.
    ("King", "king"),
]

REJECT_CASES = [
    ("", "empty"),
    ("   ", "empty"),
    ("Dr.", "empty"),
    ("Mr. Jr.", "empty"),
    ("...", "empty"),
]


@pytest.mark.parametrize(("raw", "expected"), NORMALISE_CASES)
def test_normalise(raw: str, expected: str) -> None:
    assert normalise_name(raw) == expected


@pytest.mark.parametrize(("raw", "reason"), REJECT_CASES)
def test_reject(raw: str, reason: str) -> None:
    with pytest.raises(NormalisationError) as exc_info:
        normalise_name(raw, field="given_name")
    assert exc_info.value.field == "given_name"
    assert exc_info.value.reason == reason


def test_field_label_defaults() -> None:
    with pytest.raises(NormalisationError) as exc_info:
        normalise_name("")
    assert exc_info.value.field == "name"


def test_hash_of_variants_is_equal() -> None:
    assert hash_name("Dr. José O'Brien Jr.") == hash_name("joseobrien")
