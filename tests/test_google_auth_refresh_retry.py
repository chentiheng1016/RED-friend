"""Regression tests for agent_core.google_auth._refresh_creds_with_retry.

OAuth token refresh 的失敗有**三種**結局，不是兩種：成功 / 暫時性網路故障 /
憑證永久失效。以前後兩者被摺疊成同一個「回 False」，呼叫端一律當成「需要人重新
互動授權」，於是一次網路 blip 就會丟掉一份好 creds、推一則假的「請重新登入」告警、
讓整輪失敗。實際踩過兩次：

  2026-07-15  training_video_watch（每日單發）撞 DNS blip → 卡到隔天。當時只在那
              一支 daemon 貼了字串比對的 OK 繃，根因沒動。
  2026-08-27  04:00 一次 DNS 斷線 → internal_ingest_daily exit 1，mailcheck /
              alert_check / email_ingest 同時噴數百行「重新進行 OAuth 授權」，
              而 token 從頭到尾都是好的。

這支測試鎖住分流：暫時性 → raise GoogleAuthTransientError（不通知、不丟 creds）；
永久失效 → 回 False，走原本的 fail-fast / 互動授權 + 告警。
"""
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from google.auth.exceptions import RefreshError, TransportError  # noqa: E402
from agent_core import google_auth  # noqa: E402

# 2026-08-27 04:00 從 daemon log 撈到的真實例外全文（macOS getaddrinfo 那條路徑）。
_REAL_DNS_FAILURE = (
    "HTTPSConnectionPool(host='oauth2.googleapis.com', port=443): Max retries "
    "exceeded with url: /token (Caused by NameResolutionError(\"HTTPSConnection"
    "(host='oauth2.googleapis.com', port=443): Failed to resolve "
    "'oauth2.googleapis.com' ([Errno 8] nodename nor servname provided, or not "
    "known)\"))"
)


class RefreshFailureClassifierTests(unittest.TestCase):
    """_refresh_failure_is_permanent：預設暫時性，只有明確證據才判永久。"""

    def test_real_dns_outage_is_not_permanent(self):
        # 這一條就是 08-27 的兇手；判成永久 = 冤枉一份好憑證。
        exc = TransportError(_REAL_DNS_FAILURE)
        self.assertFalse(google_auth._refresh_failure_is_permanent(exc))

    def test_transport_error_is_never_permanent(self):
        # TransportError ＝ 根本沒跟 token 端點講到話，不可能是憑證問題。
        # 連訊息完全認不出來的也一樣（舊分類器靠字串比對，這種會漏judge）。
        for msg in ("blip", "", "something nobody has seen before"):
            with self.subTest(msg=msg):
                self.assertFalse(
                    google_auth._refresh_failure_is_permanent(TransportError(msg))
                )

    def test_unknown_exception_type_defaults_to_transient(self):
        # 認不出來的一律當暫時性：誤判成永久要付假告警，誤判成暫時只是本輪失敗。
        self.assertFalse(
            google_auth._refresh_failure_is_permanent(RuntimeError("???"))
        )

    def test_wall_clock_timeout_is_transient(self):
        # 涓流/半死連線（2026-07-06 rag_sync 卡死那一種）是網路病不是憑證失效。
        exc = google_auth.RpcWallClockTimeout("oauth_refresh exceeded 60s")
        self.assertFalse(google_auth._refresh_failure_is_permanent(exc))

    def test_oauth_permanent_error_codes(self):
        for code in google_auth._PERMANENT_REFRESH_MARKS:
            with self.subTest(code=code):
                exc = RefreshError(f"('{code}: bad', {{'error': '{code}'}})")
                self.assertTrue(google_auth._refresh_failure_is_permanent(exc))

    def test_refresh_error_rejecting_credential_is_permanent(self):
        # 端點有回應且拒絕了憑證，又沒有暫時性特徵 → 有證據推翻預設。
        self.assertTrue(
            google_auth._refresh_failure_is_permanent(
                RefreshError("token endpoint said no")
            )
        )

    def test_refresh_error_with_server_side_5xx_is_transient(self):
        # 端點自己 5xx／逾時 ≠ 憑證有問題。
        for msg in ("503 Service Unavailable", "502 Bad Gateway",
                    "the read operation timed out"):
            with self.subTest(msg=msg):
                self.assertFalse(
                    google_auth._refresh_failure_is_permanent(RefreshError(msg))
                )


class RefreshRetryTests(unittest.TestCase):
    def _fake_creds(self, side_effect):
        creds = mock.Mock()
        creds.refresh.side_effect = side_effect
        return creds

    def test_succeeds_first_try(self):
        creds = self._fake_creds([None])
        with mock.patch.object(google_auth.time, "sleep") as sleep:
            self.assertTrue(google_auth._refresh_creds_with_retry(creds))
        self.assertEqual(creds.refresh.call_count, 1)
        sleep.assert_not_called()

    def test_retries_transient_then_succeeds(self):
        # 第一次 blip、第二次成功 → 不該放棄
        creds = self._fake_creds([TransportError("blip"), None])
        with mock.patch.object(google_auth.time, "sleep") as sleep:
            self.assertTrue(google_auth._refresh_creds_with_retry(creds))
        self.assertEqual(creds.refresh.call_count, 2)
        sleep.assert_called_once()

    def test_invalid_grant_gives_up_immediately(self):
        # 撤銷/過期的 token 重試無益：只試一次、不 sleep、回 False（＝永久失效）
        creds = self._fake_creds(
            RefreshError("invalid_grant: Token has been expired or revoked.")
        )
        with mock.patch.object(google_auth.time, "sleep") as sleep:
            self.assertFalse(google_auth._refresh_creds_with_retry(creds))
        self.assertEqual(creds.refresh.call_count, 1)
        sleep.assert_not_called()

    def test_persistent_transient_raises_instead_of_returning_false(self):
        # 08-27 的路徑：一直 DNS 失敗 → 重試到上限 → 拋暫時性例外，**不是**回 False。
        # 回 False 會被呼叫端讀成「憑證失效」→ 假告警。
        creds = self._fake_creds(TransportError(_REAL_DNS_FAILURE))
        with mock.patch.object(google_auth.time, "sleep"):
            with self.assertRaises(google_auth.GoogleAuthTransientError) as ctx:
                google_auth._refresh_creds_with_retry(creds)
        self.assertEqual(creds.refresh.call_count, google_auth._REFRESH_MAX_ATTEMPTS)
        # 原始例外要留在 __cause__，事後才追得到根因
        self.assertIsInstance(ctx.exception.__cause__, TransportError)

    def test_transient_error_is_recognised_by_the_shared_network_classifier(self):
        # 訊息帶原文 → retry_on_transient_network() 包住的呼叫端會自動退避重試。
        from agent_core.daemon_helpers import is_transient_network_error
        creds = self._fake_creds(TransportError(_REAL_DNS_FAILURE))
        with mock.patch.object(google_auth.time, "sleep"):
            with self.assertRaises(google_auth.GoogleAuthTransientError) as ctx:
                google_auth._refresh_creds_with_retry(creds)
        self.assertTrue(is_transient_network_error(ctx.exception))

    def test_wall_clock_timeout_raises_without_retry(self):
        # 涓流/半死 TLS 連線：refresh 卡住不返回。per-recv timeout 罩不住（每個 byte
        # 都重置它），要靠 run_rpc_with_timeout 的整體 wall-clock 上限中止 → 拋暫時性
        # 例外、且**不重試**（再起 worker 會與被遺棄那個撞非 thread-safe creds）。
        # 這正是 2026-07-06 卡死④：夜跑主執行緒卡在 refresh、沒有整體 timeout 兜住。
        gate = threading.Event()

        def hang(_request):
            gate.wait(30)  # 卡住直到斷言後才釋放（上限 30s 防測試邏輯錯時真掛死）

        creds = mock.Mock()
        creds.refresh.side_effect = hang
        try:
            with mock.patch.object(google_auth, "_REFRESH_ATTEMPT_TIMEOUT_S", 0.2), \
                 mock.patch.object(google_auth.time, "sleep") as sleep:
                with self.assertRaises(google_auth.GoogleAuthTransientError):
                    google_auth._refresh_creds_with_retry(creds)
            self.assertEqual(creds.refresh.call_count, 1)  # 逾時後不重試
            sleep.assert_not_called()  # 逾時路徑不 backoff-sleep
        finally:
            gate.set()  # 釋放被遺棄的 worker thread（gate 開在斷言之後）


class DaemonAlertRoutingTests(unittest.TestCase):
    """整條路走完：暫時性不准觸發「請重新授權」告警，永久失效才准。"""

    def setUp(self):
        self.ga = google_auth
        self.tmp = tempfile.mkdtemp(prefix="red_ga_transient_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        creds_path = os.path.join(self.tmp, "credentials.json")
        with open(creds_path, "w", encoding="utf-8") as f:
            f.write("{}")   # 存在即可，永久失效路徑才不會先炸 FileNotFoundError
        # 模組級 latch：不重設的話跨測試互相遮蔽（conftest 在 unittest 下不生效，
        # 隔離一律寫在 setUp/tearDown）。
        self.ga._daemon_oauth_alert_sent = False
        self.addCleanup(setattr, self.ga, "_daemon_oauth_alert_sent", False)
        expired = types.SimpleNamespace(
            valid=False, expired=True, refresh_token="r",
            to_json=lambda: "{}")

        class FakeCredentials:
            @staticmethod
            def from_authorized_user_info(info, scopes):
                return expired

        for p in (
            mock.patch.object(self.ga, "TOKEN_FILE",
                              os.path.join(self.tmp, "token.json")),
            mock.patch.object(self.ga, "CREDENTIALS_FILE", creds_path),
            mock.patch.object(self.ga, "_IS_DAEMON_MODE", True),
            mock.patch.object(self.ga, "get_secret",
                              return_value=types.SimpleNamespace(value='{"tok": 1}')),
            mock.patch.object(self.ga, "_get_google_oauth_classes",
                              return_value=(FakeCredentials, mock.Mock())),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_transient_failure_does_not_push_reauth_alert(self):
        """08-27 的核心回歸：DNS 斷線不該推「Google OAuth 失效／請重新登入」。"""
        transient = self.ga.GoogleAuthTransientError(_REAL_DNS_FAILURE)
        with mock.patch.object(self.ga, "_refresh_creds_with_retry",
                               side_effect=transient), \
             mock.patch.object(self.ga, "_notify_daemon_oauth_blocked") as notify:
            with self.assertRaises(self.ga.GoogleAuthTransientError):
                self.ga.get_google_credentials()
        notify.assert_not_called()

    def test_transient_failure_releases_the_token_lock(self):
        """例外從鎖內往上拋 → 鎖必須放掉，否則下一輪整艦隊卡到看門狗。"""
        transient = self.ga.GoogleAuthTransientError(_REAL_DNS_FAILURE)
        with mock.patch.object(self.ga, "_refresh_creds_with_retry",
                               side_effect=transient), \
             mock.patch.object(self.ga, "_notify_daemon_oauth_blocked"):
            with self.assertRaises(self.ga.GoogleAuthTransientError):
                self.ga.get_google_credentials()
        acquired = self.ga._token_lock.acquire(timeout=1)
        if acquired:
            self.ga._token_lock.release()
        self.assertTrue(acquired, "_token_lock 未釋放")

    def test_permanent_failure_still_pushes_reauth_alert(self):
        """憑證真的失效時，原本的 fail-fast + 告警行為一字不動。"""
        with mock.patch.object(self.ga, "_refresh_creds_with_retry",
                               return_value=False), \
             mock.patch.object(self.ga, "_notify_daemon_oauth_blocked") as notify:
            with self.assertRaisesRegex(
                    RuntimeError, "daemon 模式下無法啟動互動式 OAuth 授權"):
                self.ga.get_google_credentials()
        notify.assert_called_once()

    def test_transient_is_not_mistaken_for_the_oauth_wall(self):
        """暫時性例外不可以長得像 OAuth 牆訊息，否則下游又要靠字串猜。"""
        transient = self.ga.GoogleAuthTransientError(_REAL_DNS_FAILURE)
        self.assertNotIn("無法啟動互動式", str(transient))


if __name__ == "__main__":
    unittest.main()
