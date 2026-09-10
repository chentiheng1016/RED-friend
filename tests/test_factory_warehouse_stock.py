"""倉庫庫存料表工具（agent_core.factory_warehouse_stock）的密封測試。

不碰 live Drive：用 openpyxl 合成一份「Data 主檔 + 每料一分頁(含結餘)」的 workbook
bytes，驗證解析/彙總邏輯。重點防回歸：
  - 現有量以「各料分頁結餘」為權威，**勝過/補上** Data 主檔的 Stock 欄
    （底料主檔幾乎全空，盲讀主檔會回錯——本工具存在的理由）。
  - 主檔 Stock 空、且分頁也無結餘 → fallback 主檔；都無 → 該料不亂編。
  - 架位取 Place(Shelf No.) 而非 Production Place（兩者都含 'place'）。
"""
import io
import json
import unittest
from unittest import mock

import openpyxl

from agent_core import factory_warehouse_stock as fws


def _build_workbook_bytes():
    """合成料表 workbook bytes，結構仿真實檔。"""
    wb = openpyxl.Workbook()
    data = wb.active
    data.title = "Data"
    data.append(["材料類別 (TS)"])  # 類別 banner（第 1 列）
    data.append(["System No.", "Stock No.", "Product Name", "Unit", "Vietnamese",
                 "Color", "Supplier", "Production Place", "Price", "Currency",
                 "Remark 1", "Stock", "Place (Shelf No.)", "Minimum Amout"])  # 表頭（第 2 列）
    # 主檔 Stock 欄(索引 11) 留空 → 必須靠 TS01 分頁結餘
    data.append([None, "TS01", "Test Glue A", "KG", None, None, "ACME", "CN",
                 10, "USD", None, None, "Shelf-1", 5])
    # 主檔 Stock=7，但 TS02 分頁結餘=3 → 應以分頁(3)為準
    data.append([None, "TS02", "Test Glue B", "KG", None, None, "ACME", "CN",
                 20, "USD", None, 7, "Shelf-2", 10])
    # 主檔 Stock=42，TS05 分頁無結餘 → fallback 主檔 42
    data.append([None, "TS05", "Test Powder", "KG", None, None, "ACME", "VN",
                 5, "USD", None, 42, "Shelf-5", None])
    # 只在主檔、無對應分頁 → 不應出現（工具以料分頁為主鍵）
    data.append([None, "TS03", "Ledgerless", None, None, None, None, None,
                 None, None, None, None, None, None])

    ts01 = wb.create_sheet("TS01")
    ts01.append(["TS01", None, None, 100, None, 250])           # 值列
    ts01.append(["desc", None, None, "Stock", None, "Forecast Stock"])  # 標籤列
    ts02 = wb.create_sheet("TS02")
    ts02.append(["TS02", None, 3, None])
    ts02.append(["desc", None, "Stock", None])
    ts05 = wb.create_sheet("TS05")            # 無結餘區塊 → fallback 主檔
    ts05.append(["TS05", None, None])
    ts05.append(["desc", None, None])
    ts04 = wb.create_sheet("TS04")            # 空殼、且不在主檔 → 應略過
    ts04.append(["TS04", None, None])
    ts04.append(["desc", None, None])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class WarehouseStockParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.xlsx = _build_workbook_bytes()

    def _rows(self, material=""):
        rows, _warns, _trunc = fws._parse_stock_workbook(self.xlsx, material=material)
        return {r["stock_no"]: r for r in rows}

    def test_sheet_balance_beats_blank_master(self):
        rows = self._rows()
        self.assertIn("TS01", rows)
        self.assertEqual(rows["TS01"]["current"], 100)   # 分頁結餘（主檔空）
        self.assertEqual(rows["TS01"]["forecast"], 250)

    def test_sheet_balance_overrides_master_value(self):
        rows = self._rows()
        self.assertEqual(rows["TS02"]["current"], 3)     # 分頁(3) 勝過主檔(7)

    def test_fallback_to_master_when_no_sheet_balance(self):
        rows = self._rows()
        self.assertEqual(rows["TS05"]["current"], 42)    # 無結餘 → 主檔 42

    def test_ledgerless_material_absent(self):
        self.assertNotIn("TS03", self._rows())           # 只在主檔、無分頁 → 不出現

    def test_empty_placeholder_skipped(self):
        self.assertNotIn("TS04", self._rows())           # 空殼分頁略過

    def test_shelf_is_not_production_place(self):
        self.assertEqual(self._rows()["TS01"]["shelf"], "Shelf-1")  # 非 'CN'(產地)

    def test_material_filter(self):
        self.assertEqual(set(self._rows(material="TS02")), {"TS02"})

    def test_low_stock_flag_in_render(self):
        rows = self._rows()
        self.assertIn("⚠️", fws._trailing(rows["TS02"]))     # 3 < 安全量 10
        self.assertIn("🟡", fws._trailing(rows["TS02"]))     # 低於安全量燈號
        self.assertNotIn("⚠️", fws._trailing(rows["TS01"]))  # 100 > 安全量 5
        self.assertIn("🟢", fws._trailing(rows["TS01"]))     # 正常燈號

    def test_aligned_table_columns_line_up(self):
        rows = self._rows()
        table = fws._render_aligned_table([rows["TS01"], rows["TS02"]])
        # ``` 圍欄包住，tg_send 才會轉 <pre> 等寬
        self.assertEqual(table[0], "```")
        self.assertEqual(table[-1], "```")
        body = table[1:-1]  # 表頭 + 資料列
        # 純 ASCII 對齊欄（料號/現有/在途）對齊 → 每列「品名／供應商」前的等寬段同寬
        prefixes = [ln.split("🟢")[0].split("🟡")[0].split("🔴")[0] for ln in body[1:]]
        self.assertEqual(len(set(len(p) for p in prefixes)), 1)
        # TS01 現有=100、TS02 現有=3 都在表內
        joined = "\n".join(body)
        self.assertIn("100", joined)
        self.assertIn("TS01", joined)

    def test_incoming_is_forecast_minus_current(self):
        rows = self._rows()
        # TS01 現有100 預計250 → 在途 150
        self.assertEqual(fws._incoming(rows["TS01"]), 150)
        # TS05 無預計 → None
        self.assertIsNone(fws._incoming(rows["TS05"]))


class WarehouseStockToolTests(unittest.TestCase):
    def test_read_warehouse_stock_with_file_id(self):
        xlsx = _build_workbook_bytes()
        # file_id 路徑繞過 Drive 搜尋；只需 mock service + 下載。
        with mock.patch.object(fws, "_drive_service", return_value=object()), \
             mock.patch.object(fws, "_download_bytes",
                               return_value=(xlsx, {"name": "TEST (TS).xlsx"})):
            out = fws.read_warehouse_stock(file_id="dummy")
        self.assertIn("TS01", out)
        self.assertIn("100", out)
        self.assertIn("TEST (TS).xlsx", out)   # 標明來源檔
        self.assertIn("⚠️", out)               # TS02 低於安全量
        self.assertNotIn("TS04", out)          # 空殼略過

    def test_discovery_lists_categories(self):
        files = [{"id": "a", "name": "4化學 - Chemistry 3 (GL) (膠水 keo).xlsx",
                  "code": "GL", "category": "4化學 - Chemistry"}]
        with mock.patch.object(fws, "_drive_service", return_value=object()), \
             mock.patch.object(fws, "_find_stock_root", return_value=("root", "00 Stock Data")), \
             mock.patch.object(fws, "_list_stock_files", return_value=files):
            out = fws.read_warehouse_stock()
        self.assertIn("膠水", out)
        self.assertIn("GL", out)

    def test_material_name_without_category_says_so(self):
        # 純品名關鍵字（非料號樣式）＋無 category → 明說要搭配 category、附料類清單，
        # 別默默回料類清單誤導成「查無此料」（docstring 曾宣稱可用品名查）。
        files = [{"id": "a", "name": "4化學 - Chemistry 3 (GL) (膠水 keo).xlsx",
                  "code": "GL", "category": "4化學 - Chemistry"}]
        with mock.patch.object(fws, "_drive_service", return_value=object()), \
             mock.patch.object(fws, "_find_stock_root", return_value=("root", "00 Stock Data")), \
             mock.patch.object(fws, "_list_stock_files", return_value=files):
            out = fws.read_warehouse_stock(material="黃膠")
        self.assertIn("需搭配 category", out)
        self.assertIn("膠水", out)              # 附料類清單幫人挑
        self.assertIn("'黃膠'", out)

    def test_material_stock_code_still_autolocates(self):
        # 料號樣式（GL02）不受品名指引影響，照舊自動定位
        xlsx = _build_workbook_bytes()
        files = [{"id": "a", "name": "TEST (TS).xlsx", "code": "TS", "category": "測試"}]
        with mock.patch.object(fws, "_drive_service", return_value=object()), \
             mock.patch.object(fws, "_find_stock_root", return_value=("root", "00 Stock Data")), \
             mock.patch.object(fws, "_list_stock_files", return_value=files), \
             mock.patch.object(fws, "_download_bytes",
                               return_value=(xlsx, {"name": "TEST (TS).xlsx"})):
            out = fws.read_warehouse_stock(material="TS02")
        self.assertIn("TS02", out)
        self.assertNotIn("需搭配 category", out)

    def test_export_excel_routes_to_export_report(self):
        """export='excel' → 把結構化 rows 餵 export_report、回其摘要、不回文字表。"""
        xlsx = _build_workbook_bytes()
        captured = {}

        def fake_export(content_json, **kw):
            captured["content_json"] = content_json
            captured["kw"] = kw
            return "✅ 已產出 1 個檔案：倉庫庫存_TS.xlsx（已傳 Telegram）"

        with mock.patch.object(fws, "_drive_service", return_value=object()), \
             mock.patch.object(fws, "_download_bytes",
                               return_value=(xlsx, {"name": "TEST (TS).xlsx"})), \
             mock.patch("agent_core.doc_export.export_report",
                        side_effect=fake_export) as m:
            out = fws.read_warehouse_stock(file_id="dummy", export="excel")

        self.assertEqual(m.call_count, 1)
        self.assertIn("已產出", out)
        self.assertNotIn("```", out)          # 不是等寬文字表
        # 傳給 export_report 的 spec：欄位完整、table 型、含解析出的料號
        spec = json.loads(captured["content_json"])
        block = spec["blocks"][0]
        self.assertEqual(block["type"], "table")
        self.assertEqual(block["columns"], fws._EXPORT_COLUMNS)
        self.assertIn("TS01", {r[0] for r in block["rows"]})
        self.assertEqual(captured["kw"].get("formats"), "excel")
        self.assertTrue(captured["kw"].get("deliver"))

    def test_no_export_keeps_text_table(self):
        """不帶 export → 維持原行為（等寬 ``` 圍欄表），且不呼叫 export_report。"""
        xlsx = _build_workbook_bytes()
        with mock.patch.object(fws, "_drive_service", return_value=object()), \
             mock.patch.object(fws, "_download_bytes",
                               return_value=(xlsx, {"name": "TEST (TS).xlsx"})), \
             mock.patch("agent_core.doc_export.export_report") as m:
            out = fws.read_warehouse_stock(file_id="dummy")
        m.assert_not_called()
        self.assertIn("```", out)


class WarehouseExportSpecTests(unittest.TestCase):
    """_build_export_spec 純函式（不碰 Drive/檔案）。"""

    def _rows(self):
        return [
            {"stock_no": "GL01", "name": "黃膠 FJ-678", "unit": "KG",
             "supplier": "福積", "shelf": "A1", "min": 90.0, "currency": "TWD",
             "price": 120.0, "current": 0.0, "forecast": 2160.0, "_file": "GL.xlsx"},
            {"stock_no": "GL02", "name": "PU膠", "unit": "KG", "supplier": "福積",
             "shelf": "A2", "min": 555.0, "currency": "TWD", "price": None,
             "current": 90.0, "forecast": 555.0, "_file": "GL.xlsx"},
            {"stock_no": "GL30", "name": "活性胺", "unit": "KG", "supplier": "合力",
             "shelf": "B1", "min": 0.0, "currency": "", "price": None,
             "current": 840.0, "forecast": 840.0, "_file": "GL.xlsx"},
        ]

    def _cell(self, spec, code, col):
        cols = fws._EXPORT_COLUMNS
        by_code = {r[0]: r for r in spec["blocks"][0]["rows"]}
        return by_code[code][cols.index(col)]

    def test_columns_and_count(self):
        spec = fws._build_export_spec(self._rows(), title="T", used_files=["GL.xlsx"])
        block = spec["blocks"][0]
        self.assertEqual(block["type"], "table")
        self.assertEqual(block["columns"], fws._EXPORT_COLUMNS)
        self.assertEqual(len(block["rows"]), 3)

    def test_incoming_status_and_int_coercion(self):
        spec = fws._build_export_spec(self._rows(), title="T", used_files=["GL.xlsx"])
        # 在途 = 預計 − 現有；整數值不留 .0
        self.assertEqual(self._cell(spec, "GL01", "在途量"), 2160)
        self.assertIsInstance(self._cell(spec, "GL01", "在途量"), int)
        self.assertEqual(self._cell(spec, "GL02", "在途量"), 465)
        self.assertEqual(self._cell(spec, "GL30", "在途量"), 0)   # 預計==現有
        # 狀態文字（確定性）
        self.assertEqual(self._cell(spec, "GL01", "狀態"), "斷料")
        self.assertEqual(self._cell(spec, "GL02", "狀態"), "低於安全量")
        self.assertEqual(self._cell(spec, "GL30", "狀態"), "正常")
        # None 數字 → 空字串
        self.assertEqual(self._cell(spec, "GL02", "單價"), "")

    def test_subtitle_dedups_source_and_has_asof(self):
        spec = fws._build_export_spec(self._rows(), title="T",
                                      used_files=["GL.xlsx", "GL.xlsx"],
                                      as_of="2026-06-23")
        self.assertIn("2026-06-23", spec["subtitle"])
        self.assertEqual(spec["subtitle"].count("GL.xlsx"), 1)   # 去重

    def test_status_label_emoji_same_source(self):
        for cur, mn in [(0, 5), (-1, None), (3, 10), (100, 5), (None, 5), (5, None)]:
            self.assertEqual(fws._status_emoji(cur, mn),
                             fws._STATUS_EMOJI[fws._status_label(cur, mn)])

    def test_unknown_stock_is_gray_not_green(self):
        # 現有量解析不到（cur=None）是資料缺口，不能標 🟢 正常
        self.assertEqual(fws._status_label(None, 5), "未知")
        self.assertEqual(fws._status_label(None, None), "未知")
        self.assertEqual(fws._status_emoji(None, 5), "⚪")

    def test_export_spec_unknown_status_row(self):
        rows = [{"stock_no": "GL99", "name": "神秘料", "unit": "KG", "supplier": "",
                 "shelf": "", "min": 5.0, "currency": "", "price": None,
                 "current": None, "forecast": None, "_file": "GL.xlsx"}]
        spec = fws._build_export_spec(rows, title="T", used_files=["GL.xlsx"])
        cols = fws._EXPORT_COLUMNS
        row = spec["blocks"][0]["rows"][0]
        self.assertEqual(row[cols.index("狀態")], "未知")   # Excel 匯出同步

    def test_trailing_unknown_shows_gray_dot(self):
        r = {"stock_no": "GL99", "name": "神秘料", "unit": "KG", "supplier": "",
             "shelf": "", "min": 5.0, "currency": "", "price": None,
             "current": None, "forecast": None}
        self.assertIn("⚪", fws._trailing(r))
        self.assertNotIn("🟢", fws._trailing(r))


if __name__ == "__main__":
    unittest.main()
