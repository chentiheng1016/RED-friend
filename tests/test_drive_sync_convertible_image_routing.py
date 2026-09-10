"""bmp/tiff/x-icon must route through PIL-conversion before Gemini OCR.

Gemini Vision rejects image/bmp, image/tiff and image/x-icon outright
("400 INVALID_ARGUMENT: Unsupported MIME type"). They used to sit in
_IMAGE_MIMES (sent to Gemini as-is) so every sync 400'd on ~250 such files.
Moving them to _CONVERTIBLE_IMAGE_MIMES routes them through
_extract_convertible_image (PIL → JPEG) — run inside the _ExtractPool worker so
the conversion AND the Gemini call stay SIGSEGV-isolated, same as the image op.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class ConvertibleImageMimeCategorisationTests(unittest.TestCase):
    def test_gemini_unsupported_formats_are_convertible_not_direct(self):
        from agent_core.ingest import drive_sync
        for m in ("image/bmp", "image/tiff", "image/x-tiff", "image/x-icon"):
            self.assertIn(m, drive_sync._CONVERTIBLE_IMAGE_MIMES, m)
            self.assertNotIn(m, drive_sync._IMAGE_MIMES, m)

    def test_gemini_native_formats_stay_direct(self):
        from agent_core.ingest import drive_sync
        for m in ("image/png", "image/jpeg", "image/webp", "image/heic"):
            self.assertIn(m, drive_sync._IMAGE_MIMES, m)


class ExtractDispatchTests(unittest.TestCase):
    def test_convertible_image_op_dispatches_to_converter(self):
        from agent_core.ingest import drive_sync
        with mock.patch.object(drive_sync, "_extract_convertible_image",
                               return_value="CONVERTED OCR") as cv:
            out = drive_sync._extract_dispatch("convertible_image", "image/tiff", b"data")
        self.assertEqual(out, "CONVERTED OCR")
        cv.assert_called_once_with(b"data", "image/tiff")

    def test_image_op_still_dispatches_to_extract_image(self):
        from agent_core.ingest import drive_sync
        with mock.patch.object(drive_sync, "_extract_image",
                               return_value="IMG OCR") as im:
            out = drive_sync._extract_dispatch("image", "image/png", b"data")
        self.assertEqual(out, "IMG OCR")
        im.assert_called_once_with(b"data", "image/png")


class ExportRoutesConvertibleThroughPoolTests(unittest.TestCase):
    """The whole point: convertible images go through the pool with the
    convertible_image op (isolated convert+OCR), not the direct image op."""

    def _run_export(self, mime):
        from agent_core.ingest import drive_sync
        captured = {}

        def fake_run(op, mime_type, data, label, timeout):
            captured["op"] = op
            captured["mime"] = mime_type
            return "OCR"

        with mock.patch.object(drive_sync, "_DRIVE_ENABLE_IMAGE_INGEST", True), \
             mock.patch.object(drive_sync, "_execute_drive_request", return_value=b"\x00imgbytes"), \
             mock.patch.object(drive_sync._EXTRACT_POOL, "run", side_effect=fake_run):
            out = drive_sync._export_file_text_for_sync(mock.MagicMock(), "fid", mime)
        return out, captured

    def test_tiff_routes_convertible_op(self):
        out, cap = self._run_export("image/tiff")
        self.assertEqual(out, "OCR")
        self.assertEqual(cap["op"], "convertible_image")
        self.assertEqual(cap["mime"], "image/tiff")

    def test_bmp_routes_convertible_op(self):
        _, cap = self._run_export("image/bmp")
        self.assertEqual(cap["op"], "convertible_image")

    def test_png_still_routes_image_op(self):
        _, cap = self._run_export("image/png")
        self.assertEqual(cap["op"], "image")


if __name__ == "__main__":
    unittest.main()
