"""training_video_watch 每日單發 daemon：瞬時 blip 重試包裝 + plist 結構（免網路）。

回歸背景：2026-07-15 這支 daemon 在 token 刷新時撞到瞬時 DNS blip
（NameResolutionError → get_google_credentials daemon 模式硬退出），因它是每日
一次的 StartCalendarInterval，不像 cron 型 daemon 幾分鐘後自動重試 → 卡到隔天。
這裡鎖住「只對瞬時網路退避重試、對終局錯誤快速失敗」的分流行為。

2026-08-27 根因修好後語意收斂：google_auth 暫時性失敗改拋 GoogleAuthTransientError
（型別判定），而 OAuth 授權牆訊息**只**剩「憑證真失效／沒有 token」一種意思 → 不再
算瞬時、應快速失敗告警。
"""
import importlib.util
import plistlib
import unittest
from pathlib import Path

from agent_core.path_safety import _REPO_ROOT

_REPO = Path(_REPO_ROOT)
_RUNNER = _REPO / "launchd" / "scripts" / "training_video_watch.py"
_PLIST = _REPO / "launchd" / "templates" / "com.xiaohong.training_video_watch.plist"

# get_google_credentials 在 daemon 模式撞牆時實際 raise 的訊息（對齊 google_auth.py）
_OAUTH_WALL_MSG = "daemon 模式下無法啟動互動式 OAuth 授權，請先在前景完成登入"


def _transient_exc():
    """上游對「瞬時網路故障」的實際訊號（2026-08-27 起）。"""
    from agent_core.google_auth import GoogleAuthTransientError
    return GoogleAuthTransientError(
        "Google OAuth token 刷新遇暫時性網路故障：TransportError: "
        "Failed to resolve 'oauth2.googleapis.com'"
    )


def _load_runner():
    spec = importlib.util.spec_from_file_location("_tvw_runner", _RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)   # 只跑 top-level import；main 受 __name__ 守衛不會執行
    return mod


class IsTransientTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_runner()

    def test_google_auth_transient_error_is_transient_by_type(self):
        # 07-15 事故的實際成因（瞬時 DNS）現在由上游用專屬例外表達 → 型別判定，
        # 不再依賴訊息字串（訊息一改字就失效的那種脆弱耦合）。
        from agent_core.google_auth import GoogleAuthTransientError
        exc = GoogleAuthTransientError("刷新遇暫時性網路故障：nodename nor servname")
        self.assertTrue(self.mod._is_transient(exc))

    def test_oauth_wall_is_not_transient_anymore(self):
        # 根因修好後這道牆只剩「憑證真失效／根本沒 token」一種意思：重試無益，
        # 要快速失敗讓告警發出去，人才會真的去重新授權。
        self.assertFalse(self.mod._is_transient(RuntimeError(_OAUTH_WALL_MSG)))

    def test_transport_layer_signatures_are_transient(self):
        for exc in (
            RuntimeError("HTTPSConnectionPool(host='oauth2.googleapis.com'): "
                         "Max retries exceeded (NameResolutionError: Failed to resolve)"),
            OSError("[Errno 8] nodename nor servname provided (gaierror)"),
            TimeoutError("read operation timed out"),
            ConnectionResetError("Connection reset by peer"),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(self.mod._is_transient(exc))

    def test_terminal_errors_fail_fast(self):
        # 程式/資料類錯誤不是瞬時 blip：不該重試，讓它快速 exit 1 + 告警
        for exc in (ValueError("bad sheet layout"), KeyError("missing_field"),
                    AttributeError("NoneType has no attribute foo")):
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(self.mod._is_transient(exc))


class RetryLoopTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_runner()
        self.slept = []                      # 記錄退避秒數；同時當作「不真的睡」的替身

    def _sleep(self, s):
        self.slept.append(s)

    def test_success_first_try_no_sleep(self):
        calls = []

        def _fn():
            calls.append(1)
            return {"ok": True, "new_found": 0}

        res = self.mod._run_with_transient_retry(
            _fn, max_attempts=3, backoff_s=180, sleep=self._sleep)
        self.assertEqual(res, {"ok": True, "new_found": 0})
        self.assertEqual(len(calls), 1)      # 只跑一次
        self.assertEqual(self.slept, [])     # 沒有退避

    def test_transient_then_success_retries_and_backs_off(self):
        calls = []

        def _fn():
            calls.append(1)
            if len(calls) < 3:               # 前兩次瞬時失敗，第三次成功
                raise _transient_exc()
            return {"ok": True}

        res = self.mod._run_with_transient_retry(
            _fn, max_attempts=3, backoff_s=180, sleep=self._sleep)
        self.assertEqual(res, {"ok": True})
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.slept, [180, 180])   # 兩次退避

    def test_persistent_transient_exhausts_and_reraises(self):
        # 一直瞬時失敗 → 用盡次數後原樣拋出（保留 exit≠0 → Telegram 告警）
        calls = []

        def _fn():
            calls.append(1)
            raise _transient_exc()

        with self.assertRaises(RuntimeError):
            self.mod._run_with_transient_retry(
                _fn, max_attempts=3, backoff_s=180, sleep=self._sleep)
        self.assertEqual(len(calls), 3)            # 恰好嘗試 max_attempts 次
        self.assertEqual(self.slept, [180, 180])   # 最後一次失敗後不再睡

    def test_oauth_wall_fails_fast_without_retry(self):
        # 根因修好後這道牆＝憑證真失效／沒 token：重試只是延後告警，該一次就退。
        calls = []

        def _fn():
            calls.append(1)
            raise RuntimeError(_OAUTH_WALL_MSG)

        with self.assertRaises(RuntimeError):
            self.mod._run_with_transient_retry(
                _fn, max_attempts=3, backoff_s=180, sleep=self._sleep)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.slept, [])

    def test_terminal_error_fails_fast_without_retry(self):
        calls = []

        def _fn():
            calls.append(1)
            raise ValueError("corrupt production sheet")

        with self.assertRaises(ValueError):
            self.mod._run_with_transient_retry(
                _fn, max_attempts=3, backoff_s=180, sleep=self._sleep)
        self.assertEqual(len(calls), 1)      # 終局錯誤：只嘗試一次
        self.assertEqual(self.slept, [])     # 不退避


class PlistTests(unittest.TestCase):
    def setUp(self):
        self.data = plistlib.loads(_PLIST.read_bytes())

    def test_label_and_program(self):
        self.assertEqual(self.data["Label"], "com.xiaohong.training_video_watch")
        self.assertIn("training_video_watch.py", self.data["ProgramArguments"][1])

    def test_daily_calendar_1907(self):
        cal = self.data["StartCalendarInterval"]
        # 單一每日觸發（dict），非陣列——本測試也守住「維持每日一次、靠進程內重試自癒」
        self.assertIsInstance(cal, dict)
        self.assertEqual((cal["Hour"], cal["Minute"]), (19, 7))

    def test_no_run_at_load(self):
        # bootstrap/redeploy 時不可立刻跑一輪（RunAtLoad 隱含啟動觸發的坑）
        self.assertFalse(self.data.get("RunAtLoad", False))


if __name__ == "__main__":
    unittest.main()
