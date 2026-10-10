"""Tests for backend.channels.pairing — EVN-XXXXXXXX pairing codes.

Codes are uppercase alphanumeric with a ``EVN-`` prefix, e.g. ``EVN-AB12CD34``.
The detector keys off that prefix, so ordinary chat ("Hi khodam", "thanks")
is never mistaken for a pairing attempt.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.channels.pairing import (
    generate_pair_code,
    format_pair_code,
    validate_pair_code,
    extract_pair_code,
)


class TestGeneratePairCode(unittest.TestCase):
    """Tests for generate_pair_code()."""

    # Unambiguous charset: no 0, O, 1, I, L
    VALID_CHARS = set("ABCDEFGHJKMNPQRSTUVWXYZ23456789")
    AMBIGUOUS_CHARS = set("01OIL")

    def test_has_evn_prefix(self):
        for _ in range(100):
            self.assertTrue(generate_pair_code().startswith("EVN-"))

    def test_total_length(self):
        self.assertEqual(len(generate_pair_code()), 12)  # 'EVN-' + 8 payload chars

    def test_payload_is_uppercase_alphanumeric(self):
        for _ in range(200):
            payload = generate_pair_code()[4:]
            self.assertEqual(len(payload), 8)
            self.assertRegex(payload, r"^[A-Z0-9]{8}$")

    def test_contains_only_unambiguous_chars(self):
        for _ in range(200):
            for ch in generate_pair_code()[4:]:
                self.assertIn(ch, self.VALID_CHARS)

    def test_never_contains_ambiguous_chars(self):
        for _ in range(200):
            for ch in generate_pair_code()[4:]:
                self.assertNotIn(ch, self.AMBIGUOUS_CHARS)

    def test_no_two_codes_are_identical_in_500_runs(self):
        codes = {generate_pair_code() for _ in range(500)}
        self.assertEqual(len(codes), 500)


class TestFormatPairCode(unittest.TestCase):
    """Tests for format_pair_code() — canonical uppercase EVN-XXXXXXXX form."""

    def test_returns_canonical_form(self):
        self.assertEqual(format_pair_code("EVN-AB12CD34"), "EVN-AB12CD34")

    def test_uppercases_input(self):
        self.assertEqual(format_pair_code("evn-ab12cd34"), "EVN-AB12CD34")

    def test_output_validates(self):
        raw = generate_pair_code()
        self.assertTrue(validate_pair_code(format_pair_code(raw)))


class TestValidatePairCode(unittest.TestCase):
    """Tests for validate_pair_code()."""

    def test_accepts_new_and_legacy_codes(self):
        self.assertTrue(validate_pair_code("EVN-AB12CD34"))
        self.assertTrue(validate_pair_code("EVN-99999999"))
        self.assertTrue(validate_pair_code("EVN-A1B2C3D4"))
        self.assertTrue(validate_pair_code("EVN-AB12"))

    def test_rejects_missing_prefix(self):
        self.assertFalse(validate_pair_code("AB12"))
        self.assertFalse(validate_pair_code("ABC123"))

    def test_rejects_missing_hyphen(self):
        self.assertFalse(validate_pair_code("EVNAB12"))

    def test_rejects_lowercase(self):
        self.assertFalse(validate_pair_code("evn-ab12"))

    def test_rejects_wrong_length(self):
        self.assertFalse(validate_pair_code("EVN-AB12CD3"))
        self.assertFalse(validate_pair_code("EVN-AB12CD345"))

    def test_rejects_special_characters(self):
        self.assertFalse(validate_pair_code("EVN-AB12_CD3"))
        self.assertFalse(validate_pair_code("EVN-AB12$CD3"))

    def test_rejects_empty_and_none(self):
        self.assertFalse(validate_pair_code(""))
        self.assertFalse(validate_pair_code(None))  # type: ignore


class TestExtractPairCode(unittest.TestCase):
    """Tests for extract_pair_code() strictness."""

    def test_returns_none_for_ordinary_chat(self):
        for text in (
            "Hi khodam",
            "thanks",
            "mantap bos",
            "hello there",
            "assalamualaikum",
            "KHODAM",
            "THANKS",
        ):
            self.assertIsNone(extract_pair_code(text), msg=text)

    def test_returns_none_for_words_starting_with_evn(self):
        self.assertIsNone(extract_pair_code("EVNEWS"))  # letters only, no hyphen
        self.assertIsNone(extract_pair_code("EVNAB12CD34"))  # hyphen required
        self.assertIsNone(extract_pair_code("SEVN-AB12CD34"))  # no boundary before prefix

    def test_extracts_bare_code(self):
        self.assertEqual(extract_pair_code("EVN-AB12CD34"), "EVN-AB12CD34")

    def test_extracts_embedded_code(self):
        self.assertEqual(
            extract_pair_code("here is my code EVN-AB12CD34 ok"), "EVN-AB12CD34"
        )

    def test_is_case_insensitive_and_normalises(self):
        self.assertEqual(extract_pair_code("evn-ab12cd34"), "EVN-AB12CD34")

    def test_extracts_legacy_code_for_existing_approvals(self):
        self.assertEqual(extract_pair_code("evn-ab12"), "EVN-AB12")

    def test_returns_none_for_empty_or_none(self):
        self.assertIsNone(extract_pair_code(""))
        self.assertIsNone(extract_pair_code(None))  # type: ignore

    def test_roundtrip(self):
        for _ in range(100):
            code = generate_pair_code()
            self.assertEqual(extract_pair_code(code), code)
            self.assertEqual(extract_pair_code("code: " + code + " please"), code)


if __name__ == "__main__":
    unittest.main()
