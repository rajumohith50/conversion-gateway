import pytest
from pydantic import ValidationError

from gateway.normalise import (
    HashedIdentifiers,
    NormalisationError,
    RawIdentifiers,
    normalise_identifiers,
)
from gateway.normalise.hashing import sha256_hex

RAW_EMAIL = "M.Ohith+leads@Gmail.com"
RAW_PHONE = "(415) 555-2671"
RAW_GIVEN = "Dr. José"
RAW_FAMILY = "O'Brien Jr."
RAW_STREET = "123 Main St., Apt #4"


def full_record() -> RawIdentifiers:
    return RawIdentifiers(
        country="us",
        email=RAW_EMAIL,
        phone=RAW_PHONE,
        given_name=RAW_GIVEN,
        family_name=RAW_FAMILY,
        street_address=RAW_STREET,
        city=" San Francisco ",
        region="CA",
        postal_code=" 94103 ",
    )


def test_full_record_hashes_every_field() -> None:
    out = normalise_identifiers(full_record())

    assert out.country == "US"
    assert out.hashed_email == sha256_hex("mohith@gmail.com")
    assert out.hashed_phone == sha256_hex("+14155552671")
    assert out.hashed_given_name == sha256_hex("jose")
    assert out.hashed_family_name == sha256_hex("obrien")
    assert out.hashed_street_address == sha256_hex("123 main st apt 4")
    assert out.city == "san francisco"
    assert out.region == "ca"
    assert out.postal_code == "94103"


def test_output_contains_no_raw_values() -> None:
    # The security property of this module: serialise the output every way
    # the rest of the system will (dict, JSON) and check no raw input string
    # survives. Compare against lowercased raw values too, since a naive
    # implementation might "hash" by lowercasing.
    out = normalise_identifiers(full_record())
    serialised = out.model_dump_json().lower()

    for raw in (RAW_EMAIL, RAW_PHONE, RAW_GIVEN, RAW_FAMILY, RAW_STREET):
        assert raw.lower() not in serialised
    # Even the meaningful fragments should be gone.
    assert "mohith" not in serialised
    assert "4155552671" not in serialised
    assert "obrien" not in serialised
    assert "main st" not in serialised


def test_email_only_is_enough() -> None:
    out = normalise_identifiers(RawIdentifiers(country="US", email="a@b.com"))
    assert out.hashed_email is not None
    assert out.hashed_phone is None
    assert out.has_match_key()


def test_phone_only_uses_country_as_region() -> None:
    out = normalise_identifiers(RawIdentifiers(country="gb", phone="020 7946 0958"))
    assert out.hashed_phone == sha256_hex("+442079460958")


def test_full_address_without_email_or_phone_is_enough() -> None:
    out = normalise_identifiers(
        RawIdentifiers(country="US", given_name="A", family_name="B", postal_code="94103")
    )
    assert out.has_match_key()


def test_partial_address_alone_is_rejected() -> None:
    # A city on its own cannot be matched to anyone.
    with pytest.raises(NormalisationError) as exc_info:
        normalise_identifiers(RawIdentifiers(country="US", city="San Francisco"))
    assert exc_info.value.field == "identifiers"
    assert exc_info.value.reason == "no_match_key"


def test_country_only_is_rejected() -> None:
    with pytest.raises(NormalisationError) as exc_info:
        normalise_identifiers(RawIdentifiers(country="US"))
    assert exc_info.value.reason == "no_match_key"


def test_one_bad_field_rejects_whole_record_and_names_it() -> None:
    raw = RawIdentifiers(country="US", email="good@example.com", phone="not a phone")
    with pytest.raises(NormalisationError) as exc_info:
        normalise_identifiers(raw)
    assert exc_info.value.field == "phone"
    assert exc_info.value.reason == "unparseable"


def test_bad_country_is_reported_before_phone() -> None:
    # Country is validated first because phone parsing depends on it. A bad
    # country should be reported as such, not as a mysterious phone failure.
    raw = RawIdentifiers(country="USA", phone="415 555 2671")
    with pytest.raises(NormalisationError) as exc_info:
        normalise_identifiers(raw)
    assert exc_info.value.field == "country"


def test_models_are_frozen() -> None:
    raw = RawIdentifiers(country="US", email="a@b.com")
    with pytest.raises(ValidationError):
        raw.email = "x@y.com"  # type: ignore[misc]
    out = normalise_identifiers(raw)
    with pytest.raises(ValidationError):
        out.hashed_email = "tampered"  # type: ignore[misc]


def test_hashed_identifiers_is_a_pure_value() -> None:
    # Two normalisations of the same input are equal, so the ledger can be
    # compared/deduped on this value if ever needed.
    assert normalise_identifiers(full_record()) == normalise_identifiers(full_record())
    assert isinstance(normalise_identifiers(full_record()), HashedIdentifiers)
