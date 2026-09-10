"""Tests for _prepare_image_for_gemini: tiff/bmp → JPEG, native passthrough.

Gemini rejects bmp/tiff/x-icon/psd with 400 "Unsupported MIME type", so the
local image tools (analyze_image / spec-parse / QC) must PIL-convert them to
JPEG before sending. Native formats must pass through byte-for-byte.
"""
import io
import unittest

from PIL import Image

from agent_core.gemini_client import _prepare_image_for_gemini


def _encode(fmt: str, mode: str = "RGB", size=(8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (123, 50, 200) if mode == "RGB" else 128).save(buf, format=fmt)
    return buf.getvalue()


class PrepareImageForGeminiTest(unittest.TestCase):
    def test_tiff_converts_to_jpeg(self):
        data, mime = _prepare_image_for_gemini(_encode("TIFF"), "image/tiff")
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(Image.open(io.BytesIO(data)).format, "JPEG")

    def test_bmp_converts_to_jpeg(self):
        data, mime = _prepare_image_for_gemini(_encode("BMP"), "image/bmp")
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(Image.open(io.BytesIO(data)).format, "JPEG")

    def test_x_tiff_variant_converts(self):
        _, mime = _prepare_image_for_gemini(_encode("TIFF"), "image/x-tiff")
        self.assertEqual(mime, "image/jpeg")

    def test_native_png_passes_through_untouched(self):
        raw = _encode("PNG")
        data, mime = _prepare_image_for_gemini(raw, "image/png")
        self.assertEqual(mime, "image/png")
        self.assertIs(data, raw)

    def test_native_jpeg_passes_through_untouched(self):
        raw = _encode("JPEG")
        data, mime = _prepare_image_for_gemini(raw, "image/jpeg")
        self.assertEqual(mime, "image/jpeg")
        self.assertIs(data, raw)

    def test_rgba_tiff_flattened_without_alpha_error(self):
        # RGBA tiff would crash JPEG save without the alpha-flatten branch.
        data, mime = _prepare_image_for_gemini(_encode("TIFF", mode="RGBA"), "image/tiff")
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(Image.open(io.BytesIO(data)).mode, "RGB")

    def test_undecodable_bytes_returns_input_unchanged(self):
        # Best-effort: garbage that PIL can't open falls back to the original.
        data, mime = _prepare_image_for_gemini(b"not an image", "image/bmp")
        self.assertEqual(data, b"not an image")
        self.assertEqual(mime, "image/bmp")


if __name__ == "__main__":
    unittest.main()
