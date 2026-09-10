"""Regression: thinking 旋鈕跨世代不相容，寫死在 config 裡會炸掉 fallback。

2026-08-05 實測（同一支 key、google-genai 2.16.0）：

    模型                          thinking_level    thinking_budget
    gemini-flash-latest（3.x）        ✅                ❌ 400
    gemini-2.5-flash-lite             ❌ 400            ✅

    └─ 400 訊息："Thinking level is not supported for this model."

危險在於 `_gemini_generate` 的 fallback 分支把**同一份 config** 傳給 fallback
model，而 fleet 的組合正好跨代（RED_GEMINI_MODEL=gemini-flash-latest /
RED_GEMINI_FALLBACK_MODEL=gemini-2.5-flash-lite）。若把 thinking_level 直接寫進
分類器的 config，primary 一出事、fallback 就吃 400 INVALID_ARGUMENT —— 而 400
是不可重試的，等於把「primary 掛了但 fallback 頂著」變成「整條路徑一起死」。

所以呼叫端只表達意圖（私有鍵 `_red_thinking`），由 gemini_client 按**這一次實際
要打的 model** 翻成該模型吃得下的欄位。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_INTENT = {"http_options": {"timeout": 30000}, "_red_thinking": "minimal"}


class AdaptThinkingConfigTests(unittest.TestCase):
    def setUp(self):
        from agent_core import gemini_client

        self.gc = gemini_client

    def test_gemini_3_family_gets_thinking_level(self):
        for model in ("gemini-flash-latest", "gemini-3.6-flash", "gemini-3-pro",
                      "gemini-pro-latest"):
            with self.subTest(model=model):
                out = self.gc._adapt_thinking_config(_INTENT, model)
                self.assertEqual(out["thinking_config"], {"thinking_level": "MINIMAL"})

    def test_gemini_25_and_older_get_thinking_budget(self):
        for model in ("gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro",
                      "gemini-2.0-flash", "gemini-1.5-pro"):
            with self.subTest(model=model):
                out = self.gc._adapt_thinking_config(_INTENT, model)
                self.assertEqual(out["thinking_config"], {"thinking_budget": 0})

    def test_unknown_model_drops_the_knob_instead_of_guessing(self):
        """認不出世代就不帶 thinking_config —— 沒省到，但絕不會 400。"""
        out = self.gc._adapt_thinking_config(_INTENT, "some-future-model")
        self.assertNotIn("thinking_config", out)
        self.assertEqual(out["http_options"], {"timeout": 30000})

    def test_private_key_never_survives_adaptation(self):
        """`_red_thinking` 是私有鍵，漏給 SDK 會是 unknown field 400。"""
        for model in ("gemini-flash-latest", "gemini-2.5-flash-lite", "mystery"):
            with self.subTest(model=model):
                self.assertNotIn("_red_thinking",
                                 self.gc._adapt_thinking_config(_INTENT, model))

    def test_config_without_intent_is_untouched(self):
        cfg = {"http_options": {"timeout": 1}}
        self.assertEqual(self.gc._adapt_thinking_config(cfg, "gemini-flash-latest"), cfg)

    def test_non_dict_config_passes_through(self):
        """呼叫端自帶 GenerateContentConfig 物件時不去猜它的內部結構。"""
        sentinel = object()
        self.assertIs(self.gc._adapt_thinking_config(sentinel, "gemini-flash-latest"),
                      sentinel)
        self.assertIsNone(self.gc._adapt_thinking_config(None, "gemini-flash-latest"))

    def test_unknown_intent_is_ignored(self):
        out = self.gc._adapt_thinking_config(
            {"_red_thinking": "aggressive"}, "gemini-flash-latest")
        self.assertNotIn("thinking_config", out)
        self.assertNotIn("_red_thinking", out)

    def test_caller_config_is_not_mutated(self):
        cfg = dict(_INTENT)
        self.gc._adapt_thinking_config(cfg, "gemini-flash-latest")
        self.assertEqual(cfg, _INTENT, "不可就地改呼叫端的 dict")


class FallbackUsesItsOwnKnobTests(unittest.TestCase):
    """本案核心：primary 失敗換 fallback 時，旋鈕要跟著換成 fallback 那一代的。"""

    def setUp(self):
        # circuit breaker 狀態是模組級的，跑測試會累積失敗次數而汙染後面的測試
        # （unittest 沒有 autouse fixture，隔離要自己寫在 setUp/tearDown）。
        from agent_core import gemini_client

        self.gc = gemini_client
        gemini_client._reset_gemini_circuit_for_tests()
        self.addCleanup(gemini_client._reset_gemini_circuit_for_tests)

    def test_fallback_call_gets_budget_not_level(self):
        from agent_core import gemini_client

        seen = []

        def _generate(**kwargs):
            seen.append((kwargs["model"], kwargs.get("config")))
            if len(seen) == 1:
                raise RuntimeError("503 UNAVAILABLE: model overloaded")  # 可重試 → 觸發 fallback
            return mock.MagicMock(text="{}", usage_metadata=mock.MagicMock(
                prompt_token_count=1, candidates_token_count=1,
                cached_content_token_count=0, total_token_count=2))

        client = mock.MagicMock()
        client.models.generate_content.side_effect = _generate

        with mock.patch.object(gemini_client, "_get_gemini_client", return_value=client), \
             mock.patch.object(gemini_client, "_USES_LIBRESSL", False), \
             mock.patch.dict(os.environ,
                             {"RED_GEMINI_FALLBACK_MODEL": "gemini-2.5-flash-lite"},
                             clear=False):
            gemini_client._gemini_generate(
                model="gemini-flash-latest", contents=["hi"],
                max_attempts=1, config=dict(_INTENT))

        self.assertGreaterEqual(len(seen), 2, "應該有 primary + fallback 兩次呼叫")
        primary_model, primary_cfg = seen[0]
        fb_model, fb_cfg = seen[-1]
        self.assertEqual(primary_model, "gemini-flash-latest")
        self.assertEqual(primary_cfg["thinking_config"], {"thinking_level": "MINIMAL"})
        self.assertEqual(fb_model, "gemini-2.5-flash-lite")
        # ⚠️ 修之前這裡會是 thinking_level → 真打會 400 INVALID_ARGUMENT
        self.assertEqual(fb_cfg["thinking_config"], {"thinking_budget": 0})
        for _, cfg in seen:
            self.assertNotIn("_red_thinking", cfg)


class LowIntentTests(unittest.TestCase):
    """low 意圖的世代對應。budget 那欄有下限：實測 gemini-2.5-flash-lite
    **拒收 128**（400 "The thinking budget 128 is invalid"）、收 512。"""

    def setUp(self):
        from agent_core import gemini_client

        self.gc = gemini_client
        self.cfg = {"http_options": {"timeout": 30000}, "_red_thinking": "low"}

    def test_gemini_3_family_gets_thinking_level_low(self):
        out = self.gc._adapt_thinking_config(self.cfg, "gemini-flash-latest")
        self.assertEqual(out["thinking_config"], {"thinking_level": "LOW"})

    def test_gemini_25_gets_a_budget_above_the_api_minimum(self):
        out = self.gc._adapt_thinking_config(self.cfg, "gemini-2.5-flash-lite")
        budget = out["thinking_config"]["thinking_budget"]
        self.assertEqual(budget, 512)
        self.assertGreaterEqual(budget, 512, "低於 512 會被 API 拒收（實測 128 → 400）")

    def test_low_still_thinks_more_than_minimal(self):
        low = self.gc._THINKING_INTENTS["low"]
        minimal = self.gc._THINKING_INTENTS["minimal"]
        self.assertGreater(low["budget"], minimal["budget"])


class ClassifierWiringTests(unittest.TestCase):
    """哪支壓 thinking、壓到哪一檔，是量出來的，不能順手改。

    96 封 12 類別分層抽樣、控制組跑兩次量雜訊底線、McNemar 配對檢定：

      設定       raw 的 category / urgency        lake 的 entity / amount
      MINIMAL    p=0.61 ✅ / p=0.019 ⚠️過度升級   p=0.027 ⚠️ / p=0.031 ⚠️
      LOW        p=1.00 ✅ / p=0.61  ✅           p=0.004 ⚠️ / p=0.031 ⚠️

    ① raw 用 LOW 不用 MINIMAL：MINIMAL 會讓急迫度過度升級（Y→R 12 筆），而 R
       是 daemon 自動擬稿的開關。LOW 保留約 40 顆 thinking 剛好夠校準。
    ② lake 一律不壓：它在 LOW 下只剩 17 顆 thinking＝實質等於 MINIMAL，
       entity/amount 照樣退步（amount 還會生出多餘的假金額）。
    """

    def test_urgency_classifier_asks_for_low(self):
        from agent_core import email_classify

        cfg = email_classify._classify_gen_config()
        self.assertEqual(cfg.get("_red_thinking"), "low")
        self.assertIn("http_options", cfg)  # 原本的緊 timeout 不能被弄丟

    def test_lake_classifier_keeps_full_thinking(self):
        """回歸守門：lake 若被順手加上 thinking 壓制，entity/amount 會退步。"""
        from agent_core import email_classify

        self.assertNotIn("_red_thinking", email_classify._lake_gen_config())

    def test_lake_and_raw_configs_are_independent(self):
        """兩支各自成函式就是為了不會被合回同一份 config。"""
        from agent_core import email_classify

        email_classify._classify_gen_config()
        self.assertNotIn("_red_thinking", email_classify._lake_gen_config())

    def test_env_can_restore_model_default_thinking(self):
        from agent_core import email_classify

        with mock.patch.object(email_classify, "_CLASSIFY_THINKING", ""):
            self.assertNotIn("_red_thinking", email_classify._classify_gen_config())

    def test_every_intent_used_by_callers_is_known_to_the_adapter(self):
        """呼叫端送的意圖字串若打錯，翻譯層會靜默當作沒設 —— 這裡擋住。"""
        from agent_core import email_classify, gemini_client

        intent = email_classify._classify_gen_config().get("_red_thinking")
        self.assertIn(intent, gemini_client._THINKING_INTENTS)


if __name__ == "__main__":
    unittest.main()
