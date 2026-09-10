"""Owner session 主控台 registry 測試（agent_core/session_registry.py）。

跑的是 unittest discover（非 pytest），隔離寫在 setUp/tearDown：每個 test
把 RED_SESSION_REGISTRY_FILE 指到獨立 tmp 檔，_registry_path() 每次讀 env，
所以不會碰到真實 var/state，也不互相污染。
"""
import os
import tempfile
import unittest
from unittest import mock

from agent_core import session_registry as sr


OWNER = {"is_owner": "true", "name": "大王", "color": "red"}
EMP = {"is_owner": "false", "name": "王小明", "email": "ming@factory.com", "color": "blue"}


class _RegistryIsolation:
    """每個 test 把 registry 指到獨立 tmp 檔（_registry_path() 每次讀 env）。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._tmpdir.name, "session_registry.json")
        self._prev = os.environ.get("RED_SESSION_REGISTRY_FILE")
        os.environ["RED_SESSION_REGISTRY_FILE"] = self._path

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("RED_SESSION_REGISTRY_FILE", None)
        else:
            os.environ["RED_SESSION_REGISTRY_FILE"] = self._prev
        self._tmpdir.cleanup()


class SessionRegistryTests(_RegistryIsolation, unittest.TestCase):
    # ── touch ────────────────────────────────────────────────────────
    def test_touch_creates_and_increments(self):
        r1 = sr.touch_session("telegram:2", actor=EMP, now=100.0)
        self.assertEqual(r1["turn_count"], 1)
        self.assertEqual(r1["status"], "active")
        self.assertEqual(r1["color"], "blue")
        self.assertFalse(r1["is_owner"])
        self.assertEqual(r1["first_seen"], 100.0)

        r2 = sr.touch_session("telegram:2", actor=EMP, now=150.0)
        self.assertEqual(r2["turn_count"], 2)
        self.assertEqual(r2["first_seen"], 100.0)   # 保留
        self.assertEqual(r2["last_seen"], 150.0)     # 更新

    def test_touch_persists_to_disk(self):
        sr.touch_session("telegram:2", actor=EMP)
        self.assertTrue(os.path.exists(self._path))
        # 另一次「程序」讀得到（模擬 daemon 寫、控制工具讀）
        self.assertIsNotNone(sr.get_session("telegram:2"))

    def test_touch_does_not_clobber_paused_state(self):
        """關鍵不變量：對方被暫停後又送訊息（觸發 touch），status 必須維持
        paused，否則暫停會被對方下一則訊息自動解除。"""
        sr.touch_session("telegram:2", actor=EMP)
        sr.pause_session("telegram:2", reason="x")
        after = sr.touch_session("telegram:2", actor=EMP)
        self.assertEqual(after["status"], "paused")
        self.assertEqual(after["paused_reason"], "x")

    # ── pause ────────────────────────────────────────────────────────
    def test_pause_sets_status(self):
        sr.touch_session("telegram:2", actor=EMP)
        msg = sr.pause_session("telegram:2", reason="開會")
        self.assertIn("已暫停", msg)
        self.assertTrue(sr.is_paused("telegram:2"))
        rec = sr.get_session("telegram:2")
        self.assertEqual(rec["paused_reason"], "開會")

    def test_pause_refuses_owner(self):
        sr.touch_session("telegram:1", actor=OWNER)
        msg = sr.pause_session("telegram:1")
        self.assertIn("禁止暫停", msg)
        self.assertFalse(sr.is_paused("telegram:1"))   # 仍 active

    def test_pause_accepts_bare_chat_id(self):
        sr.touch_session("telegram:2", actor=EMP)
        msg = sr.pause_session("2")   # 省略 telegram: 前綴
        self.assertIn("telegram:2", msg)
        self.assertTrue(sr.is_paused("telegram:2"))

    def test_pause_unknown_is_graceful(self):
        msg = sr.pause_session("telegram:999")
        self.assertIn("找不到", msg)

    def test_pause_empty_id(self):
        self.assertIn("請提供", sr.pause_session("  "))

    def test_pause_reason_truncated(self):
        sr.touch_session("telegram:2", actor=EMP)
        sr.pause_session("telegram:2", reason="x" * 500)
        self.assertLessEqual(len(sr.get_session("telegram:2")["paused_reason"]), 200)

    # ── resume ───────────────────────────────────────────────────────
    def test_resume_clears_pause(self):
        sr.touch_session("telegram:2", actor=EMP)
        sr.pause_session("telegram:2", reason="x")
        msg = sr.resume_session("telegram:2")
        self.assertIn("已恢復", msg)
        self.assertFalse(sr.is_paused("telegram:2"))
        self.assertEqual(sr.get_session("telegram:2")["paused_reason"], "")

    def test_resume_when_not_paused(self):
        sr.touch_session("telegram:2", actor=EMP)
        msg = sr.resume_session("telegram:2")
        self.assertIn("本來就不是暫停", msg)

    def test_resume_unknown_is_graceful(self):
        self.assertIn("找不到", sr.resume_session("telegram:999"))

    # ── reset ────────────────────────────────────────────────────────
    def test_reset_sets_pending_and_consume_is_one_shot(self):
        sr.touch_session("telegram:2", actor=EMP)
        msg = sr.reset_session("telegram:2")
        self.assertIn("重置", msg)
        self.assertTrue(sr.get_session("telegram:2")["reset_pending"])
        self.assertTrue(sr.consume_reset("telegram:2"))    # 第一次消費
        self.assertFalse(sr.consume_reset("telegram:2"))   # 第二次已清掉
        self.assertFalse(sr.get_session("telegram:2")["reset_pending"])

    def test_consume_reset_unknown(self):
        self.assertFalse(sr.consume_reset("telegram:404"))

    def test_reset_unknown_is_graceful(self):
        self.assertIn("找不到", sr.reset_session("telegram:999"))

    # ── list ─────────────────────────────────────────────────────────
    def test_list_hides_owner_by_default(self):
        sr.touch_session("telegram:1", actor=OWNER, now=10.0)
        sr.touch_session("telegram:2", actor=EMP, now=20.0)
        out = sr.list_sessions()
        self.assertIn("telegram:2", out)
        self.assertNotIn("telegram:1", out)
        self.assertIn("大王自己的 1 個 session 未列", out)

    def test_list_include_owner(self):
        sr.touch_session("telegram:1", actor=OWNER)
        sr.touch_session("telegram:2", actor=EMP)
        out = sr.list_sessions(include_owner=True)
        self.assertIn("telegram:1", out)
        self.assertIn("telegram:2", out)

    def test_list_empty(self):
        self.assertIn("沒有任何活躍 session", sr.list_sessions())

    def test_list_only_owner_present(self):
        sr.touch_session("telegram:1", actor=OWNER)
        out = sr.list_sessions()   # 預設隱藏 owner
        self.assertIn("沒有其他人在對話", out)

    def test_list_sorted_by_last_seen_desc(self):
        sr.touch_session("telegram:a", actor=EMP, now=10.0)
        sr.touch_session("telegram:b", actor=EMP, now=99.0)
        out = sr.list_sessions()
        self.assertLess(out.index("telegram:b"), out.index("telegram:a"))

    # ── misc ─────────────────────────────────────────────────────────
    def test_paused_notice_does_not_leak_reason(self):
        notice = sr.paused_notice("內部理由：某員工亂問")
        self.assertNotIn("內部理由", notice)
        self.assertIn("暫停", notice)

    def test_get_and_is_paused_unknown(self):
        self.assertIsNone(sr.get_session("telegram:nope"))
        self.assertFalse(sr.is_paused("telegram:nope"))

    def test_corrupt_registry_file_treated_as_empty(self):
        with open(self._path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        # 不炸；當空處理
        self.assertEqual(sr.get_session("telegram:2"), None)
        r = sr.touch_session("telegram:2", actor=EMP)
        self.assertEqual(r["turn_count"], 1)


class LineSessionGateTests(_RegistryIsolation, unittest.TestCase):
    """LINE 通道（line_bot.build_line_employee_reply）的 registry 登記 + 暫停 gate。"""

    EMP = {"name": "陳採購", "email": "buy@f.com", "color": "yellow"}

    def _reply(self, text, uid, employee):
        from agent_core import line_bot
        with mock.patch.object(line_bot, "get_employee_by_line_user_id", return_value=employee):
            return line_bot.build_line_employee_reply(text, uid)

    def test_registers_line_session(self):
        self._reply("狀態", "U1", self.EMP)
        rec = sr.get_session("line:U1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["channel"], "line")
        self.assertFalse(rec["is_owner"])

    def test_paused_returns_notice(self):
        self._reply("狀態", "U1", self.EMP)          # 先登記
        sr.pause_session("line:U1", reason="LINE 先關")
        self.assertEqual(self._reply("狀態", "U1", self.EMP), sr.paused_notice())

    def test_resume_restores_normal_reply(self):
        self._reply("狀態", "U1", self.EMP)
        sr.pause_session("line:U1")
        sr.resume_session("line:U1")
        self.assertIn("小紅在線", self._reply("狀態", "U1", self.EMP))

    def test_unregistered_user_not_tracked(self):
        r = self._reply("hi", "Ux", None)
        self.assertIn("尚未登記", r)
        self.assertIsNone(sr.get_session("line:Ux"))

    def test_reset_flag_consumed(self):
        self._reply("狀態", "U1", self.EMP)
        sr.reset_session("line:U1")
        self._reply("狀態", "U1", self.EMP)   # 消費 reset 旗標
        self.assertFalse(sr.get_session("line:U1")["reset_pending"])


class WebSessionGateTests(_RegistryIsolation, unittest.TestCase):
    """Web portal 部門 API（app._session_gate_or_none）的登記 + 暫停 gate。"""

    EMP = {"email": "clerk@f.com", "name": "小職員", "color": "purple", "is_boss": "False"}
    BOSS = {"email": "boss@f.com", "name": "大王", "color": "red", "is_boss": "True"}

    def _gate(self, user):
        from agent_core.web_server.app import _session_gate_or_none
        return _session_gate_or_none(user)

    def test_active_employee_passes_and_registers(self):
        self.assertIsNone(self._gate(self.EMP))
        rec = sr.get_session("web:clerk@f.com")
        self.assertEqual(rec["channel"], "web")
        self.assertFalse(rec["is_owner"])

    def test_boss_recorded_as_owner_and_never_gated(self):
        self.assertIsNone(self._gate(self.BOSS))
        self.assertTrue(sr.get_session("web:boss@f.com")["is_owner"])
        # 連對 boss 下 pause 都被拒（owner 保護），gate 仍放行
        self.assertIn("禁止暫停", sr.pause_session("web:boss@f.com"))
        self.assertIsNone(self._gate(self.BOSS))

    def test_paused_employee_gets_423(self):
        self._gate(self.EMP)                     # 先登記
        sr.pause_session("web:clerk@f.com", reason="上班分心")
        resp = self._gate(self.EMP)
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 423)

    def test_no_email_is_not_gated(self):
        self.assertIsNone(self._gate({"email": "", "is_boss": "False"}))

    def test_reset_flag_consumed(self):
        self._gate(self.EMP)
        sr.reset_session("web:clerk@f.com")
        self._gate(self.EMP)
        self.assertFalse(sr.get_session("web:clerk@f.com")["reset_pending"])


class BroadcastRegistryTests(_RegistryIsolation, unittest.TestCase):
    """broadcast 的 registry 面：目標篩選 + web 佇列（不觸發 telegram/line 送達）。"""

    def _seed(self):
        sr.touch_session("telegram:100", channel="telegram",
                         actor={"is_owner": "false", "color": "blue", "name": "TG"})
        sr.touch_session("line:U9", channel="line",
                         actor={"is_owner": "false", "color": "blue", "name": "LINE"})
        sr.touch_session("web:c@f.com", channel="web",
                         actor={"is_owner": "false", "color": "purple", "name": "Web"})
        sr.touch_session("telegram:1", channel="telegram",
                         actor={"is_owner": "true", "color": "red", "name": "大王"})
        sr.touch_session("telegram:200", channel="telegram",
                         actor={"is_owner": "false", "color": "green", "name": "暫停"})
        sr.pause_session("telegram:200")

    def test_active_sessions_all_excludes_owner_and_paused(self):
        self._seed()
        ids = {r["session_id"] for r in sr.active_sessions("all")}
        self.assertEqual(ids, {"telegram:100", "line:U9", "web:c@f.com"})

    def test_active_sessions_by_color(self):
        self._seed()
        ids = {r["session_id"] for r in sr.active_sessions("blue")}
        self.assertEqual(ids, {"telegram:100", "line:U9"})

    def test_active_sessions_by_channel(self):
        self._seed()
        self.assertEqual({r["session_id"] for r in sr.active_sessions("web")}, {"web:c@f.com"})
        self.assertEqual({r["session_id"] for r in sr.active_sessions("line")}, {"line:U9"})

    def test_queue_and_drain_one_shot(self):
        sr.touch_session("web:c@f.com", channel="web", actor={"is_owner": "false"})
        self.assertTrue(sr.queue_broadcast("web:c@f.com", "停電通知"))
        self.assertEqual(sr.drain_broadcasts("web:c@f.com"), ["停電通知"])
        self.assertEqual(sr.drain_broadcasts("web:c@f.com"), [])   # one-shot

    def test_queue_caps_length(self):
        sr.touch_session("web:c@f.com", channel="web", actor={"is_owner": "false"})
        for i in range(30):
            sr.queue_broadcast("web:c@f.com", f"m{i}")
        drained = sr.drain_broadcasts("web:c@f.com")
        self.assertLessEqual(len(drained), 20)
        self.assertEqual(drained[-1], "m29")   # 保留最新

    def test_broadcast_web_only_queues(self):
        # 只有 web session → 不觸發 telegram/line 送達 import，純佇列
        sr.touch_session("web:c@f.com", channel="web",
                         actor={"is_owner": "false", "color": "purple"})
        out = sr.broadcast_message("倉庫提早關", target="web")
        self.assertIn("Web 佇列 1", out)
        self.assertEqual(sr.drain_broadcasts("web:c@f.com"), ["倉庫提早關"])

    def test_broadcast_empty_text_rejected(self):
        self.assertIn("不能為空", sr.broadcast_message("   "))

    def test_broadcast_no_matching_target(self):
        sr.touch_session("web:c@f.com", channel="web", actor={"is_owner": "false"})
        self.assertIn("沒有符合", sr.broadcast_message("x", target="black"))


if __name__ == "__main__":
    unittest.main()
