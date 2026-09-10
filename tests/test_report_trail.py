"""排程報表的資料來源足跡（agent_core.report_trail）。

足跡回答的是「這份報表的數字哪來的」。重點在它取自 SDK 記的
automatic_function_calling_history —— 是實際發生的呼叫，不是模型自述的出處
（模型自述會漏、會編）。

這個檔另外釘住兩個「加足跡會順手弄壞 dispatcher」的坑：
  1. 空結果加了足跡就變非空 → 本來該安靜跳過的排程開始每輪寄信
  2. dedup 雜湊算進足跡 → 參數含日期，同一份報表天天雜湊不同 → 重複寄信
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_core import report_trail  # noqa: E402


def _call(name, args):
    return types.SimpleNamespace(function_call=types.SimpleNamespace(name=name, args=args))


def _resp(*calls):
    """組一個帶 AFC history 的假回應。"""
    return types.SimpleNamespace(
        automatic_function_calling_history=[
            types.SimpleNamespace(parts=list(calls))
        ]
    )


class ExtractTrailTests(unittest.TestCase):
    def test_extracts_tool_names_and_args(self):
        trail = report_trail.extract_tool_trail(
            _resp(_call("query_email_lake", {"days": 7}))
        )
        self.assertEqual(trail, [{"tool": "query_email_lake", "args": {"days": 7}}])

    def test_ignores_non_call_parts(self):
        """history 裡混著純文字 part 和 function_response，只挑 function_call。"""
        resp = _resp(
            types.SimpleNamespace(function_call=None, text="thinking"),
            _call("read_sheet", {"file_id": "abc"}),
        )
        trail = report_trail.extract_tool_trail(resp)
        self.assertEqual([t["tool"] for t in trail], ["read_sheet"])

    def test_no_history_returns_empty(self):
        self.assertEqual(report_trail.extract_tool_trail(types.SimpleNamespace()), [])
        self.assertEqual(report_trail.extract_tool_trail(None), [])

    def test_malformed_response_never_raises(self):
        """抽不到足跡絕不能讓報表發不出去。"""
        broken = types.SimpleNamespace(automatic_function_calling_history="not-a-list")
        self.assertEqual(report_trail.extract_tool_trail(broken), [])


class FooterFormatTests(unittest.TestCase):
    def test_lists_each_call(self):
        footer = report_trail.format_source_footer([
            {"tool": "read_sheet", "args": {"file_id": "abc"}},
            {"tool": "query_email_lake", "args": {"days": 7}},
        ])
        self.assertIn("read_sheet(file_id=abc)", footer)
        self.assertIn("query_email_lake(days=7)", footer)

    def test_empty_trail_warns_loudly(self):
        """沒查任何資料的報表 = 純模型輸出，這正是最該被懷疑的情況，
        但從報表外觀完全看不出來 —— 所以要明講。"""
        footer = report_trail.format_source_footer([])
        self.assertIn("沒有查詢任何資料來源", footer)

    def test_secrets_in_args_are_redacted(self):
        """參數會原樣進到寄給員工的信裡。"""
        footer = report_trail.format_source_footer([
            {"tool": "fetch", "args": {"key": "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}},
        ])
        self.assertNotIn("sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", footer)

    def test_long_args_truncated(self):
        footer = report_trail.format_source_footer([
            {"tool": "search", "args": {"q": "x" * 500}},
        ])
        self.assertLess(len(footer), 400)
        self.assertIn("…", footer)

    def test_newlines_in_args_do_not_break_layout(self):
        footer = report_trail.format_source_footer([
            {"tool": "search", "args": {"q": "line1\nline2"}},
        ])
        # 足跡本身是多行，但單一筆呼叫必須壓成一行
        rows = [ln for ln in footer.splitlines() if ln.startswith("1. ")]
        self.assertEqual(len(rows), 1)
        self.assertIn("line1 line2", rows[0])


class AppendAndStripTests(unittest.TestCase):
    def test_append_adds_footer_after_body(self):
        out = report_trail.append_source_footer(
            "今日產能 1200 雙", _resp(_call("read_sheet", {"file_id": "abc"})),
        )
        self.assertTrue(out.startswith("今日產能 1200 雙"))
        self.assertIn("read_sheet", out)

    def test_empty_body_stays_empty(self):
        """回歸守衛：空結果加了足跡就變非空 → dispatcher 的
        dispatcher_result_is_empty 攔不住 → 本該安靜跳過的排程每輪寄信。"""
        for empty in ("", "   ", None):
            with self.subTest(value=repr(empty)):
                out = report_trail.append_source_footer(
                    empty, _resp(_call("read_sheet", {})),
                )
                self.assertFalse((out or "").strip())

    def test_skip_marker_still_detected_after_append(self):
        """「(無新發現)」開頭的結果加了足跡後，仍要被判定成空。"""
        from agent_core.daemon_dispatcher import dispatcher_result_is_empty
        out = report_trail.append_source_footer(
            "(無新發現)", _resp(_call("read_sheet", {})),
        )
        self.assertTrue(dispatcher_result_is_empty(out))

    def test_strip_recovers_body(self):
        body = "今日產能 1200 雙"
        out = report_trail.append_source_footer(body, _resp(_call("read_sheet", {})))
        self.assertEqual(report_trail.strip_source_footer(out), body)

    def test_strip_handles_no_tool_warning_variant(self):
        body = "今日產能 1200 雙"
        out = report_trail.append_source_footer(body, _resp())  # 無工具 → 警告版
        self.assertIn("沒有查詢任何資料來源", out)
        self.assertEqual(report_trail.strip_source_footer(out), body)

    def test_strip_is_noop_without_footer(self):
        self.assertEqual(report_trail.strip_source_footer("純正文"), "純正文")
        self.assertEqual(report_trail.strip_source_footer(""), "")


class DedupStabilityTests(unittest.TestCase):
    """回歸守衛：dedup 雜湊必須算在正文上。

    足跡帶工具參數，參數常含日期／時間戳。若雜湊算進足跡，同一份「今天沒有
    變化」的報表每天雜湊都不同 → dedup 失效 → 天天重寄同樣內容。
    """

    def test_same_body_different_args_dedups(self):
        from agent_core.daemon_dispatcher import remember_dispatcher_result
        body = "今日無異常"
        day1 = report_trail.append_source_footer(
            body, _resp(_call("query_email_lake", {"date": "2026-08-06"})))
        day2 = report_trail.append_source_footer(
            body, _resp(_call("query_email_lake", {"date": "2026-08-07"})))
        task: dict = {}
        self.assertTrue(remember_dispatcher_result(task, day1))
        self.assertFalse(
            remember_dispatcher_result(task, day2),
            "正文相同、只有足跡裡的日期不同，不該視為新結果",
        )

    def test_different_body_still_notifies(self):
        from agent_core.daemon_dispatcher import remember_dispatcher_result
        task: dict = {}
        a = report_trail.append_source_footer("產能 1200", _resp(_call("t", {})))
        b = report_trail.append_source_footer("產能 1400", _resp(_call("t", {})))
        self.assertTrue(remember_dispatcher_result(task, a))
        self.assertTrue(remember_dispatcher_result(task, b))


if __name__ == "__main__":
    unittest.main()
