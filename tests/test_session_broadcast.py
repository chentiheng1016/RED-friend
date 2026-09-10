"""broadcast_message 送達整合測試（Telegram/LINE push + Web 開頁公告）。

獨立檔：會 import daemon_telegram / line_bot / web app（較重），讓
test_session_registry.py 專注在純 registry 邏輯。送達管道全部 mock，不打外網。
"""
import os
import tempfile
import unittest
from unittest import mock

from agent_core import session_registry as sr


class _RegistryIsolation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("RED_SESSION_REGISTRY_FILE")
        os.environ["RED_SESSION_REGISTRY_FILE"] = os.path.join(self._tmp.name, "reg.json")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("RED_SESSION_REGISTRY_FILE", None)
        else:
            os.environ["RED_SESSION_REGISTRY_FILE"] = self._prev
        self._tmp.cleanup()


class BroadcastFanoutTests(_RegistryIsolation):
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

    def _run(self, target="all"):
        import agent_core.daemon_telegram as dt
        import agent_core.line_bot as lb
        self.tg, self.line = [], []
        with mock.patch.object(dt, "tg_get_token_and_chat", return_value=("TOK", "1")), \
                mock.patch.object(dt, "tg_send",
                                  side_effect=lambda tok, cid, txt: self.tg.append(cid) or True), \
                mock.patch.object(lb, "line_push",
                                  side_effect=lambda uid, txt, **k: (self.line.append(uid), (True, ""))[1]):
            return sr.broadcast_message("倉庫今天提早關", target=target)

    def test_all_fans_out_by_channel_excluding_owner_and_paused(self):
        self._seed()
        out = self._run("all")
        self.assertEqual(self.tg, ["100"])          # 只有活躍非 owner 的 tg
        self.assertEqual(self.line, ["U9"])
        self.assertEqual(sr.drain_broadcasts("web:c@f.com"), ["倉庫今天提早關"])
        self.assertNotIn("200", self.tg)            # paused 排除
        self.assertNotIn("1", self.tg)              # owner 排除
        self.assertIn("Telegram 送達 1", out)
        self.assertIn("LINE 送達 1", out)
        self.assertIn("Web 佇列 1", out)

    def test_target_color_filters(self):
        self._seed()
        self._run("blue")                            # 只有 blue：tg100 + lineU9
        self.assertEqual(self.tg, ["100"])
        self.assertEqual(self.line, ["U9"])
        self.assertEqual(sr.drain_broadcasts("web:c@f.com"), [])  # purple 的 web 沒收到

    def test_delivery_failure_counted(self):
        sr.touch_session("telegram:100", channel="telegram", actor={"is_owner": "false"})
        import agent_core.daemon_telegram as dt
        with mock.patch.object(dt, "tg_get_token_and_chat", return_value=("TOK", "1")), \
                mock.patch.object(dt, "tg_send", return_value=False):   # 送達失敗
            out = sr.broadcast_message("x", target="telegram")
        self.assertIn("失敗 1", out)


class WebBroadcastBannerTests(unittest.TestCase):
    """Web 端「下次開部門聊天頁顯示公告」：dept_page drain + 模板 banner。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("RED_SESSION_REGISTRY_FILE")
        os.environ["RED_SESSION_REGISTRY_FILE"] = os.path.join(self._tmp.name, "reg.json")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("RED_SESSION_REGISTRY_FILE", None)
        else:
            os.environ["RED_SESSION_REGISTRY_FILE"] = self._prev
        self._tmp.cleanup()

    def _client(self, user):
        from fastapi.testclient import TestClient
        from agent_core.web_server import app as app_module
        patches = [
            mock.patch.object(app_module, "get_secret_key", return_value="x" * 32),
            mock.patch.object(app_module, "_get_session_user", return_value=user),
            mock.patch.object(app_module, "get_employee", return_value={
                "email": user["email"], "name": user["name"], "color": user["color"],
            }),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        return TestClient(app_module.app)

    def test_dept_page_shows_then_drains_broadcast(self):
        user = {"email": "clerk@f.com", "name": "小職員", "color": "green", "is_boss": "False"}
        # 先讓這位員工有個 web session，再排一則廣播
        sr.touch_session("web:clerk@f.com", channel="web",
                         actor={"is_owner": "false", "color": "green", "name": "小職員"})
        sr.queue_broadcast("web:clerk@f.com", "系統今晚維護")
        client = self._client(user)

        r1 = client.get("/dept/green")
        self.assertEqual(r1.status_code, 200)
        self.assertIn("系統今晚維護", r1.text)          # 開頁看到公告

        r2 = client.get("/dept/green")
        self.assertEqual(r2.status_code, 200)
        self.assertNotIn("系統今晚維護", r2.text)       # one-shot：再開頁已清掉


if __name__ == "__main__":
    unittest.main()
