"""回覆附檔機制測試（[[TG_FILE:...]] 標記 → sendDocument 回當前對話）。

2026-08-03 開發部 UserC 案：green 員工說「請生成 excel 表」，小紅答「green
部門唯讀查詢，需 Red 管理員權限」只回 CSV 文字。兩個洞：export_report 不在
部門工具白名單；就算放行，它的自動傳送走 telegram_send_file，chat_id 閘只認
大王 keyring chat，員工要的檔會傳到大王手機。

covers：
  - dept_tool_scope 白名單放行 export_report（且仍是 SAFE tier）
  - doc_export 在部門色 context 下 → 產到 exports/dept/<色>、回 [[TG_FILE:]]
    標記、不呼叫 telegram_send_file；大王路徑（無 context）完全不變
  - daemon_telegram 標記抽取的 per-color 路徑白名單 fail-closed
  - tg_send_with_photos 的文字/照片/檔案分流

注意（unittest discover）：conftest fixture 不生效，隔離全在 setUp/tearDown；
mock.patch 一律在主執行緒。不 hardcode 任何 /Users/... 路徑。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

_TABLE = [
    {"部位": "A001", "料號": "PUB014T1718D05400000-A020", "單位用量": 0.0968},
    {"部位": "A002", "料號": "ACA007A00T1618000-A020", "單位用量": 0.5612},
]


# ────────────────────────────────────────────────────────────────────
# 白名單：export_report 對部門員工開放
# ────────────────────────────────────────────────────────────────────
class DeptToolScopeExportTests(unittest.TestCase):
    def test_export_report_allowed_for_every_color(self):
        from agent_core.agents.permission_matrix import Agent
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        for agent in Agent:
            if agent.value == "red":
                continue           # red 走 GM 全工具路徑，不在此制度內
            self.assertIn("export_report",
                          allowed_tool_names_for_color(agent.value),
                          f"{agent.value} 應拿得到 export_report")

    def test_export_report_is_safe_tier_so_it_survives_the_filter(self):
        # filter_tools_for_color 只留 SAFE；不是 SAFE 的話白名單放了也會被剔除。
        from agent_core.tool_tiers import TIER_SAFE, get_tier
        self.assertEqual(get_tier("export_report"), TIER_SAFE)

    def test_addendum_tells_llm_it_can_produce_files(self):
        from agent_core.dept_tool_scope import dept_scope_addendum
        text = dept_scope_addendum("green")
        self.assertIn("export_report", text)
        self.assertIn("[[TG_FILE:", text)


# ────────────────────────────────────────────────────────────────────
# doc_export：部門色 context 下的產檔與交付
# ────────────────────────────────────────────────────────────────────
class DeptScopedExportTests(unittest.TestCase):
    def setUp(self):
        from agent_core import doc_export
        self.doc_export = doc_export
        self._tmp = tempfile.mkdtemp(prefix="red_dept_export_test_")
        self._patch = mock.patch.object(doc_export, "EXPORTS_DIR", self._tmp)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _as_green(self):
        from agent_core.agents.middleware import AgentRequest, agent_request_context
        from agent_core.agents.permission_matrix import Agent
        return agent_request_context(AgentRequest(
            caller=Agent.GREEN, target=Agent.GREEN,
            intent="telegram.freeform.export_report", payload={}))

    def test_dept_scope_color_reads_current_request(self):
        self.assertEqual(self.doc_export._dept_scope_color(), "")
        with self._as_green():
            self.assertEqual(self.doc_export._dept_scope_color(), "green")

    def test_red_caller_is_treated_as_owner_path(self):
        from agent_core.agents.middleware import AgentRequest, agent_request_context
        from agent_core.agents.permission_matrix import Agent
        req = AgentRequest(caller=Agent.RED, target=Agent.RED,
                           intent="whatever", payload={})
        with agent_request_context(req):
            self.assertEqual(self.doc_export._dept_scope_color(), "")

    def test_employee_export_lands_in_per_color_dir_with_marker(self):
        with mock.patch("agent_core.telegram.telegram_send_file") as m_send, \
                self._as_green():
            res = self.doc_export.export_report(
                json.dumps(_TABLE, ensure_ascii=False), formats="excel")
        self.assertTrue(res.ok, res)
        # 出站推送那條路完全沒被碰（否則檔案會飛去大王手機）
        m_send.assert_not_called()
        path = res.artifacts[0]
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.path.dirname(path),
                         os.path.join(self._tmp, "dept", "green"))
        self.assertIn(f"[[TG_FILE:{path}]]", res.summary)

    def test_employee_export_ignores_llm_supplied_chat_id(self):
        # chat_id 是 LLM 可控參數：員工路徑不該讓它決定收件人。
        with mock.patch("agent_core.telegram.telegram_send_file") as m_send, \
                self._as_green():
            res = self.doc_export.export_report(
                json.dumps(_TABLE, ensure_ascii=False), formats="excel",
                chat_id="99999999")
        m_send.assert_not_called()
        self.assertNotIn("99999999", res.summary)

    def test_employee_deliver_false_still_skips_marker(self):
        with self._as_green():
            res = self.doc_export.export_report(
                json.dumps(_TABLE, ensure_ascii=False), formats="excel",
                deliver=False)
        self.assertTrue(res.ok, res)
        self.assertNotIn("[[TG_FILE:", res.summary)

    def test_owner_path_unchanged(self):
        from agent_core.tool_result import ToolResult
        sent = []

        def fake_send(path, caption="", chat_id=""):
            sent.append((path, chat_id))
            return ToolResult.success("ok")

        with mock.patch("agent_core.telegram.telegram_send_file", fake_send):
            res = self.doc_export.export_report(
                json.dumps(_TABLE, ensure_ascii=False), formats="excel",
                chat_id="123")
        self.assertTrue(res.ok, res)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], "123")             # chat_id 照舊帶過去
        self.assertNotIn("[[TG_FILE:", res.summary)     # 大王不用標記機制
        self.assertEqual(os.path.dirname(res.artifacts[0]), self._tmp)

    def test_prune_only_removes_expired_exports(self):
        import time
        out_dir = os.path.join(self._tmp, "dept", "green")
        os.makedirs(out_dir, exist_ok=True)
        old = os.path.join(out_dir, "old.xlsx")
        fresh = os.path.join(out_dir, "fresh.xlsx")
        keeper = os.path.join(out_dir, "notes.txt")
        for p in (old, fresh, keeper):
            with open(p, "wb") as f:
                f.write(b"x")
        stale = time.time() - 30 * 86400
        os.utime(old, (stale, stale))
        self.doc_export._prune_dept_exports(out_dir, keep_days=14)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(keeper))   # 非產出格式不動

    def test_prune_disabled_when_keep_days_non_positive(self):
        import time
        out_dir = os.path.join(self._tmp, "dept", "green")
        os.makedirs(out_dir, exist_ok=True)
        old = os.path.join(out_dir, "old.xlsx")
        with open(old, "wb") as f:
            f.write(b"x")
        stale = time.time() - 30 * 86400
        os.utime(old, (stale, stale))
        self.doc_export._prune_dept_exports(out_dir, keep_days=0)
        self.assertTrue(os.path.exists(old))


# ────────────────────────────────────────────────────────────────────
# daemon 端：標記抽取（per-color 路徑白名單，逐條 fail-closed）
# ────────────────────────────────────────────────────────────────────
class ExtractReplyFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self._roots_patch = mock.patch(
            "agent_core.daemon_telegram._reply_file_allowed_roots",
            return_value=(self.root,),
        )
        self._roots_patch.start()

    def tearDown(self):
        self._roots_patch.stop()
        self.tmp.cleanup()

    def _file(self, name: str, size: int = 10) -> str:
        path = os.path.join(self.root, name)
        with open(path, "wb") as f:
            f.write(b"x" * size)
        return path

    def test_no_marker_passthrough(self):
        from agent_core.daemon_telegram import _extract_reply_files
        clean, files = _extract_reply_files("一般回覆，沒有標記", "green")
        self.assertEqual(clean, "一般回覆，沒有標記")
        self.assertEqual(files, [])

    def test_valid_marker_extracted_and_stripped(self):
        from agent_core.daemon_telegram import _extract_reply_files
        p = self._file("報表.xlsx")
        clean, files = _extract_reply_files(f"表做好了：\n\n[[TG_FILE:{p}]]\n", "green")
        self.assertEqual(files, [p])
        self.assertNotIn("TG_FILE", clean)
        self.assertIn("表做好了", clean)

    def test_path_outside_allowed_roots_rejected(self):
        from agent_core.daemon_telegram import _extract_reply_files
        outside = os.path.join(os.path.dirname(self.root), "evil.xlsx")
        with open(outside, "wb") as f:
            f.write(b"x")
        try:
            clean, files = _extract_reply_files(f"[[TG_FILE:{outside}]]", "green")
            self.assertEqual(files, [])
            self.assertNotIn("TG_FILE", clean)
        finally:
            os.remove(outside)

    def test_traversal_into_root_prefix_sibling_rejected(self):
        from agent_core.daemon_telegram import _extract_reply_files
        sibling_dir = self.root + "-evil"
        os.makedirs(sibling_dir, exist_ok=True)
        evil = os.path.join(sibling_dir, "b.xlsx")
        with open(evil, "wb") as f:
            f.write(b"x")
        try:
            _clean, files = _extract_reply_files(f"[[TG_FILE:{evil}]]", "green")
            self.assertEqual(files, [])
        finally:
            os.remove(evil)
            os.rmdir(sibling_dir)

    def test_relative_path_marker_ignored(self):
        from agent_core.daemon_telegram import _extract_reply_files
        _clean, files = _extract_reply_files("[[TG_FILE:報表.xlsx]]", "green")
        self.assertEqual(files, [])

    def test_disallowed_ext_rejected(self):
        from agent_core.daemon_telegram import _extract_reply_files
        p = self._file("id_rsa.key")
        _clean, files = _extract_reply_files(f"[[TG_FILE:{p}]]", "green")
        self.assertEqual(files, [])

    def test_missing_file_rejected(self):
        from agent_core.daemon_telegram import _extract_reply_files
        ghost = os.path.join(self.root, "ghost.xlsx")
        _clean, files = _extract_reply_files(f"[[TG_FILE:{ghost}]]", "green")
        self.assertEqual(files, [])

    def test_oversize_rejected(self):
        from agent_core import daemon_telegram as dt
        p = self._file("big.xlsx")
        with mock.patch.object(dt, "_TG_REPLY_FILE_MAX_BYTES", 5):
            _clean, files = dt._extract_reply_files(f"[[TG_FILE:{p}]]", "green")
        self.assertEqual(files, [])

    def test_cap_and_dedup(self):
        from agent_core import daemon_telegram as dt
        paths = [self._file(f"r{i}.xlsx") for i in range(6)]
        markers = "\n".join(f"[[TG_FILE:{p}]]" for p in paths + [paths[0]])
        _clean, files = dt._extract_reply_files(markers, "green")
        self.assertEqual(len(files), dt._TG_REPLY_FILE_MAX)
        self.assertEqual(len(set(files)), len(files))


class ReplyFileRootsTests(unittest.TestCase):
    """白名單根目錄本身：只認自己那一色，大王/空色一律無根。"""

    def test_color_root_is_per_color_subdir(self):
        from agent_core.daemon_telegram import _reply_file_allowed_roots
        from agent_core.logging_and_paths import EXPORTS_DIR
        roots = _reply_file_allowed_roots("green")
        self.assertEqual(
            roots, (os.path.realpath(os.path.join(EXPORTS_DIR, "dept", "green")),))

    def test_other_color_root_does_not_cover_green(self):
        from agent_core.daemon_telegram import _reply_file_allowed_roots
        self.assertNotEqual(_reply_file_allowed_roots("purple"),
                            _reply_file_allowed_roots("green"))

    def test_owner_and_blank_have_no_roots(self):
        from agent_core.daemon_telegram import _reply_file_allowed_roots
        self.assertEqual(_reply_file_allowed_roots(""), ())
        self.assertEqual(_reply_file_allowed_roots("red"), ())
        self.assertEqual(_reply_file_allowed_roots("  RED  "), ())

    def test_marker_stripped_even_when_color_has_no_roots(self):
        # 沒白名單也不能把 [[TG_FILE:/絕對路徑]] 原文丟給使用者（洩漏本機路徑）。
        from agent_core.daemon_telegram import _extract_reply_files
        clean, files = _extract_reply_files(
            "檔案在這\n[[TG_FILE:/tmp/whatever.xlsx]]", "")
        self.assertEqual(files, [])
        self.assertNotIn("TG_FILE", clean)
        self.assertIn("檔案在這", clean)


class ReplyAttachmentColorTests(unittest.TestCase):
    def test_owner_gets_blank(self):
        from agent_core.daemon_telegram import _reply_attachment_color
        self.assertEqual(
            _reply_attachment_color({"is_owner": "true", "color": "red"}), "")

    def test_no_actor_gets_blank(self):
        from agent_core.daemon_telegram import _reply_attachment_color
        self.assertEqual(_reply_attachment_color(None), "")
        self.assertEqual(_reply_attachment_color({}), "")

    def test_colored_employee_gets_color(self):
        from agent_core.daemon_telegram import _reply_attachment_color
        self.assertEqual(
            _reply_attachment_color({"color": "Green", "name": "UserC"}), "green")

    def test_red_employee_gets_blank(self):
        from agent_core.daemon_telegram import _reply_attachment_color
        self.assertEqual(_reply_attachment_color({"color": "red"}), "")


# ────────────────────────────────────────────────────────────────────
# 送出點：文字 / 照片 / 檔案分流
# ────────────────────────────────────────────────────────────────────
class TgSendWithFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self._roots_patch = mock.patch(
            "agent_core.daemon_telegram._reply_file_allowed_roots",
            return_value=(self.root,),
        )
        self._roots_patch.start()
        self.doc = os.path.join(self.root, "用料表.xlsx")
        with open(self.doc, "wb") as f:
            f.write(b"xlsx")

    def tearDown(self):
        self._roots_patch.stop()
        self.tmp.cleanup()

    def test_text_and_document_sent_with_same_token_and_chat(self):
        from agent_core import daemon_telegram as dt
        calls = []

        def fake_doc(*, target, part_path, part_name, caption, token=None):
            calls.append((target, part_path, part_name, token))
            return ({"ok": True}, None, 1)

        with mock.patch.object(dt, "tg_send", return_value=True) as m_send, \
                mock.patch("agent_core.telegram._send_document_part_with_retries",
                           fake_doc):
            ok = dt.tg_send_with_photos(
                "TOKEN", "555", f"用料表做好了\n[[TG_FILE:{self.doc}]]",
                actor_color="green")
        self.assertTrue(ok)
        self.assertEqual(calls, [("555", self.doc, "用料表.xlsx", "TOKEN")])
        sent_text = m_send.call_args[0][2]
        self.assertNotIn("TG_FILE", sent_text)
        self.assertIn("用料表做好了", sent_text)

    def test_no_color_means_no_document_even_with_marker(self):
        from agent_core import daemon_telegram as dt
        with mock.patch.object(dt, "tg_send", return_value=True) as m_send, \
                mock.patch("agent_core.telegram._send_document_part_with_retries") as m_doc, \
                mock.patch.object(dt, "_reply_file_allowed_roots", return_value=()):
            ok = dt.tg_send_with_photos(
                "TOKEN", "555", f"檔案：\n[[TG_FILE:{self.doc}]]")
        self.assertTrue(ok)
        m_doc.assert_not_called()
        self.assertNotIn("TG_FILE", m_send.call_args[0][2])

    def test_document_failure_does_not_break_text_reply(self):
        from agent_core import daemon_telegram as dt
        with mock.patch.object(dt, "tg_send", return_value=True), \
                mock.patch("agent_core.telegram._send_document_part_with_retries",
                           side_effect=RuntimeError("network down")):
            ok = dt.tg_send_with_photos(
                "TOKEN", "555", f"表在這\n[[TG_FILE:{self.doc}]]",
                actor_color="green")
        self.assertTrue(ok)   # 回傳值是「文字有沒有送達」

    def test_marker_only_reply_still_sends_document_without_placeholder(self):
        from agent_core import daemon_telegram as dt
        with mock.patch.object(dt, "tg_send", return_value=True) as m_send, \
                mock.patch("agent_core.telegram._send_document_part_with_retries",
                           return_value=({"ok": True}, None, 1)) as m_doc:
            ok = dt.tg_send_with_photos(
                "TOKEN", "555", f"[[TG_FILE:{self.doc}]]", actor_color="green")
        self.assertTrue(ok)
        self.assertEqual(m_doc.call_count, 1)
        m_send.assert_not_called()   # 沒文字可送就別送空訊息

    def test_all_markers_rejected_falls_back_to_warning_text(self):
        from agent_core import daemon_telegram as dt
        ghost = os.path.join(self.root, "ghost.xlsx")
        with mock.patch.object(dt, "tg_send", return_value=True) as m_send, \
                mock.patch("agent_core.telegram._send_document_part_with_retries") as m_doc:
            ok = dt.tg_send_with_photos(
                "TOKEN", "555", f"[[TG_FILE:{ghost}]]", actor_color="green")
        self.assertTrue(ok)
        m_doc.assert_not_called()
        self.assertIn("未通過驗證", m_send.call_args[0][2])

    def test_photos_and_files_can_ride_the_same_reply(self):
        from agent_core import daemon_telegram as dt
        photo = os.path.join(self.root, "shoe.jpg")
        with open(photo, "wb") as f:
            f.write(b"jpg")
        with mock.patch.object(dt, "tg_send", return_value=True), \
                mock.patch.object(dt, "_reply_photo_allowed_roots",
                                  return_value=(self.root,)), \
                mock.patch("agent_core.telegram._send_photo_with_retries",
                           return_value=({"ok": True}, None)) as m_photo, \
                mock.patch("agent_core.telegram._send_document_part_with_retries",
                           return_value=({"ok": True}, None, 1)) as m_doc:
            ok = dt.tg_send_with_photos(
                "TOKEN", "555",
                f"都在這\n[[TG_PHOTO:{photo}]]\n[[TG_FILE:{self.doc}]]",
                actor_color="green")
        self.assertTrue(ok)
        self.assertEqual(m_photo.call_count, 1)
        self.assertEqual(m_doc.call_count, 1)

    def test_plain_reply_is_untouched(self):
        from agent_core import daemon_telegram as dt
        with mock.patch.object(dt, "tg_send", return_value=True) as m_send, \
                mock.patch("agent_core.telegram._send_document_part_with_retries") as m_doc:
            ok = dt.tg_send_with_photos("TOKEN", "555", "一般文字回覆",
                                        actor_color="green")
        self.assertTrue(ok)
        m_doc.assert_not_called()
        self.assertEqual(m_send.call_args[0][2], "一般文字回覆")


if __name__ == "__main__":
    unittest.main()
