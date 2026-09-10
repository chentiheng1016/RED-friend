"""Phase 1 掃描 PDF OCR 退路測試。

OCR 邏輯用 mock 在 _rasterize_pdf 與 _extract_image_vision 邊界，故不需真 macOS；
另有 skipUnless 的真 render 測試驗 pypdfium2 整條（裝了才跑）。主執行緒 patch、context
manager 自動還原，符合 unittest（非 pytest）隔離慣例。
"""
from __future__ import annotations

import io
import unittest
from unittest import mock

from agent_core.ingest import drive_sync

_PDF_MIME = "application/pdf"


def _has_pypdfium2() -> bool:
    try:
        import pypdfium2  # noqa: F401
        from reportlab.pdfgen import canvas  # noqa: F401
        return True
    except Exception:
        return False


def _make_pdf(pages_text: list[str]) -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for t in pages_text:
        c.drawString(100, 700, t)
        c.showPage()
    c.save()
    return buf.getvalue()


class ExtractPdfOcrTests(unittest.TestCase):
    def test_disabled_returns_empty_without_rasterizing(self):
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", False), \
             mock.patch.object(drive_sync, "_rasterize_pdf") as rast:
            self.assertEqual(drive_sync._extract_pdf_ocr(b"%PDF"), "")
            rast.assert_not_called()

    def test_enabled_joins_page_ocr(self):
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True), \
             mock.patch.object(drive_sync, "_rasterize_pdf", return_value=[b"p1", b"p2"]), \
             mock.patch.object(drive_sync, "_extract_image_vision",
                               side_effect=["第一頁文字", "第二頁文字"]):
            out = drive_sync._extract_pdf_ocr(b"%PDF")
        self.assertEqual(out, "第一頁文字\n\n第二頁文字")

    def test_all_pages_blank_returns_empty(self):
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True), \
             mock.patch.object(drive_sync, "_rasterize_pdf", return_value=[b"p1", b"p2"]), \
             mock.patch.object(drive_sync, "_extract_image_vision", return_value=None):
            self.assertEqual(drive_sync._extract_pdf_ocr(b"%PDF"), "")

    def test_no_pages_returns_empty(self):
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True), \
             mock.patch.object(drive_sync, "_rasterize_pdf", return_value=[]), \
             mock.patch.object(drive_sync, "_extract_image_vision") as vis:
            self.assertEqual(drive_sync._extract_pdf_ocr(b"%PDF"), "")
            vis.assert_not_called()

    def test_char_budget_stops_early(self):
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True), \
             mock.patch.object(drive_sync, "_PDF_OCR_MAX_CHARS", 10), \
             mock.patch.object(drive_sync, "_rasterize_pdf", return_value=[b"p"] * 10), \
             mock.patch.object(drive_sync, "_extract_image_vision",
                               return_value="ABCDEFGH") as vis:
            out = drive_sync._extract_pdf_ocr(b"%PDF")
        # page0: 8 < 10 續；page1: 累計 16 >= 10 → 停。只應呼叫 2 次
        self.assertEqual(vis.call_count, 2)
        self.assertEqual(out, "ABCDEFGH\n\nABCDEFGH")


class BinaryTextPdfDispatchTests(unittest.TestCase):
    def test_textlayer_pdf_skips_ocr(self):
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True), \
             mock.patch.object(drive_sync, "_extract_pdf", return_value="real text"), \
             mock.patch.object(drive_sync, "_extract_pdf_ocr") as ocr:
            out = drive_sync._extract_binary_text(_PDF_MIME, b"%PDF")
        self.assertEqual(out, "real text")
        ocr.assert_not_called()

    def test_empty_pdf_flag_off_no_ocr(self):
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", False), \
             mock.patch.object(drive_sync, "_extract_pdf", return_value="   "), \
             mock.patch.object(drive_sync, "_extract_pdf_ocr") as ocr:
            out = drive_sync._extract_binary_text(_PDF_MIME, b"%PDF")
        self.assertEqual(out, "   ")
        ocr.assert_not_called()

    def test_empty_pdf_flag_on_triggers_ocr(self):
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True), \
             mock.patch.object(drive_sync, "_extract_pdf", return_value=""), \
             mock.patch.object(drive_sync, "_extract_pdf_ocr", return_value="ocr text") as ocr:
            out = drive_sync._extract_binary_text(_PDF_MIME, b"%PDF")
        self.assertEqual(out, "ocr text")
        ocr.assert_called_once()


class MarkerIsPdfTests(unittest.TestCase):
    def test_by_mime(self):
        self.assertTrue(drive_sync._marker_is_pdf({"mime_type": _PDF_MIME}))

    def test_by_title_suffix_case_insensitive(self):
        self.assertTrue(drive_sync._marker_is_pdf({"title": "合約.PDF"}))
        self.assertTrue(drive_sync._marker_is_pdf({"title": "x.pdf"}))

    def test_non_pdf(self):
        self.assertFalse(drive_sync._marker_is_pdf({"title": "photo.jpg",
                                                    "mime_type": "image/png"}))
        self.assertFalse(drive_sync._marker_is_pdf({}))

    def test_empty_text_marker_shape_binds_to_recorder(self):
        # 防回歸：empty_text skip_marker 的鍵必須與 _record_skip_marker 簽名相容
        # （mime_type 要走 extra，不能當頂層 kwarg；曾因此 TypeError 炸 _commit_file）。
        import inspect
        marker = {
            "reason": "empty_text", "modified_time": "t",
            "folder_id": "f", "drive_id": "d", "title": "合約.pdf",
            "extra": {"mime_type": "application/pdf"},
        }
        inspect.signature(drive_sync._record_skip_marker).bind("fid", **marker)
        # _record_skip_marker 會把 extra flatten 進 marker 頂層 → _marker_is_pdf 讀得到
        flat = {"reason": "empty_text", "title": "合約.pdf", **marker["extra"]}
        self.assertTrue(drive_sync._marker_is_pdf(flat))


class PdfOcrCapableTests(unittest.TestCase):
    """_pdf_ocr_capable：環境探測 + process 內快取。"""

    def setUp(self):
        self._orig_cache = drive_sync._PDF_OCR_CAPABLE_CACHE
        drive_sync._PDF_OCR_CAPABLE_CACHE = None

    def tearDown(self):
        drive_sync._PDF_OCR_CAPABLE_CACHE = self._orig_cache

    def test_vision_disabled_means_incapable(self):
        # RAG_VISION_OCR=0 → OCR 鏈死路（_extract_image_vision 直接回 None）
        with mock.patch.object(drive_sync, "_VISION_OCR_ENABLED", False):
            self.assertFalse(drive_sync._pdf_ocr_capable())

    def test_result_cached_per_process(self):
        with mock.patch.object(drive_sync, "_VISION_OCR_ENABLED", False):
            self.assertFalse(drive_sync._pdf_ocr_capable())
        # 探測結果一次定案，旗標翻正也不重探
        self.assertFalse(drive_sync._pdf_ocr_capable())


class SkipMarkerOcrGateTests(unittest.TestCase):
    MT = "2026-01-01T00:00:00Z"

    def setUp(self):
        # 預設環境可跑 OCR：探測結果不隨 CI/本機有無 ocrmac 漂移
        patcher = mock.patch.object(
            drive_sync, "_pdf_ocr_capable", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _marker(self, **kw):
        m = {"reason": "empty_text", "modified_time": self.MT, "drive_id": "d1"}
        m.update(kw)
        return m

    def test_flag_on_pdf_marker_reattempts(self):
        # 同 modified_time 本應 True(續跳)；開 flag + PDF → False(重試 OCR)
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True):
            self.assertFalse(drive_sync._skip_marker_matches(
                self._marker(mime_type=_PDF_MIME), self.MT, "", "d1"))

    def test_flag_on_old_pdf_marker_by_title(self):
        # 舊 marker 沒 mime_type，靠 title .pdf 判
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True):
            self.assertFalse(drive_sync._skip_marker_matches(
                self._marker(title="財報.pdf"), self.MT, "", "d1"))

    def test_flag_on_nonpdf_marker_still_skips(self):
        # 非 PDF empty_text：開 flag 也不重抽，沿用 modified_time → True
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True):
            self.assertTrue(drive_sync._skip_marker_matches(
                self._marker(mime_type="image/png", title="x.png"), self.MT, "", "d1"))

    def test_flag_off_pdf_marker_unchanged(self):
        # 預設關：PDF empty_text 仍沿用 modified_time → True(行為同今日)
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", False):
            self.assertTrue(drive_sync._skip_marker_matches(
                self._marker(mime_type=_PDF_MIME), self.MT, "", "d1"))

    def test_changed_modified_time_reattempts_regardless(self):
        # modified_time 變了一律重試（既有行為，不受 flag 影響）
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", False):
            self.assertFalse(drive_sync._skip_marker_matches(
                self._marker(mime_type=_PDF_MIME), "2026-09-09T00:00:00Z", "", "d1"))

    def test_flag_on_ocr_attempted_pdf_stays_skipped(self):
        # OCR 跑過仍空（marker 帶 ocr_attempted）：同 modified_time 續跳，
        # 不再每晚重下載重 OCR（無限循環回歸防護）。
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True):
            self.assertTrue(drive_sync._skip_marker_matches(
                self._marker(mime_type=_PDF_MIME, ocr_attempted=True),
                self.MT, "", "d1"))

    def test_flag_on_ocr_attempted_pdf_by_title_stays_skipped(self):
        # 靠 title .pdf 判的 marker 帶 ocr_attempted 也一樣續跳
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True):
            self.assertTrue(drive_sync._skip_marker_matches(
                self._marker(title="印章掃描.pdf", ocr_attempted=True),
                self.MT, "", "d1"))

    def test_flag_on_ocr_attempted_changed_mtime_reattempts(self):
        # 檔案真的變了（modifiedTime 不同）→ 即使 ocr_attempted 也重試
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True):
            self.assertFalse(drive_sync._skip_marker_matches(
                self._marker(mime_type=_PDF_MIME, ocr_attempted=True),
                "2026-09-09T00:00:00Z", "", "d1"))

    def test_flag_on_env_incapable_stays_skipped(self):
        # flag 開但環境跑不動 OCR（Cloud Run / 缺 pypdfium2/ocrmac）：
        # 不放行——重下載也抽不出字，重試機會留給修好後的環境
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True), \
                mock.patch.object(drive_sync, "_pdf_ocr_capable",
                                  return_value=False):
            self.assertTrue(drive_sync._skip_marker_matches(
                self._marker(mime_type=_PDF_MIME), self.MT, "", "d1"))


class EmptyTextMarkerOcrStampTests(unittest.TestCase):
    """_prepare_file 落入 empty_text 時，OCR 開著的 PDF 要在 marker 蓋
    ocr_attempted 戳記——與 SkipMarkerOcrGateTests 的放行端合起來，
    才封得住「OCR 過仍空 → 隔晚又重 OCR」的循環。"""

    LISTING = {
        "id": "fid",
        "name": "印章掃描.pdf",
        "mimeType": _PDF_MIME,
        "modifiedTime": "2026-01-01T00:00:00Z",
        "size": "1234",
    }
    EXISTING = {
        "synced_at": "", "content_hash": "", "title": "",
        "folder_id": "", "drive_id": "", "sync_complete": True,
    }

    def _prepare_empty_pdf_plan(
        self, *, ocr_enabled: bool, ocr_capable: bool = True,
    ) -> "drive_sync._FilePlan":
        store = mock.MagicMock()
        store.find_duplicate_doc_id.return_value = None
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", ocr_enabled), \
                mock.patch.object(drive_sync, "_pdf_ocr_capable",
                                  return_value=ocr_capable), \
                mock.patch.object(drive_sync, "_get_matching_skip_marker",
                                  return_value=None), \
                mock.patch.object(drive_sync, "_is_meeting_folder",
                                  return_value=False), \
                mock.patch.object(drive_sync, "get_embedding_hard_quota_message",
                                  return_value=None), \
                mock.patch.object(drive_sync, "_get_image_hard_quota_message",
                                  return_value=None), \
                mock.patch.object(drive_sync, "_export_file_text_for_sync",
                                  return_value=""):
            return drive_sync._prepare_file(
                "fid", folder_id="F", drive_id="D",
                modified_time="2026-01-01T00:00:00Z",
                prefetched_metadata=dict(self.EXISTING),
                store=store, service=mock.MagicMock(),
                listing_meta=dict(self.LISTING),
            )

    def test_ocr_on_empty_pdf_marker_carries_ocr_attempted(self):
        plan = self._prepare_empty_pdf_plan(ocr_enabled=True)
        self.assertEqual(plan.result["reason"], "empty_text")
        self.assertTrue(plan.skip_marker["extra"]["ocr_attempted"])
        # extra flatten 進 marker 後，放行端要認得而續跳（端到端閉環）
        flat = {
            "reason": "empty_text",
            "modified_time": plan.skip_marker["modified_time"],
            "folder_id": "F", "drive_id": "D",
            "title": plan.skip_marker["title"],
            **plan.skip_marker["extra"],
        }
        with mock.patch.object(drive_sync, "_PDF_OCR_ENABLED", True):
            self.assertTrue(drive_sync._skip_marker_matches(
                flat, "2026-01-01T00:00:00Z", "F", "D"))

    def test_ocr_off_empty_pdf_marker_has_no_stamp(self):
        # flag 關著（OCR 根本沒跑）不可蓋戳記，日後開 OCR 才會重試這批舊 marker
        plan = self._prepare_empty_pdf_plan(ocr_enabled=False)
        self.assertEqual(plan.result["reason"], "empty_text")
        self.assertNotIn("ocr_attempted", plan.skip_marker["extra"])

    def test_ocr_on_env_incapable_has_no_stamp(self):
        # flag 開但環境跑不動 OCR（沒真的試過）：不蓋戳記，環境修好後仍會重試
        plan = self._prepare_empty_pdf_plan(ocr_enabled=True, ocr_capable=False)
        self.assertEqual(plan.result["reason"], "empty_text")
        self.assertNotIn("ocr_attempted", plan.skip_marker["extra"])


@unittest.skipUnless(_has_pypdfium2(), "pypdfium2 / reportlab not installed")
class RasterizePdfRealTests(unittest.TestCase):
    def test_returns_png_pages(self):
        pages = drive_sync._rasterize_pdf(_make_pdf(["hello 測試"]), max_pages=15, dpi=120)
        self.assertEqual(len(pages), 1)
        self.assertTrue(pages[0].startswith(b"\x89PNG"))

    def test_page_cap_truncates(self):
        data = _make_pdf(["p0", "p1", "p2"])
        self.assertEqual(len(drive_sync._rasterize_pdf(data, max_pages=2, dpi=72)), 2)

    def test_bad_bytes_returns_empty(self):
        self.assertEqual(drive_sync._rasterize_pdf(b"not a pdf at all", 15, 200), [])


if __name__ == "__main__":
    unittest.main()
