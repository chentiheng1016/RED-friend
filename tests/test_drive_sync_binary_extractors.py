"""Tests for PDF / xlsx / xlsm / docx extraction in drive_sync.

xlsx and docx are exercised end-to-end by round-tripping real bytes through
the libraries (openpyxl, python-docx). PDF uses a pypdf mock because crafting
a valid PDF with extractable text in pure pypdf isn't simple.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import types
import unittest
import zipfile
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── helpers ─────────────────────────────────────────────────────────

def _make_xlsx_bytes(rows_per_sheet: dict[str, list[list]]) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in rows_per_sheet.items():
        ws = wb.create_sheet(title=title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_docx_bytes(paragraphs: list[str], tables: list[list[list[str]]] | None = None) -> bytes:
    from docx import Document
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    for tbl in tables or []:
        rows = len(tbl)
        cols = len(tbl[0]) if rows else 0
        t = doc.add_table(rows=rows, cols=cols)
        for r, row in enumerate(tbl):
            for c, cell_text in enumerate(row):
                t.cell(r, c).text = cell_text
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_pptx_bytes(slides: list[dict]) -> bytes:
    """Build a .pptx from slide specs.

    Each spec is a dict with optional keys: 'title'/'body' (str), 'table'
    (list[list[str]]), 'notes' (str). Uses the fully blank layout so an empty
    spec yields a slide with no extractable text (exercises the empty-slide drop).
    """
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    blank = prs.slide_layouts[6]
    for spec in slides:
        slide = prs.slides.add_slide(blank)
        top = 0.5
        for key in ("title", "body"):
            text = spec.get(key)
            if text:
                tb = slide.shapes.add_textbox(Inches(0.5), Inches(top), Inches(9), Inches(1))
                tb.text_frame.text = text
                top += 1.2
        table = spec.get("table")
        if table:
            rows = len(table)
            cols = len(table[0]) if rows else 0
            gtable = slide.shapes.add_table(
                rows, cols, Inches(0.5), Inches(top), Inches(9), Inches(2)
            ).table
            for r, row in enumerate(table):
                for c, val in enumerate(row):
                    gtable.cell(r, c).text = val
        notes = spec.get("notes")
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _make_zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text.encode("utf-8"))
    return buf.getvalue()


def _add_zip_member(data: bytes, name: str, payload: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with src, zipfile.ZipFile(buf, "w") as archive:
        for info in src.infolist():
            archive.writestr(info, src.read(info.filename))
        archive.writestr(name, payload)
    return buf.getvalue()


def _service_with_media(payload: bytes):
    """Build a fake Drive service whose get_media().execute() returns payload."""
    media_req = mock.MagicMock()
    media_req.execute.return_value = payload
    files_obj = mock.MagicMock()
    files_obj.get_media.return_value = media_req
    service = mock.MagicMock()
    service.files.return_value = files_obj
    return service, files_obj


# ── XLSX round-trip ─────────────────────────────────────────────────

class XlsxExtractorTests(unittest.TestCase):
    def test_extract_returns_sheet_names_and_rows(self):
        from agent_core.ingest import drive_sync
        data = _make_xlsx_bytes({
            "Pricing": [["SKU", "Cost"], ["A1", 100], ["B2", 250]],
            "Notes":   [["delivered to 大王 on 2026-04-01"]],
        })
        text = drive_sync._extract_xlsx(data)
        # Sheet names show up as headers
        self.assertIn("# Pricing", text)
        self.assertIn("# Notes", text)
        # Actual data round-trips
        self.assertIn("SKU", text)
        self.assertIn("A1", text)
        self.assertIn("100", text)
        self.assertIn("250", text)
        self.assertIn("大王", text)

    def test_extract_skips_empty_rows(self):
        from agent_core.ingest import drive_sync
        data = _make_xlsx_bytes({"S1": [["only", "row"], [None, None], ["after", "blank"]]})
        text = drive_sync._extract_xlsx(data)
        self.assertIn("only", text)
        self.assertIn("after", text)
        # Three data rows + 1 sheet header = 3 newlines max (the empty row was skipped).
        self.assertLessEqual(text.count("\n"), 3)

    def test_extract_includes_embedded_image_ocr(self):
        from agent_core.ingest import drive_sync

        data = _add_zip_member(
            _make_xlsx_bytes({"S1": [["cell text"]]}),
            "xl/media/image1.jpeg",
            b"x" * (drive_sync._IMAGE_MIN_BYTES + 1),
        )
        with mock.patch.object(
            drive_sync,
            "_extract_image",
            return_value="OCR:\nMATERIAL SWATCH\n\nDESC:\nblue textile sample",
        ) as extract_image:
            text = drive_sync._extract_xlsx(data)

        self.assertIn("cell text", text)
        self.assertIn("# Embedded image 1: xl/media/image1.jpeg", text)
        self.assertIn("MATERIAL SWATCH", text)
        extract_image.assert_called_once()

    def test_extract_keeps_cell_text_when_embedded_image_budget_stops(self):
        from agent_core.ingest import drive_sync
        from agent_core.ingest.vector_store import GeminiHardQuotaError

        data = _add_zip_member(
            _make_xlsx_bytes({"S1": [["cell text"]]}),
            "xl/media/image1.jpeg",
            b"x" * (drive_sync._IMAGE_MIN_BYTES + 1),
        )
        with mock.patch.object(
            drive_sync,
            "_extract_image",
            side_effect=GeminiHardQuotaError("daily budget exhausted"),
        ):
            text = drive_sync._extract_xlsx(data)

        self.assertIn("cell text", text)
        self.assertIn("Embedded image OCR skipped", text)


# ── PPTX round-trip ─────────────────────────────────────────────────

class PptxExtractorTests(unittest.TestCase):
    def test_extract_text_tables_and_notes(self):
        from agent_core.ingest import drive_sync
        data = _make_pptx_bytes([
            {
                "title": "生產進度報告",
                "body": "越南廠 8月 12000 雙",
                "table": [["料號", "數量"], ["A-2024", "500"]],
                "notes": "講者備忘稿內容",
            },
        ])
        text = drive_sync._extract_pptx(data)
        self.assertIn("# Slide 1", text)
        self.assertIn("生產進度報告", text)
        self.assertIn("越南廠", text)
        self.assertIn("料號", text)
        self.assertIn("A-2024", text)
        self.assertIn("[Speaker notes]: 講者備忘稿內容", text)

    def test_extract_drops_empty_slides(self):
        from agent_core.ingest import drive_sync
        data = _make_pptx_bytes([{"body": "有內容"}, {}])
        text = drive_sync._extract_pptx(data)
        self.assertIn("# Slide 1", text)
        self.assertNotIn("# Slide 2", text)

    def test_extract_includes_embedded_image_ocr(self):
        from agent_core.ingest import drive_sync
        data = _add_zip_member(
            _make_pptx_bytes([{"body": "slide text"}]),
            "ppt/media/image1.jpeg",
            b"x" * (drive_sync._IMAGE_MIN_BYTES + 1),
        )
        with mock.patch.object(
            drive_sync,
            "_extract_image",
            return_value="OCR:\nMATERIAL LOT A-2024",
        ) as extract_image:
            text = drive_sync._extract_pptx(data)

        self.assertIn("slide text", text)
        self.assertIn("# Embedded image 1: ppt/media/image1.jpeg", text)
        self.assertIn("MATERIAL LOT A-2024", text)
        extract_image.assert_called_once()

    def test_embedded_image_hard_quota_stops_but_keeps_text(self):
        from agent_core.ingest import drive_sync
        from agent_core.ingest.vector_store import GeminiHardQuotaError
        data = _add_zip_member(
            _make_pptx_bytes([{"body": "slide text"}]),
            "ppt/media/image1.jpeg",
            b"x" * (drive_sync._IMAGE_MIN_BYTES + 1),
        )
        with mock.patch.object(
            drive_sync,
            "_extract_image",
            side_effect=GeminiHardQuotaError("daily budget exhausted"),
        ):
            text = drive_sync._extract_pptx(data)

        self.assertIn("slide text", text)
        self.assertIn("Embedded image OCR skipped", text)

    def test_embedded_image_disabled_when_budget_zero(self):
        from agent_core.ingest import drive_sync
        data = _add_zip_member(
            _make_pptx_bytes([{"body": "slide text"}]),
            "ppt/media/image1.jpeg",
            b"x" * (drive_sync._IMAGE_MIN_BYTES + 1),
        )
        with mock.patch.object(drive_sync, "_PPTX_EMBEDDED_IMAGE_MAX_COUNT", 0), \
             mock.patch.object(
                 drive_sync, "_extract_image",
                 side_effect=AssertionError("must not OCR when budget is 0")):
            text = drive_sync._extract_pptx(data)

        self.assertIn("slide text", text)
        self.assertNotIn("Embedded image", text)

    def test_embedded_image_count_truncates(self):
        from agent_core.ingest import drive_sync
        data = _make_pptx_bytes([{"body": "slide text"}])
        for i in range(3):
            data = _add_zip_member(
                data,
                f"ppt/media/image{i}.jpeg",
                b"x" * (drive_sync._IMAGE_MIN_BYTES + 1),
            )
        with mock.patch.object(drive_sync, "_PPTX_EMBEDDED_IMAGE_MAX_COUNT", 2), \
             mock.patch.object(drive_sync, "_extract_image", return_value="OCR:\ntext"):
            text = drive_sync._extract_pptx(data)

        self.assertIn("Embedded image OCR truncated", text)
        self.assertIn("Processed 2 of 3 images", text)


# ── XLS / legacy Office / email extractors ──────────────────────────

class LegacyExtractorTests(unittest.TestCase):
    def test_extract_xls_returns_sheet_names_and_rows(self):
        import xlrd
        from agent_core.ingest import drive_sync

        class Cell:
            def __init__(self, ctype, value):
                self.ctype = ctype
                self.value = value

        class Sheet:
            name = "Legacy"
            nrows = 3

            def row(self, idx):
                rows = [
                    [
                        Cell(xlrd.XL_CELL_TEXT, "SKU"),
                        Cell(xlrd.XL_CELL_TEXT, "Cost"),
                    ],
                    [
                        Cell(xlrd.XL_CELL_TEXT, "A1"),
                        Cell(xlrd.XL_CELL_NUMBER, 100.0),
                    ],
                    [
                        Cell(xlrd.XL_CELL_EMPTY, ""),
                        Cell(xlrd.XL_CELL_BLANK, ""),
                    ],
                ]
                return rows[idx]

        book = mock.MagicMock(datemode=0)
        book.sheets.return_value = [Sheet()]
        with mock.patch("xlrd.open_workbook", return_value=book) as open_workbook:
            text = drive_sync._extract_xls(b"XLS")

        open_workbook.assert_called_once()
        kwargs = open_workbook.call_args.kwargs
        self.assertEqual(kwargs["file_contents"], b"XLS")
        self.assertTrue(kwargs["on_demand"])
        # xlrd 撞壞 NAME formula 時會往 logfile（預設 sys.stdout）噴 debug
        # dump，污染 daemon log — 必須導到別處丟棄。
        self.assertIn("logfile", kwargs)
        self.assertIsNot(kwargs["logfile"], sys.stdout)
        self.assertIn("# Legacy", text)
        self.assertIn("SKU\tCost", text)
        self.assertIn("A1\t100", text)
        book.release_resources.assert_called_once()

    def test_extract_xls_falls_back_to_excel_xml_spreadsheet(self):
        import xlrd
        from agent_core.ingest import drive_sync

        data = b"""
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
          xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"
          xmlns:html="http://www.w3.org/TR/REC-html40">
  <Worksheet ss:Name="DKL">
    <Table>
      <Row>
        <Cell><Data ss:Type="String">Material</Data></Cell>
        <Cell><Data ss:Type="String">Qty</Data></Cell>
      </Row>
      <Row>
        <Cell><Data ss:Type="String"><html:Font>Leather</html:Font></Data></Cell>
        <Cell><Data ss:Type="Number">12</Data></Cell>
      </Row>
    </Table>
  </Worksheet>
</Workbook>"""

        with mock.patch("xlrd.open_workbook", side_effect=xlrd.XLRDError("Expected BOF record")):
            text = drive_sync._extract_xls(data)

        self.assertIn("# DKL", text)
        self.assertIn("Material\tQty", text)
        self.assertIn("Leather\t12", text)

    def test_extract_xls_routes_ooxml_zip_bytes_to_xlsx_extractor(self):
        # 副檔名誤標 .xls 的 OOXML（INVOICE/PACKING LIST 這類實為 xlsx 的
        # 業務檔）：真 xlrd ≥ 2.0 會炸 XLRDError("Excel xlsx file; not
        # supported")，zip magic fallback 要改走 openpyxl 把內容救回來。
        from agent_core.ingest import drive_sync

        data = _make_xlsx_bytes({"Invoice": [["LOT", "Qty"], ["208-2026", 500]]})
        self.assertEqual(data[:4], b"PK\x03\x04")

        text = drive_sync._extract_xls(data)  # 不 mock xlrd — 走真實拒收路徑

        self.assertIn("# Invoice", text)
        self.assertIn("LOT\tQty", text)
        self.assertIn("208-2026\t500", text)

    def test_extract_xls_corrupt_zip_still_raises(self):
        # zip magic 但容器壞掉：openpyxl 的 BadZipFile 要照樣冒出（它在
        # _PERMANENT_EXTRACT_ERROR_SIGNATURES 裡），fallback 不能吞錯。
        import xlrd
        from agent_core.ingest import drive_sync

        with mock.patch(
            "xlrd.open_workbook",
            side_effect=xlrd.XLRDError("Excel xlsx file; not supported"),
        ):
            with self.assertRaises(zipfile.BadZipFile):
                drive_sync._extract_xls(b"PK\x03\x04corrupt-not-a-real-zip")

    def test_extract_xls_non_zip_garbage_still_raises_xlrderror(self):
        # 非 zip、非 XML 的垃圾 bytes、soffice 也不在：維持原行為，
        # XLRDError 原樣冒出。（mock 掉 soffice 探測，測試不依賴本機有沒有裝
        # LibreOffice、也不真的花幾秒起 soffice。）
        import xlrd
        from agent_core.ingest import drive_sync

        with mock.patch.object(drive_sync, "_find_soffice", return_value=None):
            with self.assertRaises(xlrd.XLRDError):
                drive_sync._extract_xls(b"\x00\x01garbage bytes")

    def test_extract_doc_uses_textutil(self):
        from agent_core.ingest import drive_sync

        proc = types.SimpleNamespace(returncode=0, stdout="legacy text".encode(), stderr=b"")
        with mock.patch.object(drive_sync.shutil, "which", return_value="/usr/bin/textutil"), \
             mock.patch.object(drive_sync.subprocess, "run", return_value=proc) as run:
            text = drive_sync._extract_doc(b"DOC")

        self.assertEqual(text, "legacy text")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:4], ["/usr/bin/textutil", "-convert", "txt", "-stdout"])
        self.assertTrue(cmd[4].endswith(".doc"))

    def test_extract_ppt_falls_back_to_strings(self):
        from agent_core.ingest import drive_sync

        proc = types.SimpleNamespace(returncode=0, stdout=b"Slide title\nAgenda\n", stderr=b"")
        with mock.patch.object(drive_sync.shutil, "which", return_value="/usr/bin/strings"), \
             mock.patch.object(drive_sync.subprocess, "run", return_value=proc):
            with mock.patch.object(drive_sync, "_extract_with_textutil", side_effect=RuntimeError("no ppt")):
                text = drive_sync._extract_ppt(b"PPT")

        self.assertIn("Slide title", text)
        self.assertIn("Agenda", text)

    def test_extract_textlike_binary_prefers_plain_text(self):
        from agent_core.ingest import drive_sync

        text = drive_sync._extract_textlike_binary(b"WEBVTT\n\n00:00 --> 00:05\nhello meeting\n")

        self.assertIn("hello meeting", text)

    def test_extract_msg_returns_headers_body_and_attachment_names(self):
        from agent_core.ingest import drive_sync

        def utf16(text):
            return text.encode("utf-16-le") + b"\x00\x00"

        streams = {
            ("__substg1.0_0037001F",): utf16("Deal update"),
            ("__substg1.0_0C1A001F",): utf16("sender@example.com"),
            ("__substg1.0_0E04001F",): utf16("buyer@example.com"),
            ("__substg1.0_1000001F",): utf16("請確認報價。"),
            (
                "__attach_version1.0_#00000000",
                "__substg1.0_3707001F",
            ): utf16("quote.xls"),
        }
        stream_map = streams

        class FakeOle:
            closed = False

            def openstream(self, path):
                key = tuple(path)
                if key not in streams:
                    raise FileNotFoundError(key)
                return io.BytesIO(streams[key])

            def listdir(self, streams=True, storages=False):
                return [list(path) for path in stream_map if len(path) > 1]

            def close(self):
                self.closed = True

        fake_ole = FakeOle()
        with mock.patch("olefile.OleFileIO", return_value=fake_ole):
            text = drive_sync._extract_msg(b"MSG")

        self.assertIn("Subject: Deal update", text)
        self.assertIn("From: sender@example.com", text)
        self.assertIn("請確認報價。", text)
        self.assertIn("quote.xls", text)
        self.assertTrue(fake_ole.closed)

    def test_extract_eml_returns_plain_body_and_attachment_names(self):
        from agent_core.ingest import drive_sync

        data = (
            b"Subject: Lake update\r\n"
            b"From: a@example.com\r\n"
            b"To: b@example.com\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=BOUND\r\n"
            b"\r\n"
            b"--BOUND\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            + "大王，附件請查收。".encode("utf-8")
            + b"\r\n--BOUND\r\n"
            b"Content-Type: application/pdf; name=invoice.pdf\r\n"
            b"Content-Disposition: attachment; filename=invoice.pdf\r\n"
            b"\r\nPDF\r\n"
            b"--BOUND--\r\n"
        )
        text = drive_sync._extract_eml(data)

        self.assertIn("Subject: Lake update", text)
        self.assertIn("大王，附件請查收。", text)
        self.assertIn("invoice.pdf", text)

    def test_extract_mhtml_returns_embedded_html_text(self):
        from agent_core.ingest import drive_sync

        data = (
            b"Subject: Saved page\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/related; boundary=BOUND\r\n"
            b"\r\n"
            b"--BOUND\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"\r\n"
            b"<html><body><h1>Material page</h1><script>ignore()</script>"
            b"<p>Swatch details</p></body></html>\r\n"
            b"--BOUND--\r\n"
        )

        text = drive_sync._extract_mhtml(data)

        self.assertIn("Subject: Saved page", text)
        self.assertIn("Material page", text)
        self.assertIn("Swatch details", text)
        self.assertNotIn("ignore", text)

    def test_extract_html_strips_scripts_and_tags(self):
        from agent_core.ingest import drive_sync

        text = drive_sync._extract_html(
            b"<html><script>bad()</script><body><h1>Hello</h1><p>World</p></body></html>"
        )

        self.assertIn("Hello", text)
        self.assertIn("World", text)
        self.assertNotIn("bad", text)
        self.assertNotIn("<h1>", text)

    def test_extract_odt_reads_content_xml(self):
        from agent_core.ingest import drive_sync

        data = _make_zip_bytes({
            "content.xml": """
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
    xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
  <office:body><office:text>
    <text:h>會議結論</text:h>
    <text:p>採購下週確認交期。</text:p>
  </office:text></office:body>
</office:document-content>
"""
        })

        text = drive_sync._extract_odf_text(data)

        self.assertIn("會議結論", text)
        self.assertIn("採購下週確認交期", text)

    def test_extract_ods_reads_table_rows(self):
        from agent_core.ingest import drive_sync

        data = _make_zip_bytes({
            "content.xml": """
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
    xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
    xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
  <office:body><office:spreadsheet>
    <table:table table:name="Stock">
      <table:table-row>
        <table:table-cell><text:p>SKU</text:p></table:table-cell>
        <table:table-cell><text:p>Qty</text:p></table:table-cell>
      </table:table-row>
      <table:table-row>
        <table:table-cell><text:p>A1</text:p></table:table-cell>
        <table:table-cell><text:p>12</text:p></table:table-cell>
      </table:table-row>
    </table:table>
  </office:spreadsheet></office:body>
</office:document-content>
"""
        })

        text = drive_sync._extract_ods(data)

        self.assertIn("# Stock", text)
        self.assertIn("SKU\tQty", text)
        self.assertIn("A1\t12", text)


# ── 真 BIFF 但 xlrd 解析不了 → LibreOffice headless 轉檔退路 ────────

class SofficeXlsFallbackTests(unittest.TestCase):
    """會計 Drive 2017-19 舊 .xls（真 BIFF）撞 xlrd 已知限制（"Excessive
    indirect references in NAME formula"）：內容完好，走 soffice --headless
    --convert-to xlsx 轉檔再抽；轉不了要讓 xlrd 原錯誤原樣冒出。"""

    # 非 zip、非 XML：真 xlrd 對它 raise XLRDError("… Expected BOF record …")，
    # 跟真 BIFF 解析失敗走同一條 except 路徑，不用 mock xlrd。
    _GARBAGE_BIFF = b"\x00\x01not-a-real-biff-stream"

    def _fake_soffice_run(self, xlsx_bytes: bytes, returncode: int = 0,
                          write_output: bool = True):
        """假 soffice：把 xlsx bytes 寫進 --outdir，並記下 outdir 供清理斷言。"""
        def _run(cmd, **kwargs):
            outdir = cmd[cmd.index("--outdir") + 1]
            self._outdir = outdir
            if write_output:
                with open(os.path.join(outdir, "input.xlsx"), "wb") as fh:
                    fh.write(xlsx_bytes)
            return types.SimpleNamespace(
                returncode=returncode, stdout=b"", stderr=b"convert error detail")
        return _run

    def test_unparseable_biff_converts_via_soffice_then_extracts(self):
        from agent_core.ingest import drive_sync
        xlsx = _make_xlsx_bytes({"匯款明細": [["日期", "金額"], ["2019-01-02", 35000]]})
        with mock.patch.object(drive_sync, "_find_soffice", return_value="/fake/soffice"), \
             mock.patch.object(drive_sync.subprocess, "run",
                               side_effect=self._fake_soffice_run(xlsx)) as run:
            text = drive_sync._extract_xls(self._GARBAGE_BIFF)

        self.assertIn("# 匯款明細", text)
        self.assertIn("日期\t金額", text)
        self.assertIn("35000", text)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "/fake/soffice")
        self.assertIn("--headless", cmd)
        self.assertIn("--convert-to", cmd)
        self.assertIn("xlsx", cmd)
        # 獨立 profile：不撞共用 profile 的單一 instance 鎖（GUI 開著時
        # headless 會靜默失敗）。
        self.assertTrue(any(a.startswith("-env:UserInstallation=") for a in cmd))
        # temp dir 用完即清。
        self.assertFalse(os.path.exists(self._outdir))

    def test_soffice_missing_reraises_original_xlrd_error(self):
        # 優雅降級：沒裝 LibreOffice → 不起子程序、xlrd 原錯誤冒出
        # （維持 retryable，裝上之後下一輪自動救回）。
        import xlrd
        from agent_core.ingest import drive_sync
        with mock.patch.object(drive_sync, "_find_soffice", return_value=None), \
             mock.patch.object(drive_sync.subprocess, "run",
                               side_effect=AssertionError("soffice 不存在時不得起子程序")):
            with self.assertRaises(xlrd.XLRDError) as cm:
                drive_sync._extract_xls(self._GARBAGE_BIFF)
        self.assertIn("Expected BOF record", str(cm.exception))

    def test_soffice_convert_failure_reraises_original_xlrd_error(self):
        import xlrd
        from agent_core.ingest import drive_sync
        with mock.patch.object(drive_sync, "_find_soffice", return_value="/fake/soffice"), \
             mock.patch.object(drive_sync.subprocess, "run",
                               side_effect=self._fake_soffice_run(
                                   b"", returncode=1, write_output=False)):
            with self.assertRaises(xlrd.XLRDError) as cm:
                drive_sync._extract_xls(self._GARBAGE_BIFF)
        self.assertIn("Expected BOF record", str(cm.exception))
        self.assertFalse(os.path.exists(self._outdir))

    def test_soffice_exit_zero_without_output_file_is_failure(self):
        # soffice 的靜默失敗模式：exit 0 但沒寫輸出檔，也要視為轉檔失敗。
        import xlrd
        from agent_core.ingest import drive_sync
        with mock.patch.object(drive_sync, "_find_soffice", return_value="/fake/soffice"), \
             mock.patch.object(drive_sync.subprocess, "run",
                               side_effect=self._fake_soffice_run(
                                   b"", returncode=0, write_output=False)):
            with self.assertRaises(xlrd.XLRDError):
                drive_sync._extract_xls(self._GARBAGE_BIFF)
        self.assertFalse(os.path.exists(self._outdir))

    def test_soffice_timeout_reraises_original_xlrd_error(self):
        import subprocess as sp

        import xlrd
        from agent_core.ingest import drive_sync
        with mock.patch.object(drive_sync, "_find_soffice", return_value="/fake/soffice"), \
             mock.patch.object(drive_sync.subprocess, "run",
                               side_effect=sp.TimeoutExpired(cmd="soffice", timeout=1)):
            with self.assertRaises(xlrd.XLRDError) as cm:
                drive_sync._extract_xls(self._GARBAGE_BIFF)
        self.assertIn("Expected BOF record", str(cm.exception))

    def test_sst_assertion_error_converts_via_soffice_then_extracts(self):
        # xlrd 解 SST 炸 AssertionError（book.py handle_sst 的內部斷言）——
        # 會計 Drive 的 PKL packing list 類 .xls：不是 XLRDError，也要走
        # soffice 轉檔退路。
        from agent_core.ingest import drive_sync
        xlsx = _make_xlsx_bytes({"PKL": [["CARTON", "QTY"], ["1-49", 1592]]})
        with mock.patch("xlrd.open_workbook", side_effect=AssertionError()), \
             mock.patch.object(drive_sync, "_find_soffice", return_value="/fake/soffice"), \
             mock.patch.object(drive_sync.subprocess, "run",
                               side_effect=self._fake_soffice_run(xlsx)):
            text = drive_sync._extract_xls(self._GARBAGE_BIFF)
        self.assertIn("# PKL", text)
        self.assertIn("CARTON\tQTY", text)
        self.assertIn("1592", text)
        self.assertFalse(os.path.exists(self._outdir))

    def test_sst_assertion_error_soffice_failure_reraises_assertion_error(self):
        # 轉不了要讓 AssertionError 原樣冒出——維持 retryable（str() 是空
        # 字串，skip-marker 簽名分類 fallback 到 type name，與現狀相同）。
        from agent_core.ingest import drive_sync
        with mock.patch("xlrd.open_workbook", side_effect=AssertionError()), \
             mock.patch.object(drive_sync, "_find_soffice", return_value="/fake/soffice"), \
             mock.patch.object(drive_sync.subprocess, "run",
                               side_effect=self._fake_soffice_run(
                                   b"", returncode=1, write_output=False)):
            with self.assertRaises(AssertionError):
                drive_sync._extract_xls(self._GARBAGE_BIFF)
        self.assertFalse(os.path.exists(self._outdir))

    def test_sst_assertion_error_soffice_missing_reraises_assertion_error(self):
        from agent_core.ingest import drive_sync
        with mock.patch("xlrd.open_workbook", side_effect=AssertionError()), \
             mock.patch.object(drive_sync, "_find_soffice", return_value=None):
            with self.assertRaises(AssertionError):
                drive_sync._extract_xls(self._GARBAGE_BIFF)

    @staticmethod
    def _utf16_decode_error():
        # xlrd 解 SST 撞非法 utf-16 編碼時的原始例外（越南文舊檔、
        # 年終獎金系列）："'utf-16-le' codec can't decode …: illegal encoding"
        return UnicodeDecodeError("utf-16-le", b"\x00\x01", 0, 2, "illegal encoding")

    def test_sst_unicode_decode_error_converts_via_soffice_then_extracts(self):
        # 同為 SST 解析炸掉、但例外型別是 UnicodeDecodeError（非 XLRDError、
        # 非 AssertionError）——也要走 soffice 轉檔退路。
        from agent_core.ingest import drive_sync
        xlsx = _make_xlsx_bytes({"CUT": [["Họ và tên", "Tiền thưởng"], ["A", 100]]})
        with mock.patch("xlrd.open_workbook", side_effect=self._utf16_decode_error()), \
             mock.patch.object(drive_sync, "_find_soffice", return_value="/fake/soffice"), \
             mock.patch.object(drive_sync.subprocess, "run",
                               side_effect=self._fake_soffice_run(xlsx)):
            text = drive_sync._extract_xls(self._GARBAGE_BIFF)
        self.assertIn("# CUT", text)
        self.assertIn("Họ và tên\tTiền thưởng", text)
        self.assertFalse(os.path.exists(self._outdir))

    def test_sst_unicode_decode_error_soffice_missing_reraises(self):
        from agent_core.ingest import drive_sync
        with mock.patch("xlrd.open_workbook", side_effect=self._utf16_decode_error()), \
             mock.patch.object(drive_sync, "_find_soffice", return_value=None):
            with self.assertRaises(UnicodeDecodeError):
                drive_sync._extract_xls(self._GARBAGE_BIFF)


# ── Gemini 不支援的媒體容器：確定性守門 ─────────────────────────────

class MediaUnsupportedMimeTests(unittest.TestCase):
    """video/mp2t（MPEG-TS）抽幀救不回時，Gemini fallback 上傳必 400——
    要 raise 固定簽名標永久跳過，不能每晚白傳一次。"""

    def test_mp2t_gemini_fallback_raises_permanent_signature(self):
        from agent_core.ingest import drive_sync
        with mock.patch.object(drive_sync, "get_embedding_hard_quota_message",
                               return_value=None), \
             mock.patch.object(drive_sync, "_get_media_stop_message", return_value=None), \
             mock.patch.object(drive_sync, "_DRIVE_ENABLE_VIDEO_FRAMES", True), \
             mock.patch.object(drive_sync, "_extract_video_via_frames", return_value=""):
            with self.assertRaises(RuntimeError) as cm:
                drive_sync._extract_media(b"TS", "video/mp2t")
        self.assertIn("media_mime_unsupported_by_gemini", str(cm.exception))
        # 簽名必須在永久清單裡，否則照樣每晚 retryable 重試。
        self.assertTrue(drive_sync._is_permanent_extract_error(cm.exception))

    def test_mp2t_frames_hit_still_returns_text(self):
        # 好的 TS 檔抽幀路徑吃得下——守門只攔「抽幀失敗後的 Gemini fallback」。
        from agent_core.ingest import drive_sync
        with mock.patch.object(drive_sync, "get_embedding_hard_quota_message",
                               return_value=None), \
             mock.patch.object(drive_sync, "_get_media_stop_message", return_value=None), \
             mock.patch.object(drive_sync, "_DRIVE_ENABLE_VIDEO_FRAMES", True), \
             mock.patch.object(drive_sync, "_extract_video_via_frames",
                               return_value="幀內容摘要"):
            out = drive_sync._extract_media(b"TS", "video/mp2t")
        self.assertEqual(out, "幀內容摘要")


# ── DOCX round-trip ─────────────────────────────────────────────────

class DocxExtractorTests(unittest.TestCase):
    def test_extract_returns_paragraphs_and_tables(self):
        from agent_core.ingest import drive_sync
        data = _make_docx_bytes(
            paragraphs=["合約第一條：付款條件 30 天內。", "合約第二條：交貨 FOB。"],
            tables=[[["項目", "金額"], ["訂金", "30%"], ["尾款", "70%"]]],
        )
        text = drive_sync._extract_docx(data)
        self.assertIn("合約第一條", text)
        self.assertIn("合約第二條", text)
        self.assertIn("訂金", text)
        self.assertIn("30%", text)

    def test_extract_skips_empty_paragraphs(self):
        from agent_core.ingest import drive_sync
        data = _make_docx_bytes(["real text", "", "   ", "more text"])
        text = drive_sync._extract_docx(data)
        self.assertIn("real text", text)
        self.assertIn("more text", text)
        self.assertEqual(text.count("\n"), 1)  # exactly two non-blank paragraphs


# ── PDF (mocked) ────────────────────────────────────────────────────

class PdfExtractorTests(unittest.TestCase):
    def test_extract_concats_pages(self):
        from agent_core.ingest import drive_sync
        page1 = mock.MagicMock()
        page1.extract_text.return_value = "page-one"
        page2 = mock.MagicMock()
        page2.extract_text.return_value = "page-two"
        reader = mock.MagicMock(is_encrypted=False, pages=[page1, page2])
        with mock.patch("pypdf.PdfReader", return_value=reader) as r_mock:
            text = drive_sync._extract_pdf(b"%PDF-FAKE")
        self.assertEqual(text, "page-one\n\npage-two")
        # Verify it was called with a BytesIO of the input bytes.
        passed_buf = r_mock.call_args[0][0]
        passed_buf.seek(0)
        self.assertEqual(passed_buf.read(), b"%PDF-FAKE")

    def test_extract_skips_blank_pages(self):
        from agent_core.ingest import drive_sync
        p1 = mock.MagicMock()
        p1.extract_text.return_value = "real"
        p2 = mock.MagicMock()
        p2.extract_text.return_value = ""
        p3 = mock.MagicMock()
        p3.extract_text.return_value = None
        reader = mock.MagicMock(is_encrypted=False, pages=[p1, p2, p3])
        with mock.patch("pypdf.PdfReader", return_value=reader):
            text = drive_sync._extract_pdf(b"x")
        self.assertEqual(text, "real")

    def test_encrypted_pdf_with_failed_decrypt_raises(self):
        from agent_core.ingest import drive_sync
        reader = mock.MagicMock(is_encrypted=True, pages=[])
        reader.decrypt.side_effect = Exception("wrong password")
        with mock.patch("pypdf.PdfReader", return_value=reader):
            with self.assertRaises(RuntimeError) as cm:
                drive_sync._extract_pdf(b"x")
        self.assertIn("encrypted_pdf", str(cm.exception))


# ── _export_file_text dispatch ──────────────────────────────────────

class ExportFileTextDispatchTests(unittest.TestCase):
    def test_pdf_dispatches_to_extractor(self):
        from agent_core.ingest import drive_sync
        service, files_obj = _service_with_media(b"PDF-PAYLOAD")
        with mock.patch.object(drive_sync, "_extract_pdf", return_value="extracted text") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._PDF_MIME)
        self.assertEqual(text, "extracted text")
        ex.assert_called_once_with(b"PDF-PAYLOAD")
        # Must call get_media with supportsAllDrives=True so Shared-Drive files work.
        files_obj.get_media.assert_called_once_with(fileId="fid", supportsAllDrives=True)

    def test_xlsx_dispatches_to_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"XLSX")
        with mock.patch.object(drive_sync, "_extract_xlsx", return_value="rows") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._XLSX_MIME)
        self.assertEqual(text, "rows")
        ex.assert_called_once_with(b"XLSX")

    def test_xlsm_uses_xlsx_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"XLSM")
        with mock.patch.object(drive_sync, "_extract_xlsx", return_value="rows") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._XLSM_MIME)
        self.assertEqual(text, "rows")
        ex.assert_called_once_with(b"XLSM")

    def test_xlsm_camel_mime_uses_xlsx_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"XLSM")
        with mock.patch.object(drive_sync, "_extract_xlsx", return_value="rows") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._XLSM_MIME_CAMEL)
        self.assertEqual(text, "rows")
        ex.assert_called_once_with(b"XLSM")

    def test_xltx_uses_xlsx_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"XLTX")
        with mock.patch.object(drive_sync, "_extract_xlsx", return_value="template rows") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._XLTX_MIME)
        self.assertEqual(text, "template rows")
        ex.assert_called_once_with(b"XLTX")

    def test_xls_dispatches_to_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"XLS")
        with mock.patch.object(drive_sync, "_extract_xls", return_value="legacy rows") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._XLS_MIME)
        self.assertEqual(text, "legacy rows")
        ex.assert_called_once_with(b"XLS")

    def test_doc_dispatches_to_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"DOC")
        with mock.patch.object(drive_sync, "_extract_doc", return_value="legacy doc") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._DOC_MIME)
        self.assertEqual(text, "legacy doc")
        ex.assert_called_once_with(b"DOC")

    def test_ppt_dispatches_to_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"PPT")
        with mock.patch.object(drive_sync, "_extract_ppt", return_value="legacy slides") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._PPT_MIME)
        self.assertEqual(text, "legacy slides")
        ex.assert_called_once_with(b"PPT")

    def test_docx_dispatches_to_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"DOCX")
        with mock.patch.object(drive_sync, "_extract_docx", return_value="paras") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._DOCX_MIME)
        self.assertEqual(text, "paras")
        ex.assert_called_once_with(b"DOCX")

    def test_dotx_uses_docx_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"DOTX")
        with mock.patch.object(drive_sync, "_extract_docx", return_value="template paras") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._DOTX_MIME)
        self.assertEqual(text, "template paras")
        ex.assert_called_once_with(b"DOTX")

    def test_msg_dispatches_to_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"MSG")
        with mock.patch.object(drive_sync, "_extract_msg", return_value="mail") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._MSG_MIME)
        self.assertEqual(text, "mail")
        ex.assert_called_once_with(b"MSG")

    def test_eml_dispatches_to_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"EML")
        with mock.patch.object(drive_sync, "_extract_eml", return_value="mail") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._EML_MIME)
        self.assertEqual(text, "mail")
        ex.assert_called_once_with(b"EML")

    def test_mhtml_dispatches_to_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"MHTML")
        with mock.patch.object(drive_sync, "_extract_mhtml", return_value="saved page") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._MHTML_MIME)
        self.assertEqual(text, "saved page")
        ex.assert_called_once_with(b"MHTML")

    def test_rtf_dispatches_to_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"RTF")
        with mock.patch.object(drive_sync, "_extract_rtf", return_value="rtf") as ex:
            text = drive_sync._export_file_text(service, "fid", "application/rtf")
        self.assertEqual(text, "rtf")
        ex.assert_called_once_with(b"RTF")

    def test_ods_dispatches_to_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"ODS")
        with mock.patch.object(drive_sync, "_extract_ods", return_value="rows") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._ODS_MIME)
        self.assertEqual(text, "rows")
        ex.assert_called_once_with(b"ODS")

    def test_octet_stream_dispatches_to_textlike_binary_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"WEBVTT\ncaption")
        with mock.patch.object(drive_sync, "_extract_textlike_binary", return_value="caption") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._OCTET_MIME)
        self.assertEqual(text, "caption")
        ex.assert_called_once_with(b"WEBVTT\ncaption")

    def test_ai_dispatches_to_ai_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"%!PS-Adobe")
        with mock.patch.object(drive_sync, "_extract_ai", return_value="artboard text") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._AI_MIME)
        self.assertEqual(text, "artboard text")
        ex.assert_called_once_with(b"%!PS-Adobe")

    def test_tnef_dispatches_to_textlike_binary_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"winmail data")
        with mock.patch.object(drive_sync, "_extract_textlike_binary", return_value="attachment names") as ex:
            text = drive_sync._export_file_text(service, "fid", drive_sync._TNEF_MIME)
        self.assertEqual(text, "attachment names")
        ex.assert_called_once_with(b"winmail data")

    def test_numbers_dispatches_to_textlike_binary_extractor(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"numbers package")
        with mock.patch.object(drive_sync, "_extract_textlike_binary", return_value="sheet text") as ex:
            text = drive_sync._export_file_text(service, "fid", "application/x-iwork-numbers-sffnumbers")
        self.assertEqual(text, "sheet text")
        ex.assert_called_once_with(b"numbers package")

    def test_html_direct_text_is_cleaned(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"<h1>Hello</h1><script>bad()</script><p>World</p>")
        text = drive_sync._export_file_text(service, "fid", "text/html")
        self.assertIn("Hello", text)
        self.assertIn("World", text)
        self.assertNotIn("bad", text)

    def test_vcard_direct_text_is_supported(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"BEGIN:VCARD\nFN:Alice Chen\nTEL:123\nEND:VCARD")
        text = drive_sync._export_file_text(service, "fid", "text/x-vcard")
        self.assertIn("FN:Alice Chen", text)
        self.assertIn("TEL:123", text)

    def test_chemical_ascii_direct_text_is_supported(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"LOCUS sample\nORIGIN text")
        text = drive_sync._export_file_text(service, "fid", "chemical/x-ncbi-asn1-ascii")
        self.assertIn("LOCUS sample", text)

    def test_unsupported_mime_returns_none(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"")
        text = drive_sync._export_file_text(service, "fid", "image/jpeg")
        self.assertIsNone(text)

    def test_image_dispatch_requires_explicit_opt_in(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"JPEG")
        with mock.patch.object(drive_sync, "_DRIVE_ENABLE_IMAGE_INGEST", True), \
             mock.patch.object(drive_sync, "_extract_image", return_value="ocr text") as ex:
            text = drive_sync._export_file_text(service, "fid", "image/jpeg")
        self.assertEqual(text, "ocr text")
        ex.assert_called_once_with(b"JPEG", "image/jpeg")

    def test_convertible_image_dispatch_requires_explicit_opt_in(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"PSD")
        with mock.patch.object(drive_sync, "_DRIVE_ENABLE_IMAGE_INGEST", True), \
             mock.patch.object(drive_sync, "_extract_convertible_image", return_value="design ocr") as ex:
            text = drive_sync._export_file_text(service, "fid", "image/x-photoshop")
        self.assertEqual(text, "design ocr")
        ex.assert_called_once_with(b"PSD", "image/x-photoshop")

    def test_media_dispatch_requires_explicit_opt_in(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"MP4")
        with mock.patch.object(drive_sync, "_DRIVE_ENABLE_MEDIA_INGEST", True), \
             mock.patch.object(drive_sync, "_extract_media", return_value="transcript") as ex:
            text = drive_sync._export_file_text(service, "fid", "video/mp4")
        self.assertEqual(text, "transcript")
        ex.assert_called_once_with(b"MP4", "video/mp4")

    def test_mp2t_media_dispatch_requires_explicit_opt_in(self):
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"TS")
        with mock.patch.object(drive_sync, "_DRIVE_ENABLE_MEDIA_INGEST", True), \
             mock.patch.object(drive_sync, "_extract_media", return_value="transport stream") as ex:
            text = drive_sync._export_file_text(service, "fid", "video/mp2t")
        self.assertEqual(text, "transport stream")
        ex.assert_called_once_with(b"TS", "video/mp2t")

    def test_denied_archive_and_executable_mimes_stay_unsupported(self):
        from agent_core.ingest import drive_sync

        supported = drive_sync._supported_mime_set()
        listing = drive_sync._drive_listing_mimes()

        self.assertNotIn("image/x-coreldraw", supported)
        self.assertNotIn("application/x-msdownload", supported)
        self.assertIn("application/x-msdownload", listing)
        self.assertIn("application/x-msi", listing)
        self.assertIn("application/msaccess", listing)
        self.assertNotIn("application/msaccess", supported)
        self.assertIn("application/vnd.google-apps.shortcut", listing)
        self.assertNotIn("application/vnd.google-apps.shortcut", supported)
        self.assertNotIn("application/vnd.ms-htmlhelp", supported)
        self.assertNotIn("application/zip", supported)
        self.assertNotIn("application/x-zip-compressed", supported)
        self.assertNotIn("application/rar", supported)
        self.assertNotIn("application/x-rar", supported)
        self.assertIn("application/x-font-ttf", supported)
        self.assertIn("application/x-cab", supported)
        self.assertTrue(drive_sync._is_drive_junk_file("installer.exe"))
        self.assertTrue(drive_sync._is_drive_junk_file("backup.zip"))
        self.assertTrue(drive_sync._is_drive_junk_file("drawing.cdr"))

    def test_direct_sync_denied_suffix_skips_before_download_even_when_quota_exhausted(self):
        from agent_core.ingest import drive_sync

        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at": "",
            "folder_id": "",
            "drive_id": "",
            "content_hash": "",
            "title": "",
            "sync_complete": True,
        }

        meta_req = mock.MagicMock()
        meta_req.execute.return_value = {
            "id": "fid",
            "name": "backup.zip",
            "mimeType": drive_sync._OCTET_MIME,
            "modifiedTime": "2026-05-18T10:00:00.000Z",
            "parents": ["folder1"],
            "size": "123",
        }
        files_obj = mock.MagicMock()
        files_obj.get.return_value = meta_req
        service = mock.MagicMock()
        service.files.return_value = files_obj

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                drive_sync,
                "_SKIP_STATE_FILE",
                os.path.join(tmp, "drive_sync_skip_state.json"),
            ), \
                 mock.patch.object(drive_sync, "get_store", return_value=store), \
                 mock.patch("agent_core.google_auth.get_service", return_value=service), \
                 mock.patch.object(
                     drive_sync,
                     "get_embedding_hard_quota_message",
                     return_value="monthly cap",
                 ), \
                 mock.patch.object(drive_sync, "_export_file_text_for_sync") as export_text:
                drive_sync._SKIP_STATE_CACHE = None
                try:
                    result = drive_sync.sync_file("fid")
                    marker = drive_sync._get_matching_skip_marker(
                        "fid",
                        modified_time="2026-05-18T10:00:00.000Z",
                        folder_id="folder1",
                        drive_id="",
                    )
                finally:
                    drive_sync._SKIP_STATE_CACHE = None

        self.assertEqual(result["reason"], "ignored filename")
        self.assertEqual(result["title"], "backup.zip")
        self.assertTrue(result["skipped"])
        store.delete_by_doc_id.assert_called_once_with("fid")
        export_text.assert_not_called()
        files_obj.get_media.assert_not_called()
        self.assertIsNotNone(marker)
        self.assertEqual(marker["reason"], "ignored filename")

    def test_direct_sync_ignored_mime_skips_before_download_even_when_quota_exhausted(self):
        from agent_core.ingest import drive_sync

        store = mock.MagicMock()
        store.get_doc_metadata.return_value = {
            "synced_at": "",
            "folder_id": "",
            "drive_id": "",
            "content_hash": "",
            "title": "",
            "sync_complete": True,
        }

        meta_req = mock.MagicMock()
        meta_req.execute.return_value = {
            "id": "fid",
            "name": "driver.dll",
            "mimeType": "application/x-msdownload",
            "modifiedTime": "2026-05-18T10:00:00.000Z",
            "parents": ["folder1"],
            "size": "123",
        }
        files_obj = mock.MagicMock()
        files_obj.get.return_value = meta_req
        service = mock.MagicMock()
        service.files.return_value = files_obj

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                drive_sync,
                "_SKIP_STATE_FILE",
                os.path.join(tmp, "drive_sync_skip_state.json"),
            ), \
                 mock.patch.object(drive_sync, "get_store", return_value=store), \
                 mock.patch("agent_core.google_auth.get_service", return_value=service), \
                 mock.patch.object(
                     drive_sync,
                     "get_embedding_hard_quota_message",
                     return_value="monthly cap",
                 ), \
                 mock.patch.object(drive_sync, "_export_file_text_for_sync") as export_text:
                drive_sync._SKIP_STATE_CACHE = None
                try:
                    result = drive_sync.sync_file("fid")
                    marker = drive_sync._get_matching_skip_marker(
                        "fid",
                        modified_time="2026-05-18T10:00:00.000Z",
                        folder_id="folder1",
                        drive_id="",
                    )
                finally:
                    drive_sync._SKIP_STATE_CACHE = None

        self.assertEqual(result["reason"], "ignored file_type")
        self.assertEqual(result["title"], "driver.dll")
        self.assertTrue(result["skipped"])
        store.delete_by_doc_id.assert_called_once_with("fid")
        export_text.assert_not_called()
        files_obj.get_media.assert_not_called()
        self.assertIsNotNone(marker)
        self.assertEqual(marker["reason"], "ignored file_type")
        self.assertEqual(marker["mime_type"], "application/x-msdownload")

    def test_extractor_exception_bubbles_does_not_return_none(self):
        """A parse failure must raise — never return None — so sync_file's outer
        try/except records 'error: …' and does NOT delete the file's existing
        chunks (returning None would trigger delete_by_doc_id)."""
        from agent_core.ingest import drive_sync
        service, _ = _service_with_media(b"BAD-PDF")
        with mock.patch.object(drive_sync, "_extract_pdf", side_effect=RuntimeError("parse boom")):
            with self.assertRaises(RuntimeError):
                drive_sync._export_file_text(service, "fid", drive_sync._PDF_MIME)


class ImageBudgetGuardTests(unittest.TestCase):
    def test_cost_budget_message_latches_when_daily_image_spend_hits_limit(self):
        from agent_core.ingest import drive_sync

        with mock.patch.object(drive_sync, "_IMAGE_DAILY_COST_LIMIT_USD", 1.0), \
             mock.patch.object(drive_sync, "_IMAGE_COST_CHECKED_AT", 0.0), \
             mock.patch.object(drive_sync, "_IMAGE_COST_BUDGET_MESSAGE", None), \
             mock.patch.object(drive_sync, "_caller_cost_today_usd", return_value=1.25):
            msg = drive_sync._get_image_cost_budget_message()

        self.assertIn("daily Drive image OCR budget exceeded", msg)
        self.assertIn("$1.2500", msg)

    def test_extract_image_stops_before_gemini_when_budget_is_exhausted(self):
        from agent_core.ingest import drive_sync
        from agent_core.ingest.vector_store import GeminiHardQuotaError

        payload = b"x" * (drive_sync._IMAGE_MIN_BYTES + 1)
        with mock.patch.object(drive_sync, "get_embedding_hard_quota_message", return_value=None), \
             mock.patch.object(drive_sync, "_get_image_cost_budget_message", return_value="daily budget exhausted"):
            with self.assertRaises(GeminiHardQuotaError):
                drive_sync._extract_image(payload, "image/jpeg")


try:
    import ocrmac.ocrmac as _ocrmod  # noqa: F401
    _HAS_OCRMAC = True
except Exception:  # noqa: BLE001 — 非 macOS / 沒裝
    _HAS_OCRMAC = False


class VisionOcrTests(unittest.TestCase):
    """圖片 OCR 改走 macOS Vision（免費）先行、Gemini 為 fallback。"""

    @staticmethod
    def _png_bytes():
        import io as _io

        from PIL import Image
        buf = _io.BytesIO()
        Image.new("RGB", (12, 12), "white").save(buf, format="PNG")
        return buf.getvalue()

    @unittest.skipUnless(_HAS_OCRMAC, "ocrmac macOS-only")
    def test_vision_joins_recognized_text(self):
        from agent_core.ingest import drive_sync
        fake = mock.MagicMock()
        fake.recognize.return_value = [("INVOICE", 0.99, None), ("279.41", 0.9, None)]
        with mock.patch.object(_ocrmod, "OCR", return_value=fake):
            text = drive_sync._extract_image_vision(self._png_bytes(), "image/png")
        self.assertEqual(text, "INVOICE 279.41")

    @unittest.skipUnless(_HAS_OCRMAC, "ocrmac macOS-only")
    def test_vision_returns_none_when_no_text(self):
        from agent_core.ingest import drive_sync
        fake = mock.MagicMock()
        fake.recognize.return_value = []
        with mock.patch.object(_ocrmod, "OCR", return_value=fake):
            self.assertIsNone(drive_sync._extract_image_vision(self._png_bytes(), "image/png"))

    def test_vision_returns_none_when_disabled(self):
        from agent_core.ingest import drive_sync
        with mock.patch.object(drive_sync, "_VISION_OCR_ENABLED", False):
            self.assertIsNone(drive_sync._extract_image_vision(self._png_bytes(), "image/png"))

    def test_vision_hit_skips_gemini(self):
        from agent_core.ingest import drive_sync
        payload = b"x" * (drive_sync._IMAGE_MIN_BYTES + 1)
        with mock.patch.object(drive_sync, "_extract_image_vision", return_value="A" * 40), \
             mock.patch("agent_core.gemini_client._gemini_generate") as gem:
            out = drive_sync._extract_image(payload, "image/jpeg")
        self.assertTrue(out.startswith("OCR:\n"))
        self.assertIn("A" * 40, out)
        gem.assert_not_called()   # Vision 命中 → 完全不打 Gemini

    def test_vision_miss_falls_back_to_gemini(self):
        import types as _types

        from agent_core.ingest import drive_sync
        payload = b"x" * (drive_sync._IMAGE_MIN_BYTES + 1)
        resp = _types.SimpleNamespace(text="OCR:\nfrom gemini\n\nDESC:\nshoe photo")
        with mock.patch.object(drive_sync, "_extract_image_vision", return_value=None), \
             mock.patch.object(drive_sync, "get_embedding_hard_quota_message", return_value=None), \
             mock.patch.object(drive_sync, "_get_image_stop_message", return_value=None), \
             mock.patch("agent_core.gemini_client._gemini_generate", return_value=resp) as gem:
            out = drive_sync._extract_image(payload, "image/jpeg")
        gem.assert_called_once()
        self.assertIn("from gemini", out)

    def test_vision_sparse_text_falls_back_to_gemini(self):
        import types as _types

        from agent_core.ingest import drive_sync
        payload = b"x" * (drive_sync._IMAGE_MIN_BYTES + 1)
        resp = _types.SimpleNamespace(text="OCR:\nfrom gemini\n\nDESC:\nx")
        with mock.patch.object(drive_sync, "_extract_image_vision", return_value="hi"), \
             mock.patch.object(drive_sync, "get_embedding_hard_quota_message", return_value=None), \
             mock.patch.object(drive_sync, "_get_image_stop_message", return_value=None), \
             mock.patch("agent_core.gemini_client._gemini_generate", return_value=resp) as gem:
            out = drive_sync._extract_image(payload, "image/jpeg")
        gem.assert_called_once()   # 2 字 < 門檻 24 → fallback
        self.assertIn("from gemini", out)

    def test_vision_only_sparse_uses_ocrmac_not_gemini(self):
        # RAG_IMAGE_VISION_ONLY=1：ocrmac 抽到少量字也用、完全不打 Gemini
        from agent_core.ingest import drive_sync
        payload = b"x" * (drive_sync._IMAGE_MIN_BYTES + 1)
        with mock.patch.object(drive_sync, "_IMAGE_VISION_ONLY", True), \
             mock.patch.object(drive_sync, "_extract_image_vision", return_value="FC123"), \
             mock.patch("agent_core.gemini_client._gemini_generate") as gem:
            out = drive_sync._extract_image(payload, "image/jpeg")
        self.assertEqual(out, "OCR:\nFC123")
        gem.assert_not_called()

    def test_vision_only_textless_skips_not_gemini(self):
        # RAG_IMAGE_VISION_ONLY=1：ocrmac 全無字（自然照片）→ 回 ""、不打 Gemini
        from agent_core.ingest import drive_sync
        payload = b"x" * (drive_sync._IMAGE_MIN_BYTES + 1)
        with mock.patch.object(drive_sync, "_IMAGE_VISION_ONLY", True), \
             mock.patch.object(drive_sync, "_extract_image_vision", return_value=None), \
             mock.patch("agent_core.gemini_client._gemini_generate") as gem:
            out = drive_sync._extract_image(payload, "image/jpeg")
        self.assertEqual(out, "")
        gem.assert_not_called()


class ExportFileTextForSyncTests(unittest.TestCase):
    def test_binary_download_is_extracted_under_process_timeout_helper(self):
        from agent_core.ingest import drive_sync
        service, files_obj = _service_with_media(b"PDF-PAYLOAD")
        with mock.patch.object(
            drive_sync,
            "_run_binary_extract_with_timeout",
            return_value="process text",
        ) as run_extract:
            text = drive_sync._export_file_text_for_sync(service, "fid", drive_sync._PDF_MIME)

        self.assertEqual(text, "process text")
        files_obj.get_media.assert_called_once_with(fileId="fid", supportsAllDrives=True)
        run_extract.assert_called_once_with(
            drive_sync._PDF_MIME,
            b"PDF-PAYLOAD",
            "Drive file text fid",
        )

    def test_google_slides_exports_pptx_then_pool_extracts(self):
        from agent_core.ingest import drive_sync
        export_req = mock.MagicMock()
        export_req.execute.return_value = b"PPTX-BYTES"
        files_obj = mock.MagicMock()
        files_obj.export.return_value = export_req
        service = mock.MagicMock()
        service.files.return_value = files_obj

        with mock.patch.object(
            drive_sync,
            "_run_binary_extract_with_timeout",
            return_value="slide text",
        ) as run_extract:
            text = drive_sync._export_file_text_for_sync(
                service, "fid", drive_sync._GSLIDES_MIME)

        self.assertEqual(text, "slide text")
        # Slides export as .pptx (keeps images) — never text/plain on the happy path.
        files_obj.export.assert_called_once_with(
            fileId="fid", mimeType=drive_sync._PPTX_MIME)
        # Extraction of the image-bearing binary is delegated to the SIGSEGV-isolated pool.
        run_extract.assert_called_once_with(
            drive_sync._PPTX_MIME, b"PPTX-BYTES", "Drive file text fid")

    def test_google_slides_falls_back_to_text_when_pptx_export_fails(self):
        from agent_core.ingest import drive_sync
        calls = []

        def _export(fileId, mimeType):  # noqa: N803 — Google API kwarg name
            calls.append(mimeType)
            req = mock.MagicMock()
            if mimeType == drive_sync._PPTX_MIME:
                req.execute.side_effect = RuntimeError("exportSizeLimitExceeded")
            else:
                req.execute.return_value = b"plain slide text"
            return req

        files_obj = mock.MagicMock()
        files_obj.export = _export
        service = mock.MagicMock()
        service.files.return_value = files_obj

        # The text fallback is already plain text — it must NOT go through the
        # binary extractor (that path is only for the .pptx bytes).
        with mock.patch.object(
            drive_sync,
            "_run_binary_extract_with_timeout",
            side_effect=AssertionError("binary extract must not run on text fallback"),
        ):
            text = drive_sync._export_file_text_for_sync(
                service, "fid", drive_sync._GSLIDES_MIME)

        self.assertEqual(text, "plain slide text")
        self.assertEqual(calls, [drive_sync._PPTX_MIME, "text/plain"])


class ExtractSizeGuardTests(unittest.TestCase):
    """Oversized binary files must be skipped BEFORE the spawn/extract that
    SIGSEGV'd the rag_sync daemon (75 MB xlsx, 2026-06-01)."""

    def test_oversized_raises_before_extract(self):
        from agent_core.ingest import drive_sync
        big = b"x" * (drive_sync._DRIVE_EXTRACT_MAX_BYTES + 1)
        # _extract_binary_text must NOT be reached — the guard fires first.
        with mock.patch.object(drive_sync, "_extract_binary_text",
                                 side_effect=AssertionError("extract reached")):
            with self.assertRaises(drive_sync._ExtractTooLargeError):
                drive_sync._run_binary_extract_with_timeout(
                    drive_sync._XLSX_MIME, big, "Drive file text fid")

    def test_under_limit_passes_through(self):
        from agent_core.ingest import drive_sync
        small = b"y" * 2048
        with mock.patch.object(drive_sync, "_extract_binary_text",
                                 return_value="OK") as m, \
             mock.patch.object(drive_sync, "_DRIVE_FILE_TEXT_TIMEOUT_S", 0):
            out = drive_sync._run_binary_extract_with_timeout(
                drive_sync._XLSX_MIME, small, "Drive file text fid")
        self.assertEqual(out, "OK")
        self.assertTrue(m.called)

    def test_for_sync_propagates_too_large(self):
        from agent_core.ingest import drive_sync
        big = b"z" * (drive_sync._DRIVE_EXTRACT_MAX_BYTES + 1)
        service, _ = _service_with_media(big)
        with self.assertRaises(drive_sync._ExtractTooLargeError):
            drive_sync._export_file_text_for_sync(
                service, "fid", drive_sync._XLSX_MIME)


# ── supported MIME plumbing ─────────────────────────────────────────

class SupportedMimesTests(unittest.TestCase):
    def test_supported_list_includes_new_types(self):
        from agent_core.ingest import drive_sync
        mimes = drive_sync._supported_mimes()
        for required in (
            drive_sync._PDF_MIME,
            drive_sync._XLS_MIME,
            drive_sync._XLSX_MIME,
            drive_sync._XLSM_MIME,
            drive_sync._XLSM_MIME_CAMEL,
            drive_sync._DOC_MIME,
            drive_sync._DOCX_MIME,
            drive_sync._MSG_MIME,
            drive_sync._EML_MIME,
            "application/rtf",
            "text/html",
            "text/xml",
            "application/json",
            "text/x-sql",
            "text/vtt",
            "text/plain",
            "application/vnd.google-apps.document",
            drive_sync._GSLIDES_MIME,
        ):
            self.assertIn(required, mimes)
        self.assertNotIn("image/jpeg", mimes)

    def test_supported_list_includes_images_only_when_opted_in(self):
        from agent_core.ingest import drive_sync
        with mock.patch.object(drive_sync, "_DRIVE_ENABLE_IMAGE_INGEST", True):
            mimes = drive_sync._supported_mimes()
        self.assertIn("image/jpeg", mimes)
        self.assertIn("image/png", mimes)

    def test_list_drive_files_query_filters_for_new_types(self):
        from agent_core.ingest import drive_sync
        captured: dict = {}

        def _list(**kwargs):
            captured.update(kwargs)
            req = mock.MagicMock()
            req.execute.return_value = {"files": []}
            return req

        files_obj = mock.MagicMock()
        files_obj.list = _list
        service = mock.MagicMock()
        service.files.return_value = files_obj

        drive_sync._list_drive_files(service, "0AABCDEF")
        q = captured["q"]
        self.assertIn(drive_sync._PDF_MIME, q)
        self.assertIn(drive_sync._XLS_MIME, q)
        self.assertIn(drive_sync._XLSX_MIME, q)
        self.assertIn(drive_sync._XLSM_MIME, q)
        self.assertIn(drive_sync._DOC_MIME, q)
        self.assertIn(drive_sync._DOCX_MIME, q)
        self.assertIn(drive_sync._MSG_MIME, q)
        self.assertIn("text/html", q)


class XmlEntityExpansionGuardTests(unittest.TestCase):
    """untrusted Drive XML 走 defusedxml，billion-laughs DTD 實體必須被擋、不展開。"""

    _BOMB = (
        '<?xml version="1.0"?><!DOCTYPE lolz ['
        '<!ENTITY a "AAAAAAAAAA">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        ']><office:document xmlns:office="urn:x"><office:body>&b;</office:body></office:document>'
    )

    def test_spreadsheet_xml_rejects_entity_bomb(self):
        from agent_core.ingest import drive_sync
        with self.assertRaises(Exception):  # defusedxml EntitiesForbidden
            drive_sync._extract_spreadsheet_xml(self._BOMB.encode())

    def test_ods_rejects_entity_bomb(self):
        from agent_core.ingest import drive_sync
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("content.xml", self._BOMB)
        with self.assertRaises(Exception):
            drive_sync._extract_ods(buf.getvalue())

    def test_odf_text_rejects_entity_bomb(self):
        from agent_core.ingest import drive_sync
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("content.xml", self._BOMB)
        with self.assertRaises(Exception):
            drive_sync._extract_odf_text(buf.getvalue())

    def test_legit_ods_still_extracts(self):
        from agent_core.ingest import drive_sync
        good = (
            '<?xml version="1.0"?><doc xmlns:table="urn:x">'
            '<table:table table:name="S"><table:table-row><table:table-cell>'
            '<text:p xmlns:text="urn:t">hello &amp; world</text:p>'
            '</table:table-cell></table:table-row></table:table></doc>'
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("content.xml", good)
        self.assertIn("hello", drive_sync._extract_ods(buf.getvalue()))


if __name__ == "__main__":
    unittest.main()
