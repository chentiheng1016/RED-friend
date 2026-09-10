"""uploaded_docs：上傳的 Excel/CSV 整張讀、PDF 逐頁原文（員工通道）。

案由 2026-08-17 UserAng Richter 案：員工通道沒有任何「把這份檔讀完」的工具，
於是母表 19 列產出 18 列（掉的正是唯一沒有圖的那列）、12 欄剩 7 欄。這裡的
測試盯的就是那幾個具體失效點：

  * 沒有圖的列一定要出現在輸出裡（不能只看得到有圖的列）。
  * 所有欄位都要在（不能只剩前幾欄）。
  * 列號要是**原表的列號**，才對得回抽圖工具回報的 F11 這種儲存格。
  * 截斷一定要講 —— 靜默截斷就是這個案子的病灶本身。
  * 路徑閘同 analyze_uploaded_image：只讀 Telegram 上傳目錄。

隔離：檔案都在 tempdir，RED_TELEGRAM_UPLOAD_DIR 在 setUp 換掉、tearDown 還原
（不碰主 checkout 的 var/）。不 hardcode /Users 路徑。
"""
import os
import shutil
import tempfile
import unittest

from agent_core import uploaded_docs


def _make_xlsx(path: str, rows: list[list], sheet: str = "AW27",
               extra_sheet: str = "") -> str:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(row)
    if extra_sheet:
        wb.create_sheet(extra_sheet)["A1"] = "第二張表"
    wb.save(path)
    return path


def _make_pdf(path: str, pages: list[str]) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path, pagesize=A4)
    for text in pages:
        c.drawString(72, A4[1] - 100, text)
        c.showPage()
    c.save()
    return path


class UploadedDocsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="red_upldocs_test_")
        self.upload_root = os.path.join(self._tmp, "uploads")
        self.day = os.path.join(self.upload_root, "2026-08-17")
        os.makedirs(self.day, exist_ok=True)
        self._env = os.environ.get("RED_TELEGRAM_UPLOAD_DIR")
        os.environ["RED_TELEGRAM_UPLOAD_DIR"] = self.upload_root

    def tearDown(self):
        if self._env is None:
            os.environ.pop("RED_TELEGRAM_UPLOAD_DIR", None)
        else:
            os.environ["RED_TELEGRAM_UPLOAD_DIR"] = self._env
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _master(self, name: str = "master.xlsx") -> str:
        """母表縮影：第 4 列 remarks 放文字沒放圖（＝ UserAng 那列 7904）。"""
        return _make_xlsx(os.path.join(self.day, name), [
            ["group", "article number", "color number", "pair", "Material",
             "remarks", "dispatch date", "tracking number"],
            ["Freestyle", "2001A", "Magic forest viola", 0.5, "Micro suede / PU",
             "", "", ""],
            ["Husky 2.0", "5001", "vulcano", 0.5, "Richtex / PU", "", "", ""],
            ["Davos", "7904", "Unspecified", 0.5, "Richtex / PU",
             "Davos winter style", "", ""],
            ["PSS", "PSS 3", "黑色 / 湖水綠", 0.5, "Richtex / PU (SR2310026)",
             "", "", ""],
        ], extra_sheet="notes")


class TestReadUploadedTable(UploadedDocsBase):
    def test_reads_every_row_including_the_one_without_a_photo(self):
        out = uploaded_docs.read_uploaded_table(self._master())
        for style in ("2001A", "5001", "7904", "PSS 3"):
            self.assertIn(style, out)
        self.assertIn("Davos winter style", out)   # 抽圖工具看不到的那列

    def test_keeps_every_column_not_just_the_first_few(self):
        """抽圖工具的列標籤只取前 4 格；這顆是整列，材質/日期欄不能掉。"""
        out = uploaded_docs.read_uploaded_table(self._master())
        self.assertIn("SR2310026", out)            # 第 5 欄（材質）
        self.assertIn("tracking number", out)      # 第 8 欄（表頭）

    def test_row_numbers_match_the_original_sheet(self):
        out = uploaded_docs.read_uploaded_table(self._master())
        self.assertIn("r4: Davos | 7904", out)     # 對得回抽圖工具的儲存格列號

    def test_reports_row_count_and_says_nothing_was_omitted(self):
        out = uploaded_docs.read_uploaded_table(self._master())
        self.assertIn("5 列有資料", out)
        self.assertIn("沒有省略", out)

    def test_truncation_is_announced_not_silent(self):
        out = uploaded_docs.read_uploaded_table(self._master(), max_rows=2)
        aw27 = out.split("── 工作表「notes」")[0]
        self.assertIn("還有 3 列沒列出", aw27)
        self.assertIn("不要", aw27)
        self.assertNotIn("沒有省略", aw27)   # 截斷的那張表不可以宣稱完整

    def test_lists_all_sheets_and_can_target_one(self):
        path = self._master()
        self.assertIn("notes", uploaded_docs.read_uploaded_table(path))
        only = uploaded_docs.read_uploaded_table(path, sheet="notes")
        self.assertIn("第二張表", only)
        self.assertNotIn("Magic forest viola", only)

    def test_unknown_sheet_name_says_what_sheets_exist(self):
        out = uploaded_docs.read_uploaded_table(self._master(), sheet="nope")
        self.assertIn("找不到工作表", out)
        self.assertIn("AW27", out)

    def test_blank_rows_do_not_renumber_the_rest(self):
        path = _make_xlsx(os.path.join(self.day, "gaps.xlsx"), [
            ["h1", "h2"], [None, None], ["a", 1], [None, None], ["b", 2],
        ])
        out = uploaded_docs.read_uploaded_table(path)
        self.assertIn("r3: a | 1", out)
        self.assertIn("r5: b | 2", out)

    def test_dates_lose_the_meaningless_midnight_component(self):
        import datetime
        path = _make_xlsx(os.path.join(self.day, "dates.xlsx"), [
            ["target"], [datetime.datetime(2026, 9, 5, 0, 0, 0)],
        ])
        out = uploaded_docs.read_uploaded_table(path)
        self.assertIn("2026-09-05", out)
        self.assertNotIn("00:00:00", out)

    def test_long_cell_is_clipped_with_a_marker(self):
        path = _make_xlsx(os.path.join(self.day, "long.xlsx"), [
            ["note"], ["備" * 400],
        ])
        out = uploaded_docs.read_uploaded_table(path)
        self.assertIn("(截斷)", out)

    def test_in_cell_images_are_marked_not_shown_as_value_error(self):
        """「置於儲存格」的圖用 openpyxl 讀值是 #VALUE!，要標成圖不是資料錯誤。"""
        from tests.test_doc_images import _make_incell_xlsx
        src = _make_incell_xlsx(self._tmp)
        path = os.path.join(self.day, "incell.xlsx")
        shutil.copy(src, path)
        out = uploaded_docs.read_uploaded_table(path)
        self.assertIn("🖼️", out)

    def test_csv_is_read_whole(self):
        path = os.path.join(self.day, "list.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("款號,顏色\n2001A,viola\n7904,unspecified\n")
        out = uploaded_docs.read_uploaded_table(path)
        self.assertIn("2001A", out)
        self.assertIn("7904", out)
        self.assertIn("沒有省略", out)

    def test_cp950_csv_does_not_blow_up(self):
        path = os.path.join(self.day, "big5.csv")
        with open(path, "w", encoding="cp950") as f:
            f.write("款號,顏色\n2001A,紫色\n")
        out = uploaded_docs.read_uploaded_table(path)
        self.assertIn("紫色", out)

    # ── 路徑閘（同 analyze_uploaded_image） ────────────────────────
    def test_rejects_path_outside_upload_root(self):
        outside = _make_xlsx(os.path.join(self._tmp, "outside.xlsx"), [["a"]])
        out = uploaded_docs.read_uploaded_table(outside)
        self.assertTrue(out.startswith("錯誤："), out)
        self.assertIn("上傳目錄", out)

    def test_rejects_traversal_out_of_upload_root(self):
        _make_xlsx(os.path.join(self._tmp, "outside.xlsx"), [["a"]])
        sneaky = os.path.join(self.upload_root, "..", "outside.xlsx")
        self.assertTrue(uploaded_docs.read_uploaded_table(sneaky).startswith("錯誤："))

    def test_rejects_unsupported_extension(self):
        note = os.path.join(self.day, "note.txt")
        with open(note, "w", encoding="utf-8") as f:
            f.write("hi")
        self.assertIn("只吃 Excel/CSV", uploaded_docs.read_uploaded_table(note))

    def test_missing_file_is_reported_not_raised(self):
        out = uploaded_docs.read_uploaded_table(os.path.join(self.day, "nope.xlsx"))
        self.assertIn("找不到檔案", out)

    def test_corrupt_workbook_returns_error_string(self):
        broken = os.path.join(self.day, "broken.xlsx")
        with open(broken, "wb") as f:
            f.write(b"not really a workbook")
        out = uploaded_docs.read_uploaded_table(broken)
        self.assertTrue(out.startswith("❌"), out)


class TestReadUploadedPdfText(UploadedDocsBase):
    def test_reads_every_page_verbatim(self):
        path = _make_pdf(os.path.join(self.day, "PSS_6.pdf"),
                         ["Husky2.0 Richtex, PU PSS 6", "denim PSS 6 Zip inside"])
        out = uploaded_docs.read_uploaded_pdf_text(path)
        self.assertIn("第 1 頁", out)
        self.assertIn("第 2 頁", out)
        self.assertIn("denim", out)        # 真配色；先前被編成「Standard Spec」

    def test_page_range_says_how_much_was_left_unread(self):
        path = _make_pdf(os.path.join(self.day, "spec.pdf"),
                         ["page one", "page two", "page three"])
        out = uploaded_docs.read_uploaded_pdf_text(path, pages="1")
        self.assertIn("page one", out)
        self.assertNotIn("page two", out)
        self.assertIn("1/3 頁", out)

    def test_scanned_pdf_says_it_cannot_read_instead_of_guessing(self):
        path = _make_pdf(os.path.join(self.day, "blank.pdf"), [""])
        out = uploaded_docs.read_uploaded_pdf_text(path)
        self.assertIn("抽不到任何文字", out)
        self.assertIn("不可以憑檔名", out)

    def test_partial_empty_pages_are_flagged(self):
        path = _make_pdf(os.path.join(self.day, "mixed.pdf"), ["real text", ""])
        out = uploaded_docs.read_uploaded_pdf_text(path)
        self.assertIn("real text", out)
        self.assertIn("第 2 頁抽不到文字", out)

    def test_rejects_non_pdf(self):
        path = _make_xlsx(os.path.join(self.day, "sheet.xlsx"), [["a"]])
        self.assertIn("只吃 PDF", uploaded_docs.read_uploaded_pdf_text(path))

    def test_rejects_path_outside_upload_root(self):
        outside = _make_pdf(os.path.join(self._tmp, "outside.pdf"), ["secret"])
        out = uploaded_docs.read_uploaded_pdf_text(outside)
        self.assertTrue(out.startswith("錯誤："), out)


class TestEmployeeReachability(unittest.TestCase):
    """白名單是唯一閘門：prompt 點名的工具沒進去就靜默失效（#350/#354/#356）。"""

    def test_both_readers_are_safe_tier(self):
        from agent_core.tool_tiers import TIER_SAFE, get_tier
        for name in ("read_uploaded_table", "read_uploaded_pdf_text"):
            self.assertEqual(get_tier(name), TIER_SAFE, name)

    def test_every_color_can_reach_both_readers(self):
        from agent_core.agents.permission_matrix import Agent
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        for agent in Agent:
            if agent.value == "red":
                continue
            allowed = allowed_tool_names_for_color(agent.value)
            self.assertIn("read_uploaded_table", allowed, agent.value)
            self.assertIn("read_uploaded_pdf_text", allowed, agent.value)

    def test_prompt_addendum_names_only_reachable_tools(self):
        from agent_core.dept_tool_scope import (
            allowed_tool_names_for_color, dept_scope_addendum,
        )
        text = dept_scope_addendum("orange")
        allowed = allowed_tool_names_for_color("orange")
        for name in ("read_uploaded_table", "read_uploaded_pdf_text",
                     "extract_uploaded_excel_images", "parse_sample_order"):
            self.assertIn(name, text, f"prompt 沒點名 {name}")
            self.assertIn(name, allowed, f"{name} 在 prompt 但不在白名單＝靜默失效")

    def test_registry_exports_both_readers(self):
        from agent_core import tool_registry_catalog as cat
        names = {getattr(fn, "__name__", "") for fn in cat.BASE_BUILTIN_TOOLS}
        self.assertIn("read_uploaded_table", names)
        self.assertIn("read_uploaded_pdf_text", names)


if __name__ == "__main__":
    unittest.main()
