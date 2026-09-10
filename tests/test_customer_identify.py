"""客戶辨識（agent_core/customer_identify）＋樣品單解析輸出客戶行的測試。

情境：Richter 的樣品單 5010-4291 被前台臆測成 Lurchi——修法是三個確定性訊號源
（單內文品牌字樣 / 款號查 email lake / 照片庫資料夾前綴），辨識不了要明講。
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from agent_core import customer_identify as ci


class FolderPrefixTests(unittest.TestCase):
    def test_known_prefixes(self):
        self.assertEqual(ci.customer_of_folder("R-JAJLCG ( Husky )女靴"), "Richter")
        self.assertEqual(ci.customer_of_folder("L-JEK154 #20 (小靴)"), "Lurchi")
        self.assertEqual(ci.customer_of_folder("B-JDG125 (BRTK)靴"), "BRTK")
        self.assertEqual(ci.customer_of_folder("K-JEV165(kamik)雪靴"), "Kamik")

    def test_unknown_or_missing_prefix(self):
        self.assertEqual(ci.customer_of_folder("涼鞋"), "")
        self.assertEqual(ci.customer_of_folder("X-未知客戶"), "")
        self.assertEqual(ci.customer_of_folder(""), "")

    def test_lowercase_prefix(self):
        self.assertEqual(ci.customer_of_folder("r-JES162"), "Richter")


class BrandsInTextTests(unittest.TestCase):
    def test_finds_brand_case_insensitive(self):
        self.assertEqual(ci.brands_in_text("MODELSPECIFICATION RICHTER AW2026"), ["Richter"])

    def test_deca_not_double_counted_inside_decathlon(self):
        self.assertEqual(ci.brands_in_text("for decathlon order"), ["Decathlon"])

    def test_no_partial_word_match(self):
        # "pax" 不能從其他單字裡撈出來
        self.assertEqual(ci.brands_in_text("paxton street"), [])

    def test_richtex_maps_to_richter_not_double_counted(self):
        # Richter logo 是圖時，「Richtex」（自家防水膜）常是文字層唯一品牌訊號（PSS 2 案）
        self.assertEqual(ci.brands_in_text("Davos GG 25-42 Richtex, PU PSS 2"), ["Richter"])
        # 與 richter 同見也只算一個候選，不會變成「多個候選」
        self.assertEqual(ci.brands_in_text("Richter AW27 Richtex membrane"), ["Richter"])

    def test_richtex_is_weak_hint_only(self):
        # 2026-09-08 review：richtex 是材質詞、別家客戶的單也會提到——
        # Lurchi 單寫 RICHTEX 內裡不可把客戶弄成「Richter / Lurchi」多候選
        self.assertEqual(ci.brands_in_text("LURCHI 5010-4291 RICHTEX membrane"), ["Lurchi"])

    def test_empty(self):
        self.assertEqual(ci.brands_in_text(""), [])


def _write_parquet(path, rows):
    import pandas as pd
    pd.DataFrame(rows).to_parquet(path, index=False)


class StyleLookupTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.internal = os.path.join(self.td.name, "emails.parquet")
        self.external = os.path.join(self.td.name, "emails_master.parquet")
        _write_parquet(self.internal, [
            {"date": "2025-11-15", "subject": "CBD OF RICHTER 5010 (2026)+5107 (2026)",
             "brands": '["Richter"]',
             "entities_json": json.dumps({"po_numbers": ["5010-4291"]}),
             "summary": "CBD 檔案已提供"},
            {"date": "2026-01-05", "subject": "LURCHI EG2305V 出貨",
             "brands": '["Lurchi"]',
             "entities_json": json.dumps({"po_numbers": ["EG2305V-02"]}),
             "summary": "出貨"},
        ])
        # 外部 lake 故意不存在 → 應被安靜跳過
        self.p1 = mock.patch.object(ci, "_INTERNAL_PARQUET", self.internal)
        self.p2 = mock.patch.object(ci, "_EXTERNAL_PARQUET", self.external)
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        self.td.cleanup()

    def test_style_hits_richter_with_evidence(self):
        cust, ev = ci.identify_customer_by_style(["5010-4291"])
        self.assertEqual(cust, "Richter")
        self.assertTrue(ev and "CBD OF RICHTER" in ev[0])

    def test_unknown_style_returns_empty(self):
        self.assertEqual(ci.identify_customer_by_style(["9999-0000"]), ("", []))

    def test_empty_styles(self):
        self.assertEqual(ci.identify_customer_by_style([]), ("", []))

    def test_identify_customer_prefers_sheet_text(self):
        ident = ci.identify_customer("SPEC FOR LURCHI AW2026", ["5010-4291"])
        self.assertEqual(ident["customer"], "Lurchi")
        self.assertEqual(ident["source"], "樣品單內文")

    def test_identify_customer_falls_back_to_lake(self):
        ident = ci.identify_customer("MODELSPECIFICATION AW2026 Husky2.0", ["5010-4291"])
        self.assertEqual(ident["customer"], "Richter")
        self.assertEqual(ident["source"], "email 紀錄")
        self.assertTrue(ident["evidence"])

    def test_identify_customer_unidentifiable(self):
        ident = ci.identify_customer("MODELSPECIFICATION AW2026", ["9999-0000"])
        self.assertEqual(ident["customer"], "")


class MissingFieldRowsTests(unittest.TestCase):
    """email lake 列常有缺欄（parquet null）。

    pandas 3 起 astype(str) 保留 NaN（不轉 "nan" 字串），款號 haystack 的逐列
    " ".join 會 TypeError——修法是 fillna("") 先行（#323 同型雷）。此測試在
    pandas 2 下也綠，鎖住兩版共同行為。
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.internal = os.path.join(self.td.name, "emails.parquet")
        self.external = os.path.join(self.td.name, "emails_master.parquet")
        _write_parquet(self.internal, [
            {"date": "2025-11-15", "subject": "CBD OF RICHTER 5010 (2026)",
             "brands": '["Richter"]', "entities_json": None, "summary": None},
            {"date": None, "subject": None, "brands": None,
             "entities_json": None, "summary": "無主旨的泛泛 email"},
        ])
        self.p1 = mock.patch.object(ci, "_INTERNAL_PARQUET", self.internal)
        self.p2 = mock.patch.object(ci, "_EXTERNAL_PARQUET", self.external)
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        self.td.cleanup()

    def test_style_lookup_survives_null_fields(self):
        cust, ev = ci.identify_customer_by_style(["5010"])
        self.assertEqual(cust, "Richter")
        self.assertTrue(ev and "CBD OF RICHTER" in ev[0])

    def test_all_null_row_does_not_match_or_crash(self):
        self.assertEqual(ci.identify_customer_by_style(["9999-0000"]), ("", []))


class ParseSampleOrderCustomerLineTests(unittest.TestCase):
    """parse_sample_order 端到端：mock Gemini、tmp xlsx、tmp parquet → 輸出要有客戶行。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.internal = os.path.join(self.td.name, "emails.parquet")
        _write_parquet(self.internal, [
            {"date": "2025-11-15", "subject": "CBD OF RICHTER 5010 (2026)+5107 (2026)",
             "brands": '["Richter"]',
             "entities_json": json.dumps({"po_numbers": ["5010-4291"]}),
             "summary": "CBD"},
        ])
        self.p1 = mock.patch.object(ci, "_INTERNAL_PARQUET", self.internal)
        self.p2 = mock.patch.object(ci, "_EXTERNAL_PARQUET",
                                    os.path.join(self.td.name, "no_such.parquet"))
        self.p1.start()
        self.p2.start()

        # 樣品單 xlsx：只有款號、沒印客戶名（重現 5010-4291 的實況）
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["MODELSPECIFICATION AW2026"])
        ws.append(["Group", "Husky2.0"])
        ws.append(["Pattern", "5010-4291", "Color", "6511"])
        self.xlsx = os.path.join(self.td.name, "5010-4291.xlsx")
        wb.save(self.xlsx)

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        self.td.cleanup()

    def _mock_gemini(self, payload: str):
        fake = mock.MagicMock()
        fake.models.generate_content.return_value = mock.MagicMock(text=payload)
        return mock.patch("agent_core.gemini_client._get_gemini_client", return_value=fake)

    def test_customer_line_from_lake(self):
        import skills.sample_order as so
        payload = json.dumps([{"款號": "5010-4291", "顏色": "6511", "部位規格": "碳灰豹紋麂皮"}])
        with self._mock_gemini(payload):
            out = so.parse_sample_order(self.xlsx)
        self.assertIn("客戶：Richter", out)
        self.assertIn("CBD OF RICHTER", out)

    def test_unidentifiable_says_do_not_guess(self):
        import skills.sample_order as so
        payload = json.dumps([{"款號": "9999-0000", "顏色": "0000", "部位規格": "x"}])
        with self._mock_gemini(payload):
            out = so.parse_sample_order(self.xlsx)
        self.assertIn("無法辨識", out)
        self.assertIn("請勿臆測", out)

    def test_raw_fallback_still_has_customer_line(self):
        import skills.sample_order as so
        with self._mock_gemini("這不是 JSON"):
            out = so.parse_sample_order(self.xlsx)
        self.assertIn("客戶：", out)


if __name__ == "__main__":
    unittest.main()
