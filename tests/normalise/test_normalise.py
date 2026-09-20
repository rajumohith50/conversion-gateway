"""Fixture tables for every normaliser. These tables ARE the specification.

Each row is (input, expected_normalised, expected_digest):
  - input:               exactly what the CRM sent, including None
  - expected_normalised: the canonical string, or the RejectionReason
  - expected_digest:     SHA-256 hex of the canonical string, or None when
                         rejected (a rejected value never produces a digest)

Digests are literal, not computed in the test, so the table can be checked
against an external tool: `printf 'mohith@gmail.com' | shasum -a 256`.
"""

import pytest

from gateway.normalise import (
    RejectionReason,
    normalise_email,
    normalise_name,
    normalise_phone,
    normalise_street,
    sha256_hex,
)

R = RejectionReason

# fmt: off
# ---------------------------------------------------------------------------
# Email. Section 5: trim, lowercase, gmail/googlemail dot + plus handling.
# ---------------------------------------------------------------------------
EMAIL_CASES: list[tuple[str | None, str | RejectionReason, str | None]] = [
    # Plain.
    ("mohith@example.com",          "mohith@example.com",   "be0d0b9b85e1260330f8a52ae5698f4cfe99b0ce488d201f4d0a293bd4f6ca74"),
    # Whitespace.
    ("  mohith@example.com  ",      "mohith@example.com",   "be0d0b9b85e1260330f8a52ae5698f4cfe99b0ce488d201f4d0a293bd4f6ca74"),
    ("\tmohith@example.com\n",      "mohith@example.com",   "be0d0b9b85e1260330f8a52ae5698f4cfe99b0ce488d201f4d0a293bd4f6ca74"),
    # Mixed case.
    ("Mohith@Example.COM",          "mohith@example.com",   "be0d0b9b85e1260330f8a52ae5698f4cfe99b0ce488d201f4d0a293bd4f6ca74"),
    # Gmail dots in the local part are ignored.
    ("m.o.hith@gmail.com",          "mohith@gmail.com",     "acf14504ffef2b4bcb0d8b78d10014ce8e4371b3f2baaaf91ea587e90019ddec"),
    # Gmail plus addressing: everything from the first "+" is dropped.
    ("mohith+ads@gmail.com",        "mohith@gmail.com",     "acf14504ffef2b4bcb0d8b78d10014ce8e4371b3f2baaaf91ea587e90019ddec"),
    ("mohith+a+b@gmail.com",        "mohith@gmail.com",     "acf14504ffef2b4bcb0d8b78d10014ce8e4371b3f2baaaf91ea587e90019ddec"),
    # All gmail rules plus case and whitespace at once.
    ("  M.Ohith+Campaign@GMAIL.com ", "mohith@gmail.com",   "acf14504ffef2b4bcb0d8b78d10014ce8e4371b3f2baaaf91ea587e90019ddec"),
    # googlemail.com is the same service and gets the same treatment.
    ("m.ohith+x@googlemail.com",    "mohith@googlemail.com", "bfdd987598d9d82882fbc167c6414c5f44dce282564f75914a79c570e130ac98"),
    # Dots in the DOMAIN are never touched, and a gmail subdomain is not gmail.
    ("mohith@mail.gmail.com",       "mohith@mail.gmail.com", "2d685e17d008e0bf10e4de171c2a37d60ea74c3cb20eef1ebb61720ab89d5337"),
    # Non-gmail: dots and plus tags are part of the address and MUST survive.
    ("m.ohith@outlook.com",         "m.ohith@outlook.com",  "93671c0099d473a7f2913e50d941b5d0002f0fdb8c2484d133d0871b5021b30d"),
    ("mohith+ads@outlook.com",      "mohith+ads@outlook.com", "224d107342d4b9bd9ce0e92c1f22788726f55c33339b34a0c74c99157b7e819d"),
    # Rejections.
    (None,                          R.MISSING,              None),
    ("",                            R.EMPTY,                None),
    ("   ",                         R.EMPTY,                None),
    ("mohith",                      R.MALFORMED,            None),
    ("@example.com",                R.MALFORMED,            None),
    ("mohith@",                     R.MALFORMED,            None),
    ("mo@hith@example.com",         R.MALFORMED,            None),
    # Gmail address whose local part is nothing but a tag or dots.
    ("+ads@gmail.com",              R.MALFORMED,            None),
    ("...@gmail.com",               R.MALFORMED,            None),
]

# ---------------------------------------------------------------------------
# Phone. Section 5: E.164 via the record's country; unparseable is rejected.
# Rows carry a fourth element, the default region, because a national-format
# number has no meaning without one.
# ---------------------------------------------------------------------------
PHONE_CASES: list[tuple[str | None, str | None, str | RejectionReason, str | None]] = [
    # (input, default_region, expected_normalised, expected_digest)
    # Without country code: the region resolves it.
    ("415 555 2671",    "US", "+14155552671",  "cb6880e416769253645cb9c6b8989154bf66a56a77fc14c81fb1019663cbb928"),
    ("(415) 555-2671",  "US", "+14155552671",  "cb6880e416769253645cb9c6b8989154bf66a56a77fc14c81fb1019663cbb928"),
    ("415.555.2671",    "us", "+14155552671",  "cb6880e416769253645cb9c6b8989154bf66a56a77fc14c81fb1019663cbb928"),
    ("  415-555-2671 ", "US", "+14155552671",  "cb6880e416769253645cb9c6b8989154bf66a56a77fc14c81fb1019663cbb928"),
    # UK national format: trunk "0" is dropped in E.164.
    ("020 7946 0958",   "GB", "+442079460958", "f0bf0228144d9fe2bdf1da2d8ca698f17bf1410ee688b075c27062e47b6f0b6d"),
    ("098765 43210",    "IN", "+919876543210", "f3a47ce5ce3d4ca8ad15225a245b2759022f79489f5c62719b8c9490f7aab90e"),
    # With country code: region is irrelevant, even when wrong or absent.
    ("+14155552671",    "US", "+14155552671",  "cb6880e416769253645cb9c6b8989154bf66a56a77fc14c81fb1019663cbb928"),
    ("+1 415 555 2671", "GB", "+14155552671",  "cb6880e416769253645cb9c6b8989154bf66a56a77fc14c81fb1019663cbb928"),
    ("+14155552671",    None, "+14155552671",  "cb6880e416769253645cb9c6b8989154bf66a56a77fc14c81fb1019663cbb928"),
    ("+44 20 7946 0958", "US", "+442079460958", "f0bf0228144d9fe2bdf1da2d8ca698f17bf1410ee688b075c27062e47b6f0b6d"),
    # "00" international prefix as dialled from the UK is understood.
    ("0014155552671",   "GB", "+14155552671",  "cb6880e416769253645cb9c6b8989154bf66a56a77fc14c81fb1019663cbb928"),
    # Rejections.
    (None,              "US", R.MISSING,           None),
    ("",                "US", R.EMPTY,             None),
    ("   ",             "US", R.EMPTY,             None),
    ("hello",           "US", R.UNPARSEABLE,       None),
    # Parseable but not a valid number for the region.
    ("123",             "US", R.INVALID,           None),
    ("415 555",         "US", R.INVALID,           None),
    ("999 555 2671",    "US", R.INVALID,           None),  # 999 is not an assigned area code
    # National format with no region to resolve it against.
    ("415 555 2671",    None, R.NO_DEFAULT_REGION, None),
]

# ---------------------------------------------------------------------------
# Name. Section 5: trim, lowercase, strip accents and punctuation, drop titles
# and suffixes. Applies to both given and family name.
# ---------------------------------------------------------------------------
NAME_CASES: list[tuple[str | None, str | RejectionReason, str | None]] = [
    ("Smith",               "smith",      "6627835f988e2c5e50533d491163072d3f4f41f5c8b04630150debb3722ca2dd"),
    # Whitespace and case.
    ("  Smith  ",           "smith",      "6627835f988e2c5e50533d491163072d3f4f41f5c8b04630150debb3722ca2dd"),
    ("SMITH",               "smith",      "6627835f988e2c5e50533d491163072d3f4f41f5c8b04630150debb3722ca2dd"),
    # Accents fold to base letters.
    ("José",                "jose",       "1ec4ed037766aa181d8840ad04b9fc6e195fd37dedc04c98a5767a67d3758ece"),
    ("Müller",              "muller",     "a8b1fc83cb92ad90b731937cf76116ef6c1aba436f8d0d0ec673e8d34022aca8"),
    ("Nguyễn",              "nguyen",     "04db248bd13040d52df9ada78044e23e9826953c783597340bfad2577de9aa0e"),
    # Punctuation is removed, not replaced with a space.
    ("O'Brien",             "obrien",     "b4cb6cb33fe4b865868de825023a1e2790dc12ac01ecc8d7c5afe8254071c8ba"),
    ("Smith-Jones",         "smithjones", "6a77f47e3dec33252edbebb457d65e90501b643806c50cf8d6c3e2bd9cb74681"),
    ("St. John",            "stjohn",     "2012ca9043ebe0e4ad081f0033e5a72bc380249a621f1c5705fc50deebc51663"),
    # Titles and suffixes are dropped wherever they appear as whole tokens.
    ("Dr. Smith",           "smith",      "6627835f988e2c5e50533d491163072d3f4f41f5c8b04630150debb3722ca2dd"),
    ("Mr Smith",            "smith",      "6627835f988e2c5e50533d491163072d3f4f41f5c8b04630150debb3722ca2dd"),
    ("Smith Jr.",           "smith",      "6627835f988e2c5e50533d491163072d3f4f41f5c8b04630150debb3722ca2dd"),
    ("Smith, III",          "smith",      "6627835f988e2c5e50533d491163072d3f4f41f5c8b04630150debb3722ca2dd"),
    ("Prof. Dr. Smith PhD", "smith",      "6627835f988e2c5e50533d491163072d3f4f41f5c8b04630150debb3722ca2dd"),
    # Multi-part names collapse to one token.
    ("Mary Ann",            "maryann",    "16b3f07f0ec2f9dd127b71dc2e6c791b7d93116a1c3bc99d057eaa7a8a077e73"),
    ("van der Berg",        "vanderberg", "aad607ca3121d5d843c2cd64e8c73c2a24fa3a4cb890ed8d40a1efa8af7a14d5"),
    # A strip token embedded in a longer word is not touched...
    ("Drake",               "drake",      "54444aa7bc07ccec734fddb6f35b9918f914123ad2a0a6e5223d2cda95568de5"),
    # ...and a surname that happens to be a title-ish word is not in the list.
    ("King",                "king",       "e81dfe69841ad2f7b5790b63e998f0febaf3b29acd732881975130761b98e2c7"),
    # Rejections.
    (None,                  R.MISSING,    None),
    ("",                    R.EMPTY,      None),
    ("   ",                 R.EMPTY,      None),
    ("...",                 R.EMPTY,      None),
    # Nothing left once titles are removed.
    ("Dr.",                 R.EMPTY,      None),
    ("Mr. Jr.",             R.EMPTY,      None),
]

# ---------------------------------------------------------------------------
# Street. Section 5: normalised then hashed. Internal spaces are kept.
# ---------------------------------------------------------------------------
STREET_CASES: list[tuple[str | None, str | RejectionReason, str | None]] = [
    ("123 Main St",           "123 main st",       "c56a092e33fef672c4d8658e31ad4b17e8ceac569d5a88ca481846966d364fe5"),
    ("  123 Main St.  ",      "123 main st",       "c56a092e33fef672c4d8658e31ad4b17e8ceac569d5a88ca481846966d364fe5"),
    ("123   Main\tSt",        "123 main st",       "c56a092e33fef672c4d8658e31ad4b17e8ceac569d5a88ca481846966d364fe5"),
    ("123 Main St., Apt #4",  "123 main st apt 4", "2752fcfb4696cff5c693373fc40a90db5e21202e8e32d0dc034d3dec3408d415"),
    ("12 Rue de l'Église",    "12 rue de leglise", "d8bf8e99131169d7852a40e7e90801fe89355642b6d9dc8c5fd965293d7d40c3"),
    # Rejections.
    (None,                    R.MISSING,           None),
    ("",                      R.EMPTY,             None),
    ("   ",                   R.EMPTY,             None),
    ("...",                   R.EMPTY,             None),
]
# fmt: on


# ---------------------------------------------------------------------------
# One assertion pattern for every table. If the normaliser returns a string,
# its digest must match the row; if it returns a reason, there is no digest.
# ---------------------------------------------------------------------------
def _check(
    result: str | RejectionReason, expected: str | RejectionReason, digest: str | None
) -> None:
    assert result == expected
    if isinstance(result, RejectionReason):
        assert digest is None, "a rejected value must not have a digest in the table"
    else:
        assert digest is not None, "a normalised value must have a digest in the table"
        assert sha256_hex(result) == digest


@pytest.mark.parametrize(("raw", "expected", "digest"), EMAIL_CASES)
def test_email(raw: str | None, expected: str | RejectionReason, digest: str | None) -> None:
    _check(normalise_email(raw), expected, digest)


@pytest.mark.parametrize(("raw", "region", "expected", "digest"), PHONE_CASES)
def test_phone(
    raw: str | None, region: str | None, expected: str | RejectionReason, digest: str | None
) -> None:
    _check(normalise_phone(raw, region), expected, digest)


@pytest.mark.parametrize(("raw", "expected", "digest"), NAME_CASES)
def test_name(raw: str | None, expected: str | RejectionReason, digest: str | None) -> None:
    _check(normalise_name(raw), expected, digest)


@pytest.mark.parametrize(("raw", "expected", "digest"), STREET_CASES)
def test_street(raw: str | None, expected: str | RejectionReason, digest: str | None) -> None:
    _check(normalise_street(raw), expected, digest)


def test_tables_cover_every_rejection_reason_that_a_normaliser_can_return() -> None:
    # Guard: if a new reason is added to the enum, a row exercising it must be
    # added here too, or the table stops being the spec.
    seen = {
        row[-2]
        for table in (EMAIL_CASES, NAME_CASES, STREET_CASES)
        for row in table
        if isinstance(row[-2], RejectionReason)
    }
    seen |= {row[2] for row in PHONE_CASES if isinstance(row[2], RejectionReason)}
    # NOT_ALPHA2 and NO_MATCH_KEY belong to country and the record level,
    # covered in test_identifiers.py.
    field_level = set(RejectionReason) - {R.NOT_ALPHA2, R.NO_MATCH_KEY}
    assert seen == field_level
