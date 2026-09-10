"""chart_export 圖表產生 tool 的測試。

涵蓋：SAFE tier（不需 +確認/+雙確認，這正是大王要的）、型別別名正規化、各型別
渲染出合法 PNG、甘特圖日期/數字軸、雙軸組合圖、錯誤路徑、Telegram 交付 wiring。

隔離：用 tempdir monkeypatch chart_export.EXPORTS_DIR（不碰真的 var/data/exports），
全部 deliver=False 或 patch 掉 telegram_send_photo，不真的連 Telegram。
不 hardcode 任何 /Users/... 路徑。
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from agent_core import chart_export
from agent_core.chart_export import generate_chart

# PNG 檔頭魔術數字 —— 確認真的產出圖檔而不只是空檔
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class ChartExportBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="red_chart_test_")
        self._patch = mock.patch.object(chart_export, "EXPORTS_DIR", self._tmp)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _dumps(self, obj):
        return json.dumps(obj, ensure_ascii=False)

    def _render(self, spec):
        """畫一張圖（不傳 Telegram），回 (ToolResult, png_path)。"""
        res = generate_chart(self._dumps(spec), deliver=False)
        path = (res.data or {}).get("path") if res.ok else None
        return res, path

    def _assert_valid_png(self, res, path):
        self.assertTrue(res.ok, f"預期成功，卻失敗：{res}")
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path), f"PNG 不存在：{path}")
        with open(path, "rb") as f:
            head = f.read(8)
        self.assertEqual(head, _PNG_MAGIC, "產出的不是合法 PNG")
        self.assertGreater(os.path.getsize(path), 2000, "PNG 過小，疑似空圖")
        self.assertEqual(res.artifacts, [path])


# ────────────────────────────────────────────────────────────────────
# 安全層：這是大王最在意的 —— 畫圖不該要 c / cc 確認
# ────────────────────────────────────────────────────────────────────
class TestTierIsSafe(unittest.TestCase):
    def test_not_sensitive_no_confirmation_needed(self):
        from agent_core.tg_auth import is_sensitive
        self.assertFalse(is_sensitive("generate_chart"),
                         "generate_chart 不該是 sensitive（否則又要 +確認）")

    def test_tier_is_safe(self):
        from agent_core.tool_tiers import get_tier, TIER_SAFE
        self.assertEqual(get_tier("generate_chart"), TIER_SAFE)

    def test_registered_in_catalog(self):
        from agent_core import tool_registry_catalog as cat
        names = {getattr(f, "__name__", "") for f in cat.BASE_BUILTIN_TOOLS}
        self.assertIn("generate_chart", names)


# ────────────────────────────────────────────────────────────────────
# 型別別名正規化
# ────────────────────────────────────────────────────────────────────
class TestTypeNormalization(unittest.TestCase):
    def test_chinese_and_english_aliases(self):
        cases = {
            "甘特圖": "gantt", "gantt": "gantt", "排程圖": "gantt",
            "長條圖": "grouped_bar", "bar": "grouped_bar", "柱狀圖": "grouped_bar",
            "堆疊長條圖": "stacked_bar", "stacked": "stacked_bar",
            "折線圖": "line", "line": "line", "趨勢圖": "line",
            "圓餅圖": "pie", "pie": "pie",
        }
        for raw, expected in cases.items():
            self.assertEqual(chart_export._normalize_type(raw), expected, f"{raw} 應正規化成 {expected}")

    def test_unknown_type_returns_none(self):
        self.assertIsNone(chart_export._normalize_type("熱力圖"))
        self.assertIsNone(chart_export._normalize_type(""))
        self.assertIsNone(chart_export._normalize_type(None))


# ────────────────────────────────────────────────────────────────────
# 各型別渲染
# ────────────────────────────────────────────────────────────────────
class TestRendering(ChartExportBase):
    def test_gantt_with_dates_and_annotations(self):
        spec = {"type": "甘特圖", "title": "6月生產線甘特圖", "xlabel": "日期",
                "tasks": [
                    {"label": "迪卡儂", "start": "2026-06-01", "end": "2026-06-14", "annotation": "累計 12000 雙"},
                    {"label": "Jalas", "start": "2026-06-02", "end": "2026-06-02", "annotation": "單日趕工"},
                ]}
        self._assert_valid_png(*self._render(spec))

    def test_gantt_with_numeric_axis(self):
        spec = {"type": "gantt", "title": "工序", "tasks": [
            {"label": "裁斷", "start": 0, "end": 3},
            {"label": "車縫", "start": 2, "end": 7}]}
        self._assert_valid_png(*self._render(spec))

    def test_stacked_bar_with_secondary_axis_combo(self):
        spec = {"type": "堆疊長條圖", "title": "產量×效率",
                "categories": ["6/1", "6/2", "6/3"], "ylabel": "產量", "ylabel2": "效率%",
                "series": [
                    {"name": "迪卡儂", "values": [120, 150, 90]},
                    {"name": "Lurchi", "values": [80, 60, 110]},
                    {"name": "效率%", "values": [88, 91, 75], "kind": "line", "axis": "secondary"}]}
        self._assert_valid_png(*self._render(spec))

    def test_grouped_bar(self):
        spec = {"type": "bar", "title": "群組", "categories": ["A", "B", "C"],
                "series": [{"name": "X", "values": [1, 2, 3]}, {"name": "Y", "values": [3, 2, 1]}]}
        self._assert_valid_png(*self._render(spec))

    def test_line_shorthand_values(self):
        # 單序列簡寫：頂層直接給 values
        spec = {"type": "折線圖", "title": "趨勢", "categories": ["6/1", "6/2", "6/3"],
                "values": [88, 91, 75]}
        self._assert_valid_png(*self._render(spec))

    def test_pie(self):
        spec = {"type": "圓餅圖", "title": "占比", "labels": ["甲", "乙", "丙"], "values": [45, 30, 25]}
        self._assert_valid_png(*self._render(spec))

    def test_line_without_categories_autonumbers(self):
        spec = {"type": "line", "values": [1, 4, 9, 16]}
        self._assert_valid_png(*self._render(spec))


# ────────────────────────────────────────────────────────────────────
# 錯誤路徑 —— 要回可讀錯誤、不可崩
# ────────────────────────────────────────────────────────────────────
class TestErrorPaths(ChartExportBase):
    def test_bad_json(self):
        res = generate_chart("{not json", deliver=False)
        self.assertFalse(res.ok)
        self.assertIn("JSON", str(res))

    def test_unknown_type(self):
        res = generate_chart(self._dumps({"type": "熱力圖", "values": [1, 2]}), deliver=False)
        self.assertFalse(res.ok)
        self.assertIn("型別", str(res))

    def test_gantt_missing_tasks(self):
        res = generate_chart(self._dumps({"type": "gantt", "title": "空"}), deliver=False)
        self.assertFalse(res.ok)
        self.assertTrue(res.recoverable)

    def test_gantt_mixed_date_and_number_rejected(self):
        spec = {"type": "gantt", "tasks": [
            {"label": "a", "start": "2026-06-01", "end": 5}]}
        res = generate_chart(self._dumps(spec), deliver=False)
        self.assertFalse(res.ok)

    def test_series_length_mismatch(self):
        spec = {"type": "bar", "categories": ["a", "b", "c"],
                "series": [{"name": "x", "values": [1, 2]}]}
        res = generate_chart(self._dumps(spec), deliver=False)
        self.assertFalse(res.ok)
        self.assertIn("一致", str(res))

    def test_non_numeric_values(self):
        spec = {"type": "line", "categories": ["a", "b"],
                "series": [{"name": "x", "values": [1, "oops"]}]}
        res = generate_chart(self._dumps(spec), deliver=False)
        self.assertFalse(res.ok)


# ────────────────────────────────────────────────────────────────────
# Telegram 交付 wiring（不真的連線）
# ────────────────────────────────────────────────────────────────────
class TestDelivery(ChartExportBase):
    def test_deliver_true_calls_send_photo_with_png(self):
        sent = {}

        def _fake_send_photo(path, caption="", chat_id=""):
            sent["path"] = path
            sent["caption"] = caption
            from agent_core.tool_result import ToolResult
            return ToolResult.success("✅ sent")

        with mock.patch("agent_core.telegram.telegram_send_photo", _fake_send_photo):
            spec = {"type": "pie", "title": "占比", "labels": ["甲", "乙"], "values": [1, 1]}
            res = generate_chart(self._dumps(spec), deliver=True)
        self.assertTrue(res.ok)
        self.assertIn("path", sent)
        self.assertTrue(sent["path"].endswith(".png"))
        self.assertIn("占比", sent["caption"])
        self.assertIn("Telegram", str(res))

    def test_deliver_failure_does_not_crash_tool(self):
        # 傳送爆掉時，畫圖本身仍算成功（檔案已存），只在 summary 標註
        def _boom(*a, **k):
            raise RuntimeError("network down")

        with mock.patch("agent_core.telegram.telegram_send_photo", _boom):
            spec = {"type": "line", "values": [1, 2, 3]}
            res = generate_chart(self._dumps(spec), deliver=True)
        self.assertTrue(res.ok)  # 畫圖成功
        self.assertIn("傳送失敗", str(res))

    def test_deliver_false_keeps_file_no_send(self):
        with mock.patch("agent_core.telegram.telegram_send_photo") as m:
            spec = {"type": "line", "values": [1, 2, 3]}
            res = generate_chart(self._dumps(spec), deliver=False)
        m.assert_not_called()
        self.assertTrue(res.ok)
        self.assertIn(self._tmp, (res.data or {}).get("path", ""))


# ────────────────────────────────────────────────────────────────────
# barh + 逐條上色 + exports 自動清理
# ────────────────────────────────────────────────────────────────────
class TestBarhAndColors(ChartExportBase):
    def test_barh_renders_with_per_bar_colors_and_refline(self):
        spec = {"type": "橫條圖", "title": "達成率", "xlabel": "%", "value_suffix": "%",
                "reference_line": 100, "reference_label": "目標",
                "categories": ["甲", "乙", "丙"],
                "series": [{"name": "達成率", "values": [33, 72, 100],
                            "colors": ["#E74C3C", "#E67E22", "#2ECC71"],
                            "annotations": ["33%", "72%", "100% 結案"]}]}
        self._assert_valid_png(*self._render(spec))

    def test_vertical_bar_per_point_colors(self):
        # 單序列 bar 給 colors 逐條上色（每日產量標峰用）
        spec = {"type": "bar", "title": "每日產量", "categories": ["1", "2", "3"],
                "series": [{"name": "量", "values": [10, 50, 30],
                            "colors": ["#3498DB", "#E74C3C", "#3498DB"]}]}
        self._assert_valid_png(*self._render(spec))

    def test_barh_annotation_length_mismatch_errors(self):
        spec = {"type": "barh", "categories": ["a", "b"],
                "series": [{"name": "x", "values": [1, 2], "annotations": ["only-one"]}]}
        res = generate_chart(self._dumps(spec), deliver=False)
        self.assertFalse(res.ok)


class TestPruneOldExports(ChartExportBase):
    def _touch(self, name, age_days):
        import os
        import time
        p = os.path.join(self._tmp, name)
        with open(p, "wb") as f:
            f.write(b"x")
        old = time.time() - age_days * 86400
        os.utime(p, (old, old))
        return p

    def test_prunes_old_keeps_recent(self):
        import os
        old_png = self._touch("old.png", 30)
        old_xlsx = self._touch("old.xlsx", 30)
        new_png = self._touch("new.png", 1)
        keep_txt = self._touch("notes.txt", 30)   # 非產出格式 → 不動
        chart_export._prune_old_exports(keep_days=14)
        self.assertFalse(os.path.exists(old_png))
        self.assertFalse(os.path.exists(old_xlsx))
        self.assertTrue(os.path.exists(new_png))
        self.assertTrue(os.path.exists(keep_txt))

    def test_keep_days_zero_disables(self):
        import os
        old_png = self._touch("old.png", 99)
        chart_export._prune_old_exports(keep_days=0)
        self.assertTrue(os.path.exists(old_png))   # 0 = 停用，不刪

    def test_generate_chart_triggers_prune(self):
        import os
        old_png = self._touch("ancient.png", 60)
        res = generate_chart(self._dumps({"type": "line", "values": [1, 2, 3]}), deliver=False)
        self.assertTrue(res.ok)
        self.assertFalse(os.path.exists(old_png))   # 畫圖前順手清掉舊檔


if __name__ == "__main__":
    unittest.main()
