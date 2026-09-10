"""Intent router 回歸測試 — 聚焦 media_processing 路由。

`media_processing` intent（commit d2b0fe44）把「下載 + 降/升調」這類請求路由到
`adjust_audio_pitch` / downloaders，免得 agent 在 download 上空轉、找不到變調工具
（feature 的原始動機就是讓「降一個全音」surface 變調工具）。

這條路由（其實整個 intent_router 模組）一直沒有專屬測試，本檔補上，鎖住：
  1. 變調 / 下載語句確實落在 media_processing
  2. 混合句「下載 + 降全音」由變調規則勝出（pitch 1.4 > download 1.0）
  3. 新 intent 沒有過度吃掉 email / query 流量
  4. tool bucket 含核心媒體工具、filter 把變調工具排前面且不漏工具
  5. 描述不再提及早已下架的「混音 / mix」(PR #119)

純函式 / 純資料測試（classify_heuristic / filter_tools_by_intent / _TOOL_BUCKETS）
不打 API、不寫 intent log，故無需 STATE_DIR 隔離。
"""
import unittest

from agent_core import intent_router as ir


class MediaProcessingRoutingTests(unittest.TestCase):
    def test_transpose_phrases_route_to_media(self):
        for text in [
            "下載這首歌然後降一個全音",
            "幫我把這段 mp3 升半音",
            "把這首歌降兩個半音",
            "升調",
            "download this youtube video and pitch shift it",
        ]:
            with self.subTest(text=text):
                r = ir.classify_heuristic(text)
                self.assertEqual(
                    r.intent, ir.INTENT_MEDIA_PROCESSING,
                    f"{text!r} -> {r.intent} (conf={r.confidence}, {r.reason})")

    def test_pure_download_phrases_route_to_media(self):
        for text in ["下載 YouTube 影片", "下載這個音樂", "抓這部影片"]:
            with self.subTest(text=text):
                r = ir.classify_heuristic(text)
                self.assertEqual(r.intent, ir.INTENT_MEDIA_PROCESSING)

    def test_transpose_outranks_download_in_mixed_message(self):
        # 「下載 + 降全音」要落在 media（pitch 1.4 > download 1.0），
        # 且 matched_keywords 應包含變調詞 — feature 的重點。
        r = ir.classify_heuristic("下載這首歌然後降一個全音")
        self.assertEqual(r.intent, ir.INTENT_MEDIA_PROCESSING)
        self.assertTrue(
            any("降" in k for k in r.matched_keywords),
            f"matched_keywords 應含變調詞: {r.matched_keywords}")

    def test_media_does_not_steal_plain_email(self):
        r = ir.classify_heuristic("幫我寄信給客戶 A 確認交期")
        self.assertEqual(r.intent, ir.INTENT_WRITE_EMAIL)

    def test_media_does_not_steal_plain_query(self):
        r = ir.classify_heuristic("幫我查一下今天的信")
        self.assertNotEqual(r.intent, ir.INTENT_MEDIA_PROCESSING)


class MediaProcessingBucketTests(unittest.TestCase):
    def test_bucket_has_core_media_tools(self):
        bucket = ir._TOOL_BUCKETS[ir.INTENT_MEDIA_PROCESSING]
        for name in ("adjust_audio_pitch", "download_youtube_audio",
                     "download_online_video", "telegram_send_file"):
            self.assertIn(name, bucket)

    def test_filter_surfaces_pitch_tool_first_without_dropping_any(self):
        class _Fn:
            def __init__(self, n):
                self.__name__ = n
        names = ["search_gmail", "adjust_audio_pitch", "run_shell", "send_gmail"]
        tools = [_Fn(n) for n in names]
        out = ir.filter_tools_by_intent(tools, ir.INTENT_MEDIA_PROCESSING)
        # 相關工具排最前
        self.assertEqual(out[0].__name__, "adjust_audio_pitch")
        # safety net：一個工具都沒漏
        self.assertEqual({t.__name__ for t in out}, set(names))

    def test_media_intent_registered_and_description_clean(self):
        self.assertIn(ir.INTENT_MEDIA_PROCESSING, ir._ALL_INTENTS)
        self.assertIn(ir.INTENT_MEDIA_PROCESSING, ir._INTENT_DESC)
        # 描述不該再提及早已下架的「混音 / mix」
        desc = ir._INTENT_DESC[ir.INTENT_MEDIA_PROCESSING].lower()
        self.assertNotIn("混音", desc)
        self.assertNotIn("mix", desc)


if __name__ == "__main__":
    unittest.main()
