import pytest

from gateway.normalise.email import hash_email, normalise_email
from gateway.normalise.errors import NormalisationError

# Fixture table: (input, expected canonical form). This table IS the spec.
NORMALISE_CASES = [
    ("mohith@example.com", "mohith@example.com"),
    ("  mohith@example.com  ", "mohith@example.com"),
    ("Mohith@Example.COM", "mohith@example.com"),
    ("\tMohith@Example.com\n", "mohith@example.com"),
    # Gmail: dots in the local part are ignored.
    ("m.o.hith@gmail.com", "mohith@gmail.com"),
    # Gmail: everything from the first "+" is a tag and is dropped.
    ("mohith+ads@gmail.com", "mohith@gmail.com"),
    ("mohith+a+b@gmail.com", "mohith@gmail.com"),
    # Both rules together, with case and whitespace.
    ("  M.Ohith+Campaign@GMAIL.com ", "mohith@gmail.com"),
    # googlemail.com is the same service.
    ("m.ohith+x@googlemail.com", "mohith@googlemail.com"),
    # Dots in the DOMAIN are never touched.
    ("mohith@mail.gmail.com", "mohith@mail.gmail.com"),
    # Non-Google domains keep dots and plus tags: these are distinct inboxes.
    ("m.ohith@outlook.com", "m.ohith@outlook.com"),
    ("mohith+ads@outlook.com", "mohith+ads@outlook.com"),
]

REJECT_CASES = [
    ("", "empty"),
    ("   ", "empty"),
    ("mohith", "malformed"),
    ("@example.com", "malformed"),
    ("mohith@", "malformed"),
    ("mo@hith@example.com", "malformed"),
    # Gmail address whose local part is nothing but a tag.
    ("+ads@gmail.com", "malformed"),
    ("...@gmail.com", "malformed"),
]


@pytest.mark.parametrize(("raw", "expected"), NORMALISE_CASES)
def test_normalise(raw: str, expected: str) -> None:
    assert normalise_email(raw) == expected


@pytest.mark.parametrize(("raw", "reason"), REJECT_CASES)
def test_reject(raw: str, reason: str) -> None:
    with pytest.raises(NormalisationError) as exc_info:
        normalise_email(raw)
    assert exc_info.value.field == "email"
    assert exc_info.value.reason == reason


def test_hash_is_of_normalised_form() -> None:
    # The whole point: two spellings of one inbox produce one digest.
    assert hash_email(" M.Ohith+x@Gmail.com ") == hash_email("mohith@gmail.com")


def test_hash_known_answer() -> None:
    # SHA-256("test@example.com"), independently verifiable with
    # `printf 'test@example.com' | shasum -a 256`.
    assert (
        hash_email("Test@Example.com")
        == "973dfe463ec85785f5f95af5ba3906eedb2d931c24e69824a89ea65dba4e813b"
    )


def test_rejection_message_contains_no_raw_value() -> None:
    # Section 9: no raw PII in error messages.
    with pytest.raises(NormalisationError) as exc_info:
        normalise_email("secret-person@")
    assert "secret-person" not in str(exc_info.value)
