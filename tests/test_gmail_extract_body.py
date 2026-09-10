"""Regression (review finding #9): extract_body walked the MIME tree with a
LIFO stack (reverse document order) and didn't skip parts with a filename. So
a message whose real body precedes an inline text/plain attachment — or whose
attachment simply sorted later — could return the ATTACHMENT's text. That
garbage then fed read_gmail, the classifiers, and quote extraction. Pin
document-order, attachment-skipping traversal.
"""
from __future__ import annotations

import base64
import os
import sys
import unittest


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode()


def _part(mime, *, text=None, filename=None, parts=None):
    p = {"mimeType": mime}
    if text is not None:
        p["body"] = {"data": _b64(text)}
    if filename:
        p["filename"] = filename
    if parts:
        p["parts"] = parts
    return p


class ExtractBodyTests(unittest.TestCase):
    def setUp(self):
        from agent_core import gmail_ops

        self.extract = gmail_ops.extract_body

    def test_body_before_inline_text_attachment(self):
        payload = _part("multipart/mixed", parts=[
            _part("text/plain", text="REAL BODY"),
            _part("text/plain", text="ATTACHMENT CONTENT", filename="log.txt"),
        ])
        self.assertEqual(self.extract(payload), "REAL BODY")

    def test_inline_text_attachment_before_body_is_skipped(self):
        payload = _part("multipart/mixed", parts=[
            _part("text/plain", text="ATTACHMENT CONTENT", filename="log.txt"),
            _part("multipart/alternative", parts=[
                _part("text/plain", text="REAL BODY"),
                _part("text/html", text="<p>REAL BODY</p>"),
            ]),
        ])
        self.assertEqual(self.extract(payload), "REAL BODY")

    def test_alternative_prefers_plain(self):
        payload = _part("multipart/alternative", parts=[
            _part("text/plain", text="PLAIN BODY"),
            _part("text/html", text="<p>HTML BODY</p>"),
        ])
        self.assertEqual(self.extract(payload), "PLAIN BODY")

    def test_html_only_is_stripped(self):
        payload = _part("multipart/alternative", parts=[
            _part("text/html", text="<p>Hello<br>World</p>"),
        ])
        out = self.extract(payload)
        self.assertIn("Hello", out)
        self.assertIn("World", out)
        self.assertNotIn("<p>", out)

    def test_plain_single_part(self):
        self.assertEqual(self.extract(_part("text/plain", text="Just text")), "Just text")

    def test_no_text_body_returns_placeholder(self):
        payload = _part("multipart/mixed", parts=[
            _part("application/pdf", filename="invoice.pdf"),
        ])
        self.assertEqual(self.extract(payload), "（無內文）")

    def test_single_part_text_with_filename_falls_back_to_its_text(self):
        # A message whose ONLY text is a filename-tagged text/plain part
        # (forwarded-as-attachment / some bounce shapes). We must not return
        # the placeholder and feed "（無內文）" downstream — the attachment text
        # is a last-resort body.
        payload = _part("text/plain", text="THE ONLY BODY", filename="message.txt")
        self.assertEqual(self.extract(payload), "THE ONLY BODY")

    def test_real_body_still_wins_over_filename_text_fallback(self):
        # When a genuine non-attachment body exists, the filename-tagged text
        # must NOT be returned even though it is encountered first.
        payload = _part("multipart/mixed", parts=[
            _part("text/plain", text="ATTACHMENT TEXT", filename="log.txt"),
            _part("text/plain", text="REAL BODY"),
        ])
        self.assertEqual(self.extract(payload), "REAL BODY")


if __name__ == "__main__":
    unittest.main()
