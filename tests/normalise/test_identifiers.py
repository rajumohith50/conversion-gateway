"""build_user_identifiers: the record-level assembler."""

import pytest
from pydantic import ValidationError

from gateway.normalise import (
    FieldRejection,
    HashedIdentifiers,
    RawIdentifiers,
    RejectionReason,
    build_user_identifiers,
    sha256_hex,
)
from gateway.normalise.address import (
    normalise_city,
    normalise_country,
    normalise_postal_code,
    normalise_region,
)

R = RejectionReason

RAW_EMAIL = "M.Ohith+leads@Gmail.com"
RAW_PHONE = "(415) 555-2671"
RAW_GIVEN = "Dr. José"
RAW_FAMILY = "O'Brien Jr."
RAW_STREET = "123 Main St., Apt #4"


def full_record() -> RawIdentifiers:
    return RawIdentifiers(
        email=RAW_EMAIL,
        phone=RAW_PHONE,
        given_name=RAW_GIVEN,
        family_name=RAW_FAMILY,
        street_address=RAW_STREET,
        city=" San Francisco ",
        region="CA",
        postal_code=" 94103 ",
        country="us",
    )


def test_full_record_hashes_every_field_and_has_no_rejections() -> None:
    result = build_user_identifiers(full_record())

    assert result.ok
    assert result.rejections == ()
    ids = result.identifiers
    assert ids.hashed_email == sha256_hex("mohith@gmail.com")
    assert ids.hashed_phone == sha256_hex("+14155552671")
    assert ids.hashed_given_name == sha256_hex("jose")
    assert ids.hashed_family_name == sha256_hex("obrien")
    assert ids.hashed_street_address == sha256_hex("123 main st apt 4")
    assert ids.city == "san francisco"
    assert ids.region == "ca"
    assert ids.postal_code == "94103"
    assert ids.country == "US"


def test_output_contains_no_raw_values() -> None:
    # The security property of this module: serialise the output every way
    # the rest of the system will and check no raw input string survives.
    # Compare against lowercased raw values too, since a naive implementation
    # might "hash" by lowercasing.
    result = build_user_identifiers(full_record())
    serialised = result.identifiers.model_dump_json().lower()

    for raw in (RAW_EMAIL, RAW_PHONE, RAW_GIVEN, RAW_FAMILY, RAW_STREET):
        assert raw.lower() not in serialised
    # Even the meaningful fragments should be gone.
    assert "mohith" not in serialised
    assert "4155552671" not in serialised
    assert "obrien" not in serialised
    assert "main st" not in serialised


def test_rejections_carry_field_names_not_values() -> None:
    result = build_user_identifiers(RawIdentifiers(email="secret-person@", country="US"))
    assert result.rejections == (
        FieldRejection("email", R.MALFORMED),
        FieldRejection("identifiers", R.NO_MATCH_KEY),
    )
    assert "secret-person" not in repr(result.rejections)


def test_email_only_is_enough() -> None:
    result = build_user_identifiers(RawIdentifiers(email="a@b.com"))
    assert result.ok
    assert result.identifiers.hashed_email is not None
    assert result.identifiers.hashed_phone is None


def test_phone_only_uses_country_as_region() -> None:
    result = build_user_identifiers(RawIdentifiers(phone="020 7946 0958", country="gb"))
    assert result.ok
    assert result.identifiers.hashed_phone == sha256_hex("+442079460958")


def test_full_address_without_email_or_phone_is_enough() -> None:
    result = build_user_identifiers(
        RawIdentifiers(given_name="A", family_name="B", postal_code="94103", country="US")
    )
    assert result.ok


@pytest.mark.parametrize(
    "record",
    [
        RawIdentifiers(),
        RawIdentifiers(country="US"),
        RawIdentifiers(city="San Francisco", country="US"),
        # Names without postal code do not make a full address.
        RawIdentifiers(given_name="A", family_name="B", country="US"),
    ],
)
def test_no_usable_key_is_a_record_level_rejection(record: RawIdentifiers) -> None:
    result = build_user_identifiers(record)
    assert not result.ok
    assert result.rejections == (FieldRejection("identifiers", R.NO_MATCH_KEY),)


def test_every_failing_field_is_reported_not_just_the_first() -> None:
    raw = RawIdentifiers(email="nope", phone="hello", given_name="Dr.", country="US")
    result = build_user_identifiers(raw)
    assert not result.ok
    assert result.rejections == (
        FieldRejection("email", R.MALFORMED),
        FieldRejection("phone", R.UNPARSEABLE),
        FieldRejection("given_name", R.EMPTY),
        FieldRejection("identifiers", R.NO_MATCH_KEY),
    )


def test_good_fields_are_still_returned_alongside_rejections() -> None:
    # The ledger wants to show what would have been sent; the processor
    # decides (via .ok) whether it actually is.
    raw = RawIdentifiers(email="good@example.com", phone="hello", country="US")
    result = build_user_identifiers(raw)
    assert not result.ok
    assert result.rejections == (FieldRejection("phone", R.UNPARSEABLE),)
    assert result.identifiers.hashed_email == sha256_hex("good@example.com")
    assert result.identifiers.hashed_phone is None


def test_bad_country_is_reported_as_country_and_blocks_national_phone() -> None:
    # Country is validated first because phone parsing depends on it. The
    # phone is not silently parsed against a guessed region.
    raw = RawIdentifiers(phone="415 555 2671", country="USA")
    result = build_user_identifiers(raw)
    assert result.rejections == (
        FieldRejection("country", R.NOT_ALPHA2),
        FieldRejection("phone", R.NO_DEFAULT_REGION),
        FieldRejection("identifiers", R.NO_MATCH_KEY),
    )


def test_bad_country_does_not_block_international_phone() -> None:
    raw = RawIdentifiers(phone="+14155552671", country="USA")
    result = build_user_identifiers(raw)
    assert result.rejections == (FieldRejection("country", R.NOT_ALPHA2),)
    assert result.identifiers.hashed_phone == sha256_hex("+14155552671")


def test_absent_fields_are_not_rejections() -> None:
    # None means "the CRM did not send it", which is normal, not an error.
    result = build_user_identifiers(RawIdentifiers(email="a@b.com"))
    assert result.rejections == ()
    assert result.identifiers.country is None


def test_models_are_frozen() -> None:
    raw = RawIdentifiers(email="a@b.com")
    with pytest.raises(ValidationError):
        raw.email = "x@y.com"  # type: ignore[misc]
    ids = build_user_identifiers(raw).identifiers
    with pytest.raises(ValidationError):
        ids.hashed_email = "tampered"  # type: ignore[misc]


def test_result_is_a_pure_value() -> None:
    assert build_user_identifiers(full_record()) == build_user_identifiers(full_record())
    assert isinstance(build_user_identifiers(full_record()).identifiers, HashedIdentifiers)


# ---------------------------------------------------------------------------
# The unhashed address components, which are not in the main fixture table
# because they are pass-through with light cleanup.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("us", "US"),
        (" GB ", "GB"),
        ("In", "IN"),
        (None, R.MISSING),
        ("", R.EMPTY),
        ("USA", R.NOT_ALPHA2),
        ("U1", R.NOT_ALPHA2),
        ("United States", R.NOT_ALPHA2),
    ],
)
def test_country(raw: str | None, expected: str | RejectionReason) -> None:
    assert normalise_country(raw) == expected


def test_city_and_region_are_lowercased_and_collapsed_only() -> None:
    # Punctuation is kept: these are sent unhashed and the platform does its
    # own fuzzy matching on them.
    assert normalise_city("  San   Francisco ") == "san francisco"
    assert normalise_city("St. Louis") == "st. louis"
    assert normalise_region(" New South Wales ") == "new south wales"
    assert normalise_city(None) == R.MISSING
    assert normalise_region("  ") == R.EMPTY


def test_postal_code_is_trimmed_only() -> None:
    assert normalise_postal_code(" 94043 ") == "94043"
    assert normalise_postal_code("SW1A 1AA") == "SW1A 1AA"  # case and space preserved
    assert normalise_postal_code(None) == R.MISSING
    assert normalise_postal_code("  ") == R.EMPTY
