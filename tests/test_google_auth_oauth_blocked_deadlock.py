"""Regression test for the daemon-mode OAuth-wall self-deadlock in
agent_core.google_auth.get_google_credentials.

事故：factory_warehouse_rebuild exit 75（run_with_deadline 1200s 看門狗）。真因不是
「File Lock」，而是 get_google_credentials() 撞到 daemon 互動式 OAuth 牆時，在還握著
非重入的 _token_lock 的情況下呼叫 _notify_daemon_oauth_blocked()，其 email fallback
會 send_gmail → get_service → 再進 get_google_credentials() → 重入想再抓同一把鎖 →
同一 thread 自我死鎖，卡到 1200s 才被砍。修法：把 notify + raise 移到鎖外。

這支測試用「notify 會重入 get_google_credentials()」模擬 email fallback，整段放進
worker thread + join(timeout)：修復前會卡死 → join 逾時 → 測試失敗；修復後重入能拿到
鎖、走到 RuntimeError 並在時限內收斂。所有 patch 都在主執行緒做（worker 只呼叫、不
patch），避免跨測試的 mock 洩漏。
"""
import os
import sys
import threading
import unittest
from unittest import mock

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import google_auth  # noqa: E402


class OAuthBlockedDeadlockTests(unittest.TestCase):
    def test_daemon_oauth_block_notify_does_not_deadlock_on_token_lock(self):
        ga = google_auth
        reentry = []  # 記重入是否發生、結果為何

        def fake_notify(*args, **kwargs):
            # 模擬 email fallback：通知時重入 get_google_credentials()。真實程式靠
            # _daemon_oauth_alert_sent latch 只重入一次，這裡用 reentry 旗標等效擋住。
            if reentry:
                return
            reentry.append("reentered")
            try:
                ga.get_google_credentials()
            except RuntimeError:
                reentry.append("RuntimeError")

        fake_secret = mock.Mock()
        fake_secret.value = ""  # 沒有 token_json → 落到 TOKEN_FILE 檢查

        outcome = {}

        def run():
            try:
                ga.get_google_credentials()
            except BaseException as exc:  # noqa: BLE001
                outcome["exc"] = exc

        with mock.patch.object(ga, "_IS_DAEMON_MODE", True), \
             mock.patch.object(ga, "_get_google_oauth_classes",
                               return_value=(object(), object())), \
             mock.patch.object(ga, "get_secret", return_value=fake_secret), \
             mock.patch.object(ga, "TOKEN_FILE", "/nonexistent/red-test/token.json"), \
             mock.patch.object(ga, "CREDENTIALS_FILE", os.path.abspath(__file__)), \
             mock.patch.object(ga, "_notify_daemon_oauth_blocked",
                               side_effect=fake_notify):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            worker.join(timeout=10)
            deadlocked = worker.is_alive()

        self.assertFalse(
            deadlocked,
            "get_google_credentials() 在 daemon OAuth 牆 + 重入通知下死鎖（未在時限內返回）",
        )
        self.assertIsInstance(outcome.get("exc"), RuntimeError)
        # 確認真的有走到「重入 + 重入也乾淨地 raise」這條路，而非測試早退。
        self.assertEqual(reentry, ["reentered", "RuntimeError"])


if __name__ == "__main__":
    unittest.main()
