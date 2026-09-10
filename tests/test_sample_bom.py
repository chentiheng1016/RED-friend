"""read_sample_bom 的純函式 + 組裝測試（mock，不依賴 live var/ 或真 Drive）。"""
import unittest
from unittest import mock

import agent_core.sample_status as ss
from agent_core.sample_status import (
    _bom_join_key, _pick_bom_files, _is_header_row,
)


class BomJoinKeyTests(unittest.TestCase):
    def test_extracts_digit_run(self):
        self.assertEqual(_bom_join_key("JALAS 5618"), "5618")
        self.assertEqual(_bom_join_key("JA#1155"), "1155")
        self.assertEqual(_bom_join_key("  5618  "), "5618")

    def test_no_digit_run_returns_stripped(self):
        self.assertEqual(_bom_join_key("abc"), "abc")
        self.assertEqual(_bom_join_key("   "), "")
        self.assertEqual(_bom_join_key(""), "")

    def test_short_tokens_not_treated_as_order_no(self):
        # 'FY26' 的 26 只 2 位、不算單號（樣品單號都 ≥3 位）→ 退原字串
        self.assertEqual(_bom_join_key("FY26"), "FY26")


class PickBomFilesTests(unittest.TestCase):
    def test_prioritizes_hinted_sheets_newest_first(self):
        files = [
            {"name": "5618 用量明細 Price BOM.xlsx", "id": "a", "modifiedTime": "2026-06-18"},
            {"name": "匯總 JA#5618.xlsx", "id": "b", "modifiedTime": "2026-06-15"},
            {"name": "RE_ JALAS 5618.msg", "id": "c", "modifiedTime": "2026-06-24"},
            {"name": "Invoice 5618.pdf", "id": "d", "modifiedTime": "2026-06-16"},
            {"name": "RFQ_5618.xlsx", "id": "f", "modifiedTime": "2026-05-27"},  # xlsx 但非 hinted
        ]
        picks = _pick_bom_files(files, max_files=2)
        self.assertEqual([p["id"] for p in picks], ["a", "b"])

    def test_falls_back_to_plain_xlsx_when_no_hint(self):
        files = [
            {"name": "RFQ_5618.xlsx", "id": "f", "modifiedTime": "2026-05-27"},
            {"name": "note.msg", "id": "c", "modifiedTime": "2026-06-24"},
        ]
        self.assertEqual([p["id"] for p in _pick_bom_files(files, max_files=2)], ["f"])

    def test_excludes_non_sheets(self):
        files = [
            {"name": "x.msg", "id": "1", "modifiedTime": "t"},
            {"name": "y.pdf", "id": "2", "modifiedTime": "t"},
            {"name": "z", "id": "3", "modifiedTime": "t", "mimeType": "application/vnd.google-apps.folder"},
        ]
        self.assertEqual(_pick_bom_files(files), [])


class HeaderRowTests(unittest.TestCase):
    def test_detects_second_block_header(self):
        self.assertTrue(_is_header_row({"code": "Material Code", "part": "Material Description"}))
        self.assertTrue(_is_header_row({"code": "料號", "part": ""}))

    def test_real_material_row_kept(self):
        self.assertFalse(_is_header_row({"code": "54100101-10", "part": "Steel toe cap 1443"}))


class ReadSampleBomAssemblyTests(unittest.TestCase):
    def test_assembles_email_and_bom_sections(self):
        fake_files = [{"name": "5618 用量明細 Price BOM.xlsx", "id": "a", "modifiedTime": "2026-06-18"}]
        fake_mats = [
            {"part": "Steel toe cap 1443", "code": "54100101-10", "supplier": "Flecksteel",
             "unit": "PAR", "usage": 1, "unit_price": 0.9},
            {"part": "Material Description", "code": "Material Code", "supplier": "Vendor Name",
             "unit": "", "usage": None, "unit_price": None},  # 第二區塊表頭噪音，應被濾掉
        ]
        import agent_core.factory_bom as fb
        with mock.patch.object(ss, "read_sample_status", return_value="EMAIL_TIMELINE"), \
             mock.patch.object(ss, "_search_drive_by_name", return_value=fake_files), \
             mock.patch.object(fb, "_grab", return_value=b""), \
             mock.patch.object(fb, "_parse_jalas_bom", return_value=fake_mats):
            out = ss.read_sample_bom("JALAS 5618")
        self.assertIn("5618", out)
        self.assertIn("EMAIL_TIMELINE", out)            # 信件段有串進來
        self.assertIn("Steel toe cap 1443", out)        # 真料列在
        self.assertIn("1 項料", out)                     # 噪音行被濾掉 → 1 項而非 2
        self.assertNotIn("Material Description", out)    # 表頭噪音不出現

    def test_no_bom_files_still_returns_emails(self):
        with mock.patch.object(ss, "read_sample_status", return_value="EMAIL_TIMELINE"), \
             mock.patch.object(ss, "_search_drive_by_name", return_value=[]):
            out = ss.read_sample_bom("9999")
        self.assertIn("EMAIL_TIMELINE", out)
        self.assertIn("搜不到料表", out)

    def test_empty_input_guarded(self):
        self.assertIn("不能為空", ss.read_sample_bom("   "))


if __name__ == "__main__":
    unittest.main()
