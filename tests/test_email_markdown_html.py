"""排程通知信 markdown → HTML alternative（表格對齊）整條鏈的測試。

背景：daily_production_8am 這類 dispatcher 任務的結果是 Gemini 產的 markdown
（含表格），以前整包塞 text/plain 寄出 → Mail 客戶端把 `| :---: |` 原樣顯示、
欄位沒對齊。修法分三層，各自在這裡驗：
  1. email_format.markdown_to_email_html：markdown → HTML（表格轉真 <table>）；
     沒有 markdown 結構回 None（呼叫端維持純文字＝零行為改變）。
  2. gmail_ops.build_mime(html_alt=...)：multipart/alternative（純文字 + HTML），
     純文字版保留給 extract_body / email lake。
  3. gmail_ops.send_gmail(html_alt_fn=...)：簽名檔之後才渲染；渲染失敗/回 None
     都退回純文字，絕不擋寄信。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core.email_format import markdown_to_email_html  # noqa: E402
from agent_core import gmail_ops  # noqa: E402


_PRODUCTION_BODY = (
    "🔔 任務「daily_production_8am」的最新結果：\n"
    "\n"
    "### 📊 生產日報與進度核報\n"
    "\n"
    "**生產日期：2026-07-19 (星期日)**\n"
    "*註：昨日適逢週日休工，僅有 LURCHI 安排少量包裝。*\n"
    "\n"
    "#### 1. 昨日各客戶生產數量明細 (精簡表)\n"
    "\n"
    "| 客戶品牌 | 灌注 (成型) 產出 | 包裝 產出 | 截至昨日 (19號) 累計已包裝 | 本季剩餘未完 (雙) |\n"
    "| :--- | :---: | :---: | :---: | :---: |\n"
    "| **LURCHI** | 0 雙 | 127 雙 | 25,608 雙 | 13,021 雙 |\n"
    "| **DECA.26** | 0 雙 | 0 雙 | 126,232 雙 | 54,779 雙 |\n"
    "\n"
    "---\n"
    "（移除：跟小紅說「取消排程 daily_production_8am」）"
)


class MarkdownToEmailHtmlTests(unittest.TestCase):
    def test_production_report_table_renders_as_html_table(self):
        html = markdown_to_email_html(_PRODUCTION_BODY)
        self.assertIsNotNone(html)
        self.assertIn("<table", html)
        # 分隔列被吃掉、儲存格不再有原始管線符號。
        self.assertNotIn(":---", html)
        self.assertIn("<th", html)
        self.assertIn("客戶品牌", html)
        # 對齊行 `:---` → 左、`:---:` → 置中。
        self.assertIn("text-align:left", html)
        self.assertIn("text-align:center", html)
        # 儲存格內 **粗體** 轉 <b>。
        self.assertIn("<b>LURCHI</b>", html)
        self.assertIn("25,608 雙", html)
        # 標題/分隔線/斜體也轉了。
        self.assertIn("生產日報與進度核報", html)
        self.assertIn("<hr", html)
        self.assertIn("<i>註：", html)
        self.assertNotIn("**", html)

    def test_right_alignment_honoured(self):
        html = markdown_to_email_html("| a | b |\n| :--- | ---: |\n| x | 1 |")
        self.assertIn("text-align:right", html)

    def test_plain_text_returns_none(self):
        self.assertIsNone(markdown_to_email_html("今天沒有新發現。\n一切正常。"))
        self.assertIsNone(markdown_to_email_html(""))
        self.assertIsNone(markdown_to_email_html(None))

    def test_html_in_content_is_escaped(self):
        html = markdown_to_email_html("### 標題\n<script>alert(1)</script>")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_fence_becomes_pre(self):
        html = markdown_to_email_html("```\n對齊  用  等寬\n```")
        self.assertIn("<pre", html)
        self.assertIn("對齊  用  等寬", html)

    def test_unclosed_fence_is_not_markdown(self):
        # 未成對的 ``` 不冒險渲染；沒有其他結構 → 整體回 None（純文字信）。
        self.assertIsNone(markdown_to_email_html("```\n只有開頭沒有結尾"))

    def test_table_without_separator_row_stays_text(self):
        # 只有含 | 的行、第二行不是分隔列 → 不是 markdown 表格。
        self.assertIsNone(markdown_to_email_html("a | b\nc | d"))


class BuildMimeHtmlAltTests(unittest.TestCase):
    def test_html_alt_builds_multipart_alternative(self):
        msg = gmail_ops.build_mime("純文字版", [], html_alt="<div>HTML 版</div>")
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        parts = msg.get_payload()
        self.assertEqual(len(parts), 2)
        # 純文字在前（客戶端顯示最後一個看得懂的 part → HTML 優先）。
        self.assertEqual(parts[0].get_content_type(), "text/plain")
        self.assertEqual(parts[1].get_content_type(), "text/html")
        self.assertEqual(parts[0].get_payload(decode=True).decode("utf-8"), "純文字版")
        self.assertIn("HTML 版", parts[1].get_payload(decode=True).decode("utf-8"))

    def test_html_alt_with_attachment_nests_alternative_in_mixed(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"att")
            path = f.name
        try:
            msg = gmail_ops.build_mime("text", [path], html_alt="<p>h</p>")
            self.assertEqual(msg.get_content_type(), "multipart/mixed")
            parts = msg.get_payload()
            self.assertEqual(parts[0].get_content_type(), "multipart/alternative")
            self.assertEqual(len(parts), 2)  # alternative + 附件
        finally:
            os.unlink(path)

    def test_no_html_alt_keeps_legacy_plain_shape(self):
        msg = gmail_ops.build_mime("text", [])
        self.assertEqual(msg.get_content_type(), "text/plain")


class SendGmailHtmlAltFnTests(unittest.TestCase):
    def _send(self, body: str, html_alt_fn):
        build_calls = []

        def fake_build_mime(b, attachments, html=False, html_alt=""):
            build_calls.append({"body": b, "html_alt": html_alt})
            from email.mime.text import MIMEText
            return MIMEText("x", "plain", "utf-8")

        service = mock.Mock()
        res = gmail_ops.send_gmail(
            "a@x.com", "主旨", body,
            cc="", bcc="", attachments="",
            get_service=lambda *a: service,
            append_signature_fn=lambda b: b + "\n\nBest regards,\n\nRed",
            build_mime_fn=fake_build_mime,
            split_paths_fn=lambda s: [],
            index_memory_fn=lambda *a, **k: None,
            html_alt_fn=html_alt_fn,
        )
        return res, build_calls

    def test_markdown_body_gets_html_alt_with_signature(self):
        res, calls = self._send(
            "| a | b |\n| :--- | ---: |\n| x | 1 |", markdown_to_email_html
        )
        self.assertIn("已寄出", res)
        self.assertIn("<table", calls[0]["html_alt"])
        # 渲染在簽名檔之後 → HTML 版也含簽名。
        self.assertIn("Best regards", calls[0]["html_alt"])

    def test_plain_body_falls_back_to_text_only(self):
        res, calls = self._send("純文字，沒有表格。", markdown_to_email_html)
        self.assertIn("已寄出", res)
        self.assertEqual(calls[0]["html_alt"], "")

    def test_render_crash_never_blocks_sending(self):
        def boom(_):
            raise RuntimeError("render 炸了")
        res, calls = self._send("| a |\n| --- |\n| 1 |", boom)
        self.assertIn("已寄出", res)
        self.assertEqual(calls[0]["html_alt"], "")


class NotifyMarkdownHtmlRoutingTests(unittest.TestCase):
    """notify() 的 markdown_html 轉送語意。

    以前 notify 靠「呼叫 send_gmail 還是 send_gmail_markdown」來分流；現在統一
    走 send_gmail_internal(markdown_html=...)，好讓它能一併帶 generated_by
    出處標記（send_gmail 是 LLM 工具、簽名不能動）。契約沒變，斷言換到新接縫。
    """

    def _notify(self, *, markdown_html):
        from agent_core import daemon_helpers
        with mock.patch("agent_core.gmail.send_gmail_internal",
                        return_value="已寄出給 me") as send, \
             mock.patch.object(daemon_helpers, "get_my_email",
                               return_value="me@x.com"):
            ok = daemon_helpers.notify(
                "主旨", "| a |\n| --- |\n| 1 |", "t", markdown_html=markdown_html,
            )
        send.assert_called_once()
        return ok, send.call_args.kwargs

    def test_markdown_html_flag_requests_html_alternative(self):
        ok, kwargs = self._notify(markdown_html=True)
        self.assertTrue(ok)
        self.assertTrue(kwargs["markdown_html"])

    def test_default_stays_plain_text(self):
        ok, kwargs = self._notify(markdown_html=False)
        self.assertTrue(ok)
        self.assertFalse(kwargs["markdown_html"])

    def test_notify_always_marks_output_as_red_generated(self):
        """daemon 通知信一律帶出處標記——否則隔天 RAG 會當公司原始信吃回去。"""
        _ok, kwargs = self._notify(markdown_html=False)
        self.assertEqual(kwargs["generated_by"], "daemon:t")


if __name__ == "__main__":
    unittest.main()
