"""Tests for the quota-retry + timeout wrapper around _gemini_embed.

The previous production sync stalled for 3.5h with TCP CLOSE_WAIT and 0% CPU
because the Gemini SDK's call hung on a half-closed socket. Without these
guards, any network blip turns daily syncs into permanent zombies.
"""
from __future__ import annotations

import concurrent.futures
import os
import sys
import threading
import time
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── _is_quota_error classifier ──────────────────────────────────────

class IsQuotaErrorTests(unittest.TestCase):
    def test_recognises_resource_exhausted_class_name(self):
        from agent_core.ingest import vector_store
        # google.api_core.exceptions.ResourceExhausted shape
        ResourceExhausted = type("ResourceExhausted", (Exception,), {})
        self.assertTrue(vector_store._is_quota_error(ResourceExhausted("...")))

    def test_recognises_rate_limit_class_name(self):
        from agent_core.ingest import vector_store
        RateLimitError = type("RateLimitError", (Exception,), {})
        self.assertTrue(vector_store._is_quota_error(RateLimitError("...")))

    def test_recognises_429_in_message(self):
        from agent_core.ingest import vector_store
        # Generic ClientError wrapping a 429 — the genai SDK does this.
        self.assertTrue(vector_store._is_quota_error(
            Exception("ClientError: 429 RESOURCE_EXHAUSTED Quota exceeded for...")
        ))

    def test_recognises_quota_keyword(self):
        from agent_core.ingest import vector_store
        self.assertTrue(vector_store._is_quota_error(
            Exception("Per-minute quota exceeded; please retry after 60s")
        ))

    def test_does_not_misclassify_unrelated_error(self):
        from agent_core.ingest import vector_store
        self.assertFalse(vector_store._is_quota_error(ValueError("bad input")))
        self.assertFalse(vector_store._is_quota_error(KeyError("missing")))
        # Don't false-positive on 'request' or 'limit' alone.
        self.assertFalse(vector_store._is_quota_error(Exception("connection refused")))


class IsHardQuotaErrorTests(unittest.TestCase):
    def test_recognises_monthly_spending_cap(self):
        from agent_core.ingest import vector_store

        self.assertTrue(vector_store._is_hard_quota_error(
            Exception("429 RESOURCE_EXHAUSTED: project exceeded its monthly spending cap")
        ))

    def test_does_not_treat_burst_quota_as_hard_cap(self):
        from agent_core.ingest import vector_store

        self.assertFalse(vector_store._is_hard_quota_error(
            Exception("Per-minute quota exceeded; please retry after 60s")
        ))

    def test_recognises_prepayment_credits_depleted(self):
        """2026-07-23 事故：prepay 429 曾被當暫時性重試 5 次/批，夜跑拖過
        6h wall-clock、KeepAlive 無限重跑。matcher 現在委派 gemini_client。"""
        from agent_core.ingest import vector_store

        self.assertTrue(vector_store._is_hard_quota_error(Exception(
            "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': "
            "'Your prepayment credits are depleted. Please go to AI Studio "
            "to purchase more credits.'}}"
        )))

    def test_drive_sync_matcher_recognises_prepayment_too(self):
        """drive_sync 的同名複本也委派同一來源，不得再漂移。"""
        from agent_core.ingest import drive_sync

        self.assertTrue(drive_sync._is_hard_quota_error(
            Exception("429: Your prepayment credits are depleted.")
        ))
        self.assertFalse(drive_sync._is_hard_quota_error(
            Exception("Per-minute quota exceeded; please retry after 60s")
        ))


# ── _gemini_embed_with_timeout ──────────────────────────────────────

class TimeoutWrapperTests(unittest.TestCase):
    def test_fast_call_returns_value(self):
        from agent_core.ingest import vector_store
        with mock.patch.object(vector_store, "_gemini_embed_raw", return_value=[[0.1, 0.2]]):
            result = vector_store._gemini_embed_with_timeout(["hi"], "RETRIEVAL_DOCUMENT")
        self.assertEqual(result, [[0.1, 0.2]])

    def test_hanging_call_raises_timeout_error(self):
        """Verify the embed wrapper aborts a hung call instead of waiting forever.
        This is the core guarantee that prevents CLOSE_WAIT zombies."""
        from agent_core.ingest import vector_store

        # Force the deadline very low so the test runs quickly.
        with mock.patch.object(vector_store, "_EMBED_TIMEOUT_S", 0.1):
            never_returns = threading.Event()
            self.addCleanup(never_returns.set)  # release the leaked thread when test exits

            def hang(*args, **kwargs):
                never_returns.wait(timeout=10)  # blocks; timeout safety net
                return [[0.0]]

            with mock.patch.object(vector_store, "_gemini_embed_raw", side_effect=hang):
                with self.assertRaises(concurrent.futures.TimeoutError):
                    vector_store._gemini_embed_with_timeout(["hi"], "RETRIEVAL_DOCUMENT")

    def test_inner_exception_propagates(self):
        from agent_core.ingest import vector_store
        with mock.patch.object(vector_store, "_gemini_embed_raw", side_effect=ValueError("xyz")):
            with self.assertRaises(ValueError) as cm:
                vector_store._gemini_embed_with_timeout(["hi"], "RETRIEVAL_DOCUMENT")
        self.assertIn("xyz", str(cm.exception))


# ── _backoff_sleep_s ─────────────────────────────────────────────────

class BackoffSleepTests(unittest.TestCase):
    def test_exponential_growth_below_cap(self):
        from agent_core.ingest import vector_store
        with mock.patch.object(vector_store.random, "uniform", return_value=1.0):
            self.assertEqual(vector_store._backoff_sleep_s(0), vector_store._RETRY_BASE_SLEEP_S)
            self.assertEqual(vector_store._backoff_sleep_s(1), vector_store._RETRY_BASE_SLEEP_S * 2)
            self.assertEqual(vector_store._backoff_sleep_s(2), vector_store._RETRY_BASE_SLEEP_S * 4)

    def test_caps_at_max_sleep_for_high_attempt_numbers(self):
        """A long retry storm must not blow past _RETRY_MAX_SLEEP_S — otherwise
        a handful of extra attempts could overrun RAG_CHROMA_OP_TIMEOUT_S and
        get the whole op aborted instead of just the individual embed call."""
        from agent_core.ingest import vector_store
        with mock.patch.object(vector_store.random, "uniform", return_value=1.0):
            self.assertEqual(vector_store._backoff_sleep_s(10), vector_store._RETRY_MAX_SLEEP_S)

    def test_jitter_stays_within_plus_minus_50_percent(self):
        from agent_core.ingest import vector_store
        for _ in range(200):
            sleep_s = vector_store._backoff_sleep_s(1)
            base = vector_store._RETRY_BASE_SLEEP_S * 2
            self.assertGreaterEqual(sleep_s, base * 0.5)
            self.assertLessEqual(sleep_s, base * 1.5)


# ── _gemini_embed retry layer ───────────────────────────────────────

class RetryLayerTests(unittest.TestCase):
    def setUp(self):
        from agent_core.ingest import vector_store

        vector_store._clear_embedding_hard_quota_for_tests()
        self.addCleanup(vector_store._clear_embedding_hard_quota_for_tests)
        # No real waits in tests — patch time.sleep where it's used.
        self._sleep_patch = mock.patch("agent_core.ingest.vector_store.time.sleep")
        self._sleep = self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)
        # Neutralize jitter so backoff assertions stay exact (uniform(0.5, 1.5) -> 1.0).
        self._jitter_patch = mock.patch(
            "agent_core.ingest.vector_store.random.uniform", return_value=1.0
        )
        self._jitter_patch.start()
        self.addCleanup(self._jitter_patch.stop)

    def test_succeeds_on_first_attempt_does_not_sleep(self):
        from agent_core.ingest import vector_store
        with mock.patch.object(vector_store, "_gemini_embed_with_timeout",
                               return_value=[[0.1]]) as call:
            result = vector_store._gemini_embed(["hi"])
        self.assertEqual(result, [[0.1]])
        call.assert_called_once()
        self._sleep.assert_not_called()

    def test_retries_on_quota_burst_then_succeeds(self):
        from agent_core.ingest import vector_store
        ResourceExhausted = type("ResourceExhausted", (Exception,), {})
        side_effects = [
            ResourceExhausted("burst 1"),
            ResourceExhausted("burst 2"),
            [[0.1]],
        ]
        with mock.patch.object(vector_store, "_gemini_embed_with_timeout",
                               side_effect=side_effects) as call:
            result = vector_store._gemini_embed(["hi"])
        self.assertEqual(result, [[0.1]])
        self.assertEqual(call.call_count, 3)
        # Two backoff sleeps — exponential per attempt.
        self.assertEqual(self._sleep.call_count, 2)
        sleeps = [c.args[0] for c in self._sleep.call_args_list]
        self.assertEqual(sleeps, [vector_store._RETRY_BASE_SLEEP_S,
                                   vector_store._RETRY_BASE_SLEEP_S * 2])

    def test_retries_on_timeout_then_succeeds(self):
        from agent_core.ingest import vector_store
        with mock.patch.object(vector_store, "_gemini_embed_with_timeout",
                               side_effect=[concurrent.futures.TimeoutError("hang"),
                                            [[0.1]]]) as call:
            result = vector_store._gemini_embed(["hi"])
        self.assertEqual(result, [[0.1]])
        self.assertEqual(call.call_count, 2)
        self._sleep.assert_called_once_with(vector_store._RETRY_BASE_SLEEP_S)

    def test_does_not_retry_on_non_quota_exception(self):
        """Real bugs (KeyError, ValueError, RuntimeError on missing embeddings)
        must bubble immediately — retry would just hide the bug."""
        from agent_core.ingest import vector_store
        with mock.patch.object(vector_store, "_gemini_embed_with_timeout",
                               side_effect=ValueError("real bug")) as call:
            with self.assertRaises(ValueError):
                vector_store._gemini_embed(["hi"])
        call.assert_called_once()
        self._sleep.assert_not_called()

    def test_quota_retry_budget_exhausted_re_raises_last(self):
        from agent_core.ingest import vector_store
        ResourceExhausted = type("ResourceExhausted", (Exception,), {})
        with mock.patch.object(vector_store, "_gemini_embed_with_timeout",
                               side_effect=ResourceExhausted("forever")) as call:
            with self.assertRaises(ResourceExhausted) as cm:
                vector_store._gemini_embed(["hi"])
        self.assertEqual(call.call_count, vector_store._RETRY_MAX_ATTEMPTS)
        self.assertEqual(
            self._sleep.call_count,
            max(0, vector_store._RETRY_MAX_ATTEMPTS - 1),
        )
        self.assertIn("forever", str(cm.exception))

    def test_hard_quota_latches_and_skips_future_calls(self):
        from agent_core.ingest import vector_store

        ResourceExhausted = type("ResourceExhausted", (Exception,), {})
        hard_cap = ResourceExhausted(
            "429 RESOURCE_EXHAUSTED: Your project has exceeded its monthly spending cap"
        )
        with mock.patch.object(vector_store, "_gemini_embed_with_timeout",
                               side_effect=hard_cap) as call:
            with self.assertRaises(vector_store.GeminiHardQuotaError):
                vector_store._gemini_embed(["hi"])
        call.assert_called_once()
        self._sleep.assert_not_called()
        self.assertIn("spending cap", vector_store.get_embedding_hard_quota_message())

        with mock.patch.object(vector_store, "_gemini_embed_with_timeout",
                               return_value=[[0.1]]) as call:
            with self.assertRaises(vector_store.GeminiHardQuotaError):
                vector_store._gemini_embed(["hi"])
        call.assert_not_called()

    def test_prepayment_depleted_latches_first_attempt_no_backoff(self):
        """prepay 429 必須第一擊就鎖斷路器：不 sleep、不重試（每批 5 次
        backoff 合計數分鐘 × 數百批 = 整輪拖過 6h 硬上限的元凶）。"""
        from agent_core.ingest import vector_store

        depleted = Exception(
            "ClientError: 429 RESOURCE_EXHAUSTED. "
            "'Your prepayment credits are depleted.'"
        )
        with mock.patch.object(vector_store, "_gemini_embed_with_timeout",
                               side_effect=depleted) as call:
            with self.assertRaises(vector_store.GeminiHardQuotaError):
                vector_store._gemini_embed(["hi"])
        call.assert_called_once()
        self._sleep.assert_not_called()
        self.assertIn(
            "prepayment", vector_store.get_embedding_hard_quota_message().lower()
        )


class StaleCollectionHandleTests(unittest.TestCase):
    def test_stale_collection_error_is_classified_narrowly(self):
        from agent_core.ingest import vector_store

        self.assertTrue(vector_store._is_stale_collection_error(
            RuntimeError("Collection [abc] does not exist.")
        ))
        self.assertFalse(vector_store._is_stale_collection_error(
            RuntimeError("Error loading hnsw index")
        ))

    def test_operation_refreshes_collection_once(self):
        from agent_core.ingest import vector_store

        store = vector_store.VectorStore.__new__(vector_store.VectorStore)
        store._collection_name = "drive_docs"
        stale_col = mock.MagicMock()
        fresh_col = mock.MagicMock()
        stale_col.count.side_effect = RuntimeError("Collection [old] does not exist.")
        fresh_col.count.return_value = 7
        store._col = stale_col

        # count() has a SQL fast path that bypasses _with_collection when a
        # metadata segment id is available; force the slow path so this test
        # exercises the stale-handle refresh mechanism it's named after.
        with mock.patch.object(store, "_open_collection", return_value=fresh_col), \
             mock.patch.object(store, "_load_metadata_segment_id", return_value=""):
            self.assertEqual(store.count(), 7)

        self.assertIs(store._col, fresh_col)
        fresh_col.count.assert_called_once()


if __name__ == "__main__":
    unittest.main()


# ── embed HTTP timeout must undercut the thread belt ────────────────

class EmbedHttpTimeoutOrderingTests(unittest.TestCase):
    """2026-08-03 卡死案：belt(45s) 在保護一個容許 600s 的 HTTP 呼叫。

    順序反了 ⇒ 每次 >45s 的 embed，belt 先放棄、被放生的 daemon thread 仍抓著
    httpx 連線再跑最多 600s（Python 殺不掉 thread）⇒ CLOSE_WAIT 累積到整輪 wedge。
    正解是 SDK 自己先死、belt 只當保險，所以 HTTP 逾時必須**嚴格短於** belt。
    """

    def test_http_timeout_strictly_under_belt(self):
        from agent_core.ingest import vector_store
        self.assertLess(
            vector_store._EMBED_HTTP_TIMEOUT_S,
            vector_store._EMBED_TIMEOUT_S,
            "embed 的 HTTP 逾時必須短於 thread belt，否則 belt 每次放棄都在洩漏連線",
        )

    def test_raw_call_passes_http_timeout_in_config(self):
        """裸呼叫要把 per-request http_options 帶進 EmbedContentConfig。

        不能改全域 client 的逾時——那條 600s 是 generation 長輸出在用的。
        """
        from agent_core.ingest import vector_store

        captured = {}

        class _FakeModels:
            def embed_content(self, *, model, contents, config):
                captured["config"] = config
                return mock.Mock(embeddings=[mock.Mock(values=[0.1, 0.2])])

        class _FakeClient:
            models = _FakeModels()

        class _FakeTypes:
            @staticmethod
            def EmbedContentConfig(**kw):
                return kw

        with mock.patch("agent_core.gemini_client._get_embed_client",
                        return_value=_FakeClient()), \
             mock.patch("agent_core.gemini_client._get_genai_types",
                        return_value=_FakeTypes()), \
             mock.patch.object(vector_store, "embed_config_extra", return_value={}), \
             mock.patch.object(vector_store, "_record_embed_cost", create=True):
            try:
                vector_store._gemini_embed_raw(["hello"], "RETRIEVAL_DOCUMENT")
            except Exception:
                pass  # 記帳/回傳格式非本測試重點，只驗 config 帶了什麼

        cfg = captured.get("config")
        self.assertIsNotNone(cfg, "embed_content 沒被呼叫到")
        self.assertIn("http_options", cfg)
        self.assertEqual(
            cfg["http_options"]["timeout"],
            vector_store._EMBED_HTTP_TIMEOUT_S * 1000,
            "http_options.timeout 單位是毫秒",
        )

    def test_belt_timeout_message_reports_leak_count(self):
        """belt 真放生 thread 時要把累積洩漏數印出來（下次 wedge 的第一手線索）。"""
        from agent_core.ingest import vector_store

        gate = threading.Event()
        self.addCleanup(gate.set)   # 收尾放行，別留住 daemon thread

        def _hang(*_a, **_k):
            gate.wait(30)
            return []

        with mock.patch.object(vector_store, "_gemini_embed_raw", _hang), \
             mock.patch.object(vector_store, "_EMBED_TIMEOUT_S", 0.05):
            before = vector_store._EMBED_LEAKED_THREADS
            with self.assertRaises(concurrent.futures.TimeoutError) as ctx:
                vector_store._gemini_embed_with_timeout(["x"], "RETRIEVAL_DOCUMENT")
        self.assertIn("累積洩漏 thread", str(ctx.exception))
        self.assertEqual(vector_store._EMBED_LEAKED_THREADS, before + 1)


# ── httpx 傳輸逾時要重試，不能整批失敗 ─────────────────────────────

class EmbedHttpxTimeoutRetryTests(unittest.TestCase):
    """PR #340 把 embed HTTP 逾時縮到 40s 讓 SDK 先死（不再洩漏 thread），
    但 httpx 的逾時**不繼承** TimeoutError ⇒ 會掉進「非配額錯誤」那條直接 raise，
    把「慢一次重試就過」變成「整批失敗」。這裡把重試補回來。

    ⚠️ Python 3.11 起 concurrent.futures.TimeoutError / socket.timeout 都是內建
    TimeoutError 的別名，本來就被第一個 except 接住——真正的漏網之魚只有 httpx。
    """

    def test_stdlib_timeout_aliases_are_the_builtin(self):
        # 這個前提一旦被 Python 改掉，下面的分支判斷就要重新檢查
        self.assertIs(concurrent.futures.TimeoutError, TimeoutError)
        import socket
        self.assertIs(socket.timeout, TimeoutError)

    def test_httpx_timeout_is_not_builtin_timeout(self):
        import httpx
        self.assertFalse(issubclass(httpx.TimeoutException, TimeoutError))

    def test_is_timeout_error_recognises_httpx_variants(self):
        import httpx
        from agent_core.ingest import vector_store
        for exc in (httpx.ReadTimeout("x"), httpx.ConnectTimeout("x"),
                    httpx.PoolTimeout("x"), httpx.WriteTimeout("x")):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(vector_store._is_timeout_error(exc))

    def test_is_timeout_error_sees_through_wrapping(self):
        """genai SDK 可能把 httpx 例外包成自己的型別 → 查 __cause__ 鏈。"""
        import httpx
        from agent_core.ingest import vector_store
        try:
            try:
                raise httpx.ReadTimeout("upstream timed out")
            except httpx.ReadTimeout as inner:
                raise RuntimeError("APIError: request failed") from inner
        except RuntimeError as wrapped:
            self.assertTrue(vector_store._is_timeout_error(wrapped))

    def test_is_timeout_error_does_not_swallow_real_bugs(self):
        from agent_core.ingest import vector_store
        for exc in (ValueError("bad dimension"), KeyError("embeddings"),
                    RuntimeError("model not found")):
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(vector_store._is_timeout_error(exc))

    def test_httpx_timeout_is_retried_then_succeeds(self):
        """第一次 httpx 逾時 → 退避重試 → 第二次成功，不該讓整批失敗。"""
        from agent_core.ingest import vector_store
        import httpx

        calls = {"n": 0}

        def _flaky(texts, task_type):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("timed out")
            return [[0.1, 0.2]]

        with mock.patch.object(vector_store, "_gemini_embed_with_timeout", _flaky), \
             mock.patch.object(vector_store, "_backoff_sleep_s", return_value=0), \
             mock.patch.object(vector_store, "raise_if_embedding_hard_quota",
                               lambda: None):
            out = vector_store._gemini_embed(["hello"])

        self.assertEqual(out, [[0.1, 0.2]])
        self.assertEqual(calls["n"], 2, "httpx 逾時應該有被重試一次")

    def test_non_timeout_still_bubbles_immediately(self):
        """真 bug 不該被重試層蓋掉——維持原本「立刻拋」的語義。"""
        from agent_core.ingest import vector_store

        calls = {"n": 0}

        def _boom(texts, task_type):
            calls["n"] += 1
            raise ValueError("bad dimension")

        with mock.patch.object(vector_store, "_gemini_embed_with_timeout", _boom), \
             mock.patch.object(vector_store, "raise_if_embedding_hard_quota",
                               lambda: None):
            with self.assertRaises(ValueError):
                vector_store._gemini_embed(["hello"])
        self.assertEqual(calls["n"], 1, "非逾時例外不該重試")

    def test_quota_classified_before_timeout(self):
        """訊息同時像配額又像逾時 → 必須走配額路徑（硬配額短路不可被繞過）。

        2026-07-23 事故：預付燒乾的 429 被當暫時性錯誤重試 5 次/批，夜跑被拖過
        6h wall-clock 上限、KeepAlive 無限重跑、連鎖壓垮 chroma。
        """
        from agent_core.ingest import vector_store

        both = Exception("429 rate limit exceeded; retry after timeout period")
        # 前提：這個訊息確實兩個分類器都命中，測試才有意義
        self.assertTrue(vector_store._is_quota_error(both))
        self.assertTrue(vector_store._is_timeout_error(both))

        def _raise_both(texts, task_type):
            raise both

        with mock.patch.object(vector_store, "_gemini_embed_with_timeout",
                               _raise_both), \
             mock.patch.object(vector_store, "_is_hard_quota_error",
                               return_value=True), \
             mock.patch.object(vector_store, "raise_if_embedding_hard_quota",
                               lambda: None), \
             mock.patch.object(vector_store, "_latch_hard_quota",
                               return_value="prepay depleted"):
            with self.assertRaises(vector_store.GeminiHardQuotaError):
                vector_store._gemini_embed(["hello"])
