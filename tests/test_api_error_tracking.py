"""Tests for cost_tracker's external-API error tracking.

record_api_error appends failed API round-trips to api_errors.jsonl;
api_error_stats computes the error rate against cost.jsonl successes within a
time window. Both feed the external-API error-rate red-line in dashboard_alerts.

The module-global log paths (_API_ERROR_LOG / _COST_LOG) are redirected to a
tmp dir in setUp so the tests never touch the live 23MB cost.jsonl.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

from agent_core import cost_tracker


class ApiErrorTrackingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._err_log = os.path.join(self._tmp.name, "api_errors.jsonl")
        self._cost_log = os.path.join(self._tmp.name, "cost.jsonl")
        for attr, path in (("_API_ERROR_LOG", self._err_log),
                           ("_COST_LOG", self._cost_log)):
            p = mock.patch.object(cost_tracker, attr, path)
            p.start()
            self.addCleanup(p.stop)

    def _write_successes(
        self,
        n: int,
        ts: str | None = None,
        *,
        model: str = "gemini-3-flash-preview",
    ) -> None:
        ts = ts or datetime.now().isoformat(timespec="seconds")
        with open(self._cost_log, "a", encoding="utf-8") as f:
            for _ in range(n):
                f.write(json.dumps({
                    "ts": ts, "cost_usd": 0.001, "model": model,
                    "prompt_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                }) + "\n")

    def test_record_then_count(self):
        cost_tracker.record_api_error("gemini", "503", model="m")
        cost_tracker.record_api_error("gemini", "429")
        stats = cost_tracker.api_error_stats(hours=6)
        self.assertEqual(stats["errors"], 2)
        self.assertEqual(stats["by_status"].get("503"), 1)
        self.assertEqual(stats["by_status"].get("429"), 1)

    def test_error_rate_against_successes(self):
        self._write_successes(8, model="gemini-flash-latest")
        cost_tracker.record_api_error(
            "gemini", "503", model="gemini-flash-latest"
        )
        cost_tracker.record_api_error(
            "gemini", "503", model="gemini-flash-latest"
        )
        stats = cost_tracker.api_error_stats(hours=6)
        self.assertEqual(stats["errors"], 2)
        self.assertEqual(stats["successes"], 8)
        self.assertEqual(stats["total"], 10)
        self.assertEqual(stats["error_rate_pct"], 20.0)
        self.assertEqual(
            stats["by_model"]["gemini-flash-latest"],
            {"errors": 2, "successes": 8, "total": 10, "error_rate_pct": 20.0},
        )

    def test_window_excludes_old_errors(self):
        old_ts = (datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds")
        with open(self._err_log, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": old_ts, "service": "gemini",
                                "status": "503"}) + "\n")
        self.assertEqual(cost_tracker.api_error_stats(hours=6)["errors"], 0)

    def test_record_never_raises_on_bad_path(self):
        # Point the log at a path whose parent is a regular file → makedirs
        # raises → record_api_error must swallow it, not propagate.
        afile = os.path.join(self._tmp.name, "afile")
        open(afile, "w").close()
        bad = os.path.join(afile, "nested", "api_errors.jsonl")
        with mock.patch.object(cost_tracker, "_API_ERROR_LOG", bad):
            cost_tracker.record_api_error("gemini", "503")  # must not raise

    # ── detail 要真的寫進去（2026-08-14 之前寫入端永遠不傳）──────────────
    def test_gemini_failure_records_a_readable_detail(self):
        """面板要看得出「發生什麼事」，不然只剩 status: other×N。"""
        from agent_core import gemini_client as gc
        gc._record_gemini_api_error(
            "gemini-flash-latest", "network_unreachable",
            detail="[Errno 8] nodename nor servname provided, or not known")
        rows = [json.loads(ln) for ln in open(self._err_log, encoding="utf-8")]
        self.assertEqual(len(rows), 1)
        self.assertIn("nodename nor servname", rows[0]["detail"])
        self.assertEqual(rows[0]["status"], "network_unreachable")

    def test_detail_is_redacted_before_it_lands(self):
        """錯誤訊息可能夾帶 URL 裡的 API key —— 進檔前要洗掉。"""
        from agent_core import gemini_client as gc
        # 合成的假 key（detect-secrets 會認得這個形狀，故標 allowlist）
        fake_key = "AIzaSyD-" + "1234567890abcdefghijklmnop"  # pragma: allowlist secret
        gc._record_gemini_api_error(
            "gemini-flash-latest", "other",
            detail=f"401 from https://x/v1?key={fake_key}")
        row = json.loads(open(self._err_log, encoding="utf-8").read().strip())
        self.assertNotIn(fake_key, row["detail"])

    def test_no_detail_still_works(self):
        """沒帶 detail 的呼叫路徑不能因此壞掉。"""
        from agent_core import gemini_client as gc
        gc._record_gemini_api_error("gemini-flash-latest", "503")
        row = json.loads(open(self._err_log, encoding="utf-8").read().strip())
        self.assertEqual(row["detail"], "")

    def test_empty_is_zero(self):
        stats = cost_tracker.api_error_stats(hours=6)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["error_rate_pct"], 0.0)

    # ── 假警報普查 E：分子分母必須是同一個母體 ──────────────────────
    def test_embedding_successes_excluded_from_denominator(self):
        """embedding 進分母會把生成路徑的錯誤率稀釋到看不見（漏報）。

        分子（record_api_error）只有 _gemini_generate 最終放棄時會寫——實測 30 天
        476 筆錯誤零筆 embedding；分母若照收 cost.jsonl 全部列，24h 實測 949 筆裡
        718 筆是 embedding，錯誤率被稀釋 4.1×，夜跑背填更達 82×。這裡用同樣形狀
        的小樣本釘住：8 筆生成 + 80 筆 embedding + 2 筆失敗。
        """
        self._write_successes(8, model="gemini-flash-latest")
        self._write_successes(80, model="gemini-embedding-001")
        for _ in range(2):
            cost_tracker.record_api_error(
                "gemini", "503", model="gemini-flash-latest"
            )
        stats = cost_tracker.api_error_stats(hours=6)
        # 分母只有生成路徑的 8 筆 → 2/(2+8) = 20%（舊公式會是 2/90 = 2.2%）
        self.assertEqual(stats["successes"], 8)
        self.assertEqual(stats["total"], 10)
        self.assertEqual(stats["error_rate_pct"], 20.0)
        # 排除掉的量要留痕，面板才看得出分母為何小於 cost.jsonl 列數
        self.assertEqual(stats["embedding_successes"], 80)
        # embedding 不該出現在 by_model（它不可能有 errors，會混淆解讀）
        self.assertNotIn("gemini-embedding-001", stats["by_model"])

    def test_embedding_only_window_reports_no_denominator(self):
        """整個視窗只有 embedding（夜跑常態）→ 生成路徑樣本數 0，不是「全成功」。"""
        self._write_successes(50, model="gemini-embedding-001")
        stats = cost_tracker.api_error_stats(hours=6)
        self.assertEqual(stats["successes"], 0)
        self.assertEqual(stats["total"], 0)  # min_calls 閘會據此靜默
        self.assertEqual(stats["embedding_successes"], 50)

    # ── 別名歸屬：失敗與成功要記在同一個名字下 ──────────────────────
    def test_error_records_resolved_model_after_a_success(self):
        """成功記 `resp.model_version`、失敗只有請求別名 → by_model 兩邊對不上。

        實測後果：dashboard 長期顯示「gemini-flash-latest 12/12 (100%)」指著一個
        完全健康的別名（它現在解析到 gemini-3.7-flash），而它的成功一筆都不在那個
        名字下。30 天 476 筆錯誤有 236 筆是這樣掛的。
        """
        from agent_core import gemini_client as gc

        gc._MODEL_RESOLUTION.clear()
        self.addCleanup(gc._MODEL_RESOLUTION.clear)
        # 一次成功回應把別名的解析結果記起來
        gc._remember_model_resolution("gemini-flash-latest", "gemini-3.7-flash")
        gc._record_gemini_api_error("gemini-flash-latest", "503")

        row = json.loads(open(self._err_log, encoding="utf-8").read().strip())
        self.assertEqual(row["model"], "gemini-3.7-flash")      # 與成功列同口徑
        self.assertEqual(row["requested_model"], "gemini-flash-latest")  # 原貌留著

        # by_model 現在把失敗與成功算在同一列
        self._write_successes(9, model="gemini-3.7-flash")
        stats = cost_tracker.api_error_stats(hours=6)
        self.assertEqual(
            stats["by_model"]["gemini-3.7-flash"],
            {"errors": 1, "successes": 9, "total": 10, "error_rate_pct": 10.0},
        )
        self.assertNotIn("gemini-flash-latest", stats["by_model"])

    def test_error_falls_back_to_alias_when_resolution_unknown(self):
        """還沒有任何成功回應時（process 剛起），退回別名＝維持修正前行為。"""
        from agent_core import gemini_client as gc

        gc._MODEL_RESOLUTION.clear()
        self.addCleanup(gc._MODEL_RESOLUTION.clear)
        gc._record_gemini_api_error("gemini-flash-latest", "503")

        row = json.loads(open(self._err_log, encoding="utf-8").read().strip())
        self.assertEqual(row["model"], "gemini-flash-latest")
        self.assertNotIn("requested_model", row)  # 沒解析就不多寫這個欄位

    def test_non_string_model_version_is_ignored(self):
        """SDK 沒回 model_version 時（測試裡是 MagicMock）不能污染對照表。

        沒擋型別的話那個非字串物件會被存起來，接著寫進 api_errors.jsonl 的
        model 欄——單跑測試會過、全套合跑才炸的跨測試污染。
        """
        from unittest.mock import MagicMock

        from agent_core import gemini_client as gc

        gc._MODEL_RESOLUTION.clear()
        self.addCleanup(gc._MODEL_RESOLUTION.clear)
        gc._remember_model_resolution("gemini-flash-latest", MagicMock())
        self.assertEqual(gc._MODEL_RESOLUTION, {})

        gc._record_gemini_api_error("gemini-flash-latest", "503")
        row = json.loads(open(self._err_log, encoding="utf-8").read().strip())
        self.assertEqual(row["model"], "gemini-flash-latest")

    # ── 瞬時爆發 vs 持續故障 ────────────────────────────────────────
    def test_error_span_distinguishes_burst_from_sustained(self):
        """同樣 12 筆失敗，擠在 2 秒內 vs 攤在 6 小時，處置完全不同。"""
        now = datetime.now()
        with open(self._err_log, "w", encoding="utf-8") as f:
            for i in range(12):
                ts = (now - timedelta(seconds=i / 10)).isoformat(timespec="seconds")
                f.write(json.dumps({"ts": ts, "service": "gemini",
                                    "status": "other"}) + "\n")
        self.assertLessEqual(cost_tracker.api_error_stats(hours=6)["error_span_sec"], 2)

        with open(self._err_log, "w", encoding="utf-8") as f:
            for i in range(12):
                ts = (now - timedelta(minutes=i * 20)).isoformat(timespec="seconds")
                f.write(json.dumps({"ts": ts, "service": "gemini",
                                    "status": "other"}) + "\n")
        self.assertGreater(cost_tracker.api_error_stats(hours=6)["error_span_sec"], 3600)

    def test_error_span_is_none_for_single_error(self):
        cost_tracker.record_api_error("gemini", "503")
        self.assertIsNone(cost_tracker.api_error_stats(hours=6)["error_span_sec"])

    def test_embedding_detected_by_model_not_caller(self):
        """判定走 model 名（計價欄位、穩定），caller 改名不該影響分母。"""
        self._write_successes(4, model="text-embedding-004")
        self._write_successes(4, model="gemini-3-flash-preview")
        stats = cost_tracker.api_error_stats(hours=6)
        self.assertEqual(stats["successes"], 4)
        self.assertEqual(stats["embedding_successes"], 4)


class EmbedErrorStatsTests(unittest.TestCase):
    """embedding 路徑的分子分母都是自己的（跟生成路徑互不干擾）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._err_log = os.path.join(self._tmp.name, "api_errors.jsonl")
        self._cost_log = os.path.join(self._tmp.name, "cost.jsonl")
        for attr, path in (("_API_ERROR_LOG", self._err_log),
                           ("_COST_LOG", self._cost_log)):
            p = mock.patch.object(cost_tracker, attr, path)
            p.start()
            self.addCleanup(p.stop)

    def _write_ok(self, n, model):
        ts = datetime.now().isoformat(timespec="seconds")
        with open(self._cost_log, "a", encoding="utf-8") as f:
            for _ in range(n):
                f.write(json.dumps({"ts": ts, "cost_usd": 0.0001, "model": model,
                                    "prompt_tokens": 1, "output_tokens": 0,
                                    "total_tokens": 1}) + "\n")

    def test_embed_stats_pair_their_own_numerator_and_denominator(self):
        self._write_ok(90, "gemini-embedding-001")
        self._write_ok(50, "gemini-flash-latest")          # 生成路徑，不該算進來
        cost_tracker.record_api_error("gemini_embed", "429", model="gemini-embedding-001")
        cost_tracker.record_api_error("gemini", "503", model="gemini-flash-latest")
        s = cost_tracker.embed_error_stats(hours=6)
        self.assertEqual(s["successes"], 90)      # 只算 embedding 成功
        self.assertEqual(s["errors"], 1)          # 只算 gemini_embed 失敗
        self.assertEqual(s["by_status"], {"429": 1})

    def test_embed_errors_do_not_leak_into_the_generation_rate(self):
        """關鍵不變量：embedding 失敗進了生成路徑的分子 = 普查 E 的鏡像版
        （分子含 embedding、分母不含 → 高估）。"""
        self._write_ok(100, "gemini-flash-latest")
        for _ in range(20):
            cost_tracker.record_api_error("gemini_embed", "429",
                                          model="gemini-embedding-001")
        gen = cost_tracker.api_error_stats(hours=6)
        self.assertEqual(gen["errors"], 0, "embedding 失敗不該算進生成路徑分子")
        self.assertEqual(gen["error_rate_pct"], 0.0)

    def test_legacy_rows_without_service_count_as_generation(self):
        """舊列沒有 service 欄位（或寫 gemini）一律視為生成路徑，行為不變。"""
        self._write_ok(10, "gemini-flash-latest")
        with open(self._err_log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                "status": "503", "model": "gemini-flash-latest",
                                "detail": ""}) + "\n")
        self.assertEqual(cost_tracker.api_error_stats(hours=6)["errors"], 1)
        self.assertEqual(cost_tracker.embed_error_stats(hours=6)["errors"], 0)

    def test_empty_is_zero_not_a_crash(self):
        s = cost_tracker.embed_error_stats(hours=6)
        self.assertEqual((s["errors"], s["total"], s["error_rate_pct"]), (0, 0, 0.0))


if __name__ == "__main__":
    unittest.main()
