"""Unit tests for small pure helpers.

These functions are important glue (string sanitization, JSON fallback
parsing, audio-int16 conversion, mode-switch detection) but weren't
covered by the existing integration-style test suites. A regression in
any of them would break larger flows quietly.
"""
import os
import sys
import unittest


# Ensure repo root is on sys.path so `import agent_core.X` resolves when
# the test is run from an arbitrary cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_core.email_classify import _parse_classify_json
from agent_core.email_utils import _extract_email_addr
from agent_core.file_ops import _sanitize_filename
from agent_core.quote import _parse_quote_json
from skills.taiwan_public_2 import tw_postal_code


class SanitizeFilenameTests(unittest.TestCase):
    def test_replaces_illegal_chars(self):
        self.assertEqual(_sanitize_filename('a/b*c?d"e'), "a_b_c_d_e")

    def test_caps_length(self):
        self.assertEqual(_sanitize_filename("a" * 200, max_len=10), "a" * 10)

    def test_strips_surrounding_whitespace_always(self):
        self.assertEqual(_sanitize_filename("  hello  "), "hello")

    def test_strip_whitespace_true_kills_internal_spaces(self):
        self.assertEqual(
            _sanitize_filename("acme corp A-1", strip_whitespace=True),
            "acme_corp_A-1",
        )

    def test_strip_whitespace_false_preserves_internal_spaces(self):
        self.assertEqual(_sanitize_filename("acme corp"), "acme corp")

    def test_empty_and_none_safe(self):
        self.assertEqual(_sanitize_filename(""), "")
        self.assertEqual(_sanitize_filename(None), "")


class ExtractEmailAddrTests(unittest.TestCase):
    def test_extracts_from_name_addr_form(self):
        self.assertEqual(
            _extract_email_addr("Alice Smith <alice@example.com>"),
            "alice@example.com",
        )

    def test_passes_through_bare_address(self):
        self.assertEqual(
            _extract_email_addr("  Bob@Example.com  "),
            "bob@example.com",
        )

    def test_empty_input_returns_empty(self):
        self.assertEqual(_extract_email_addr(""), "")
        self.assertEqual(_extract_email_addr(None), "")


class ParseClassifyJsonTests(unittest.TestCase):
    def test_valid_json_parses(self):
        out = _parse_classify_json('{"category": "詢價", "urgency": "R", "reason": "急"}')
        self.assertEqual(out["category"], "詢價")
        self.assertEqual(out["urgency"], "R")
        self.assertEqual(out["reason"], "急")

    def test_unknown_category_falls_back_to_一般(self):
        out = _parse_classify_json('{"category": "外星人訊息", "urgency": "R"}')
        self.assertEqual(out["category"], "一般")

    def test_invalid_urgency_falls_back_to_G(self):
        out = _parse_classify_json('{"category": "一般", "urgency": "X"}')
        self.assertEqual(out["urgency"], "G")

    def test_garbage_returns_default(self):
        out = _parse_classify_json("not json at all")
        self.assertEqual(out["category"], "一般")
        self.assertEqual(out["urgency"], "G")

    def test_extracts_json_from_surrounding_prose(self):
        # Gemini sometimes wraps answers in preface; parser should still find the object
        wrapped = '好的，分類結果：{"category": "客戶訂單", "urgency": "Y", "reason": "非急件"} 以上。'
        out = _parse_classify_json(wrapped)
        self.assertEqual(out["category"], "客戶訂單")
        self.assertEqual(out["urgency"], "Y")


class ParseQuoteJsonTests(unittest.TestCase):
    def test_valid_json_with_items(self):
        raw = '{"direction": "out", "customer": "ACME", "items": [{"sku": "SKU-A1", "unit_price": 12.5}]}'
        out = _parse_quote_json(raw)
        self.assertEqual(out["direction"], "out")
        self.assertEqual(len(out["items"]), 1)
        self.assertEqual(out["items"][0]["sku"], "SKU-A1")

    def test_invalid_returns_default_shape(self):
        out = _parse_quote_json("bogus")
        self.assertEqual(out, {"items": [], "direction": "unknown"})

    def test_empty_string_safe(self):
        out = _parse_quote_json("")
        self.assertEqual(out["items"], [])

    def test_extracts_from_markdown_wrapped(self):
        wrapped = '```json\n{"direction": "in", "items": []}\n```'
        out = _parse_quote_json(wrapped)
        self.assertEqual(out["direction"], "in")


class TaiwanPostalCodeTests(unittest.TestCase):
    def test_city_qualified_duplicate_districts_do_not_collide(self):
        self.assertIn("401", tw_postal_code("台中市東區自由路"))
        self.assertIn("701", tw_postal_code("台南市東區大學路"))

    def test_ambiguous_duplicate_district_asks_for_city(self):
        out = tw_postal_code("東區自由路")
        self.assertIn("重名行政區", out)
        self.assertIn("臺中市東區", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
