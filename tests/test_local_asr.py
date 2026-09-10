"""local_asr（whisper.cpp 引擎）：可用性判定、引擎旗標、JSON 解析、幻覺過濾、
繁體轉換、逐行稿輸出的單元測試。

全部 mock — CI 機器不一定裝 whisper-cli/模型檔，測試不得依賴宿主環境。
"""
import math
import os
import tempfile
import unittest
from unittest import mock

from agent_core import local_asr


class _FakeCC:
    """假 OpenCC：把「软件」轉「軟體」，其他原樣。"""

    def convert(self, text):
        return text.replace("软件", "軟體")


def _engine_json(segments):
    """組 whisper.cpp full JSON 形狀：[(start_s, end_s, text, token_ps), ...]"""
    return {
        "result": {"language": "zh"},
        "transcription": [
            {
                "offsets": {"from": int(s * 1000), "to": int(e * 1000)},
                "text": text,
                "tokens": (
                    [{"text": "[_BEG_]", "p": 0.01}]
                    + [{"text": f"t{i}", "p": p} for i, p in enumerate(ps)]
                ),
            }
            for s, e, text, ps in segments
        ],
    }


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {
            "RED_ASR_DISABLE": "", "RED_ASR_BIN": "", "RED_ASR_MODEL_PATH": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_unavailable_when_binary_missing(self):
        with mock.patch.object(local_asr, "_find_binary", return_value=None):
            self.assertFalse(local_asr.is_available())

    def test_unavailable_when_model_missing(self):
        with mock.patch.object(local_asr, "_find_binary",
                               return_value="/usr/local/bin/whisper-cli"), \
                mock.patch.object(local_asr, "_model_path",
                                  return_value="/no/such/model.bin"):
            self.assertFalse(local_asr.is_available())

    def test_kill_switch(self):
        model = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
        self.addCleanup(os.unlink, model.name)
        with mock.patch.object(local_asr, "_find_binary",
                               return_value="/usr/local/bin/whisper-cli"), \
                mock.patch.object(local_asr, "_model_path",
                                  return_value=model.name), \
                mock.patch.dict(os.environ, {"RED_ASR_DISABLE": "1"}):
            self.assertFalse(local_asr.is_available())

    def test_available(self):
        model = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
        self.addCleanup(os.unlink, model.name)
        with mock.patch.object(local_asr, "_find_binary",
                               return_value="/usr/local/bin/whisper-cli"), \
                mock.patch.object(local_asr, "_model_path",
                                  return_value=model.name):
            self.assertTrue(local_asr.is_available())


class PromptTests(unittest.TestCase):
    def setUp(self):
        # _PROMPT_SEED 是 import 時讀 env 決定的常量 — 釘死預設值，
        # 免疫宿主 shell 的 RED_ASR_PROMPT_SEED（tests-immune-to-live-state）。
        self._p = mock.patch.object(
            local_asr, "_PROMPT_SEED",
            "以下是台灣製鞋工廠的教育訓練影片旁白，使用繁體中文。")
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_seed_only(self):
        p = local_asr.build_initial_prompt()
        self.assertIn("繁體中文", p)
        self.assertNotIn("專有名詞", p)

    def test_terms_appended_at_tail_and_capped(self):
        # Whisper 只吃 initial_prompt 的最後 224 token — 詞彙表必須在尾端。
        with mock.patch.object(local_asr, "_PROMPT_TERMS_MAX", 2):
            p = local_asr.build_initial_prompt(["FTE_570", "針車", "大底"])
        self.assertTrue(p.index("FTE_570") > p.index("繁體中文"))
        self.assertIn("針車", p)
        self.assertNotIn("大底", p)  # 超過上限被截


class EngineArgsTests(unittest.TestCase):
    def test_flags(self):
        args = local_asr._engine_args(
            "/bin/whisper-cli", "/m/large-v3.bin", "/tmp/a.wav", "zh",
            "prompt with FTE_570", "/tmp/out")
        self.assertEqual(args[0], "/bin/whisper-cli")
        # 鐵則：防幻覺污染 — whisper-cli 用 --max-context 0（無 --no-context 旗標）
        self.assertEqual(args[args.index("--max-context") + 1], "0")
        # glossary priming 要覆蓋每個 30s 窗口，不是只有第一個
        self.assertIn("--carry-initial-prompt", args)
        self.assertIn("--temperature", args)
        self.assertEqual(args[args.index("--temperature") + 1], "0")
        self.assertEqual(args[args.index("-l") + 1], "zh")
        self.assertIn("FTE_570", args[args.index("--prompt") + 1])
        self.assertIn("--output-json-full", args)
        self.assertEqual(args[-1], "/tmp/a.wav")


class SegmentsFromEngineTests(unittest.TestCase):
    def test_parses_offsets_logprob_and_compression(self):
        raw = _engine_json([
            (0.0, 3.0, "先開收櫃畫面", [0.9, 0.8]),
            (3.0, 6.0, "謝謝謝謝謝謝謝謝", [0.5]),
        ])
        segs = local_asr._segments_from_engine(raw)
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["start"], 0.0)
        self.assertEqual(segs[0]["end"], 3.0)
        # avg_logprob = mean(ln 0.9, ln 0.8)，[_BEG_] 特殊 token 要跳過
        expect = (math.log(0.9) + math.log(0.8)) / 2
        self.assertAlmostEqual(segs[0]["avg_logprob"], round(expect, 4))
        self.assertEqual(segs[0]["no_speech_prob"], 0.0)  # 引擎不提供、缺省
        self.assertGreater(segs[0]["compression_ratio"], 0.0)
        # 高重複文字的壓縮率要明顯高於正常句
        self.assertGreater(segs[1]["compression_ratio"],
                           segs[0]["compression_ratio"])

    def test_empty_and_garbage(self):
        self.assertEqual(local_asr._segments_from_engine({}), [])
        self.assertEqual(local_asr._segments_from_engine(
            {"transcription": ["not a dict"]}), [])


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        # 門檻是 import 時讀 env 的常量 — 釘死預設，免疫宿主 shell 的 RED_ASR_*。
        for name, value in (("_LOGPROB_MIN", -1.0), ("_COMPRESSION_MAX", 2.4),
                            ("_NO_SPEECH_MAX", 0.6)):
            p = mock.patch.object(local_asr, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_normal_segment_kept(self):
        keep, why = local_asr._classify_segment(
            {"text": "先開收櫃畫面", "compression_ratio": 1.2,
             "no_speech_prob": 0.1, "avg_logprob": -0.3})
        self.assertTrue(keep)
        self.assertEqual(why, "")

    def test_repetition_loop_dropped(self):
        keep, why = local_asr._classify_segment(
            {"text": "謝謝謝謝謝謝", "compression_ratio": 5.0,
             "no_speech_prob": 0.1, "avg_logprob": -0.3})
        self.assertFalse(keep)
        self.assertEqual(why, "repetition_loop")

    def test_non_speech_needs_both_signals(self):
        # AND 條件（whisper 慣例）；whisper.cpp 缺省 no_speech=0.0 時不觸發
        keep, why = local_asr._classify_segment(
            {"text": "請訂閱按讚分享", "compression_ratio": 1.1,
             "no_speech_prob": 0.9, "avg_logprob": -1.5})
        self.assertFalse(keep)
        self.assertEqual(why, "non_speech")
        keep, _ = local_asr._classify_segment(
            {"text": "這段其實有講話", "compression_ratio": 1.1,
             "no_speech_prob": 0.9, "avg_logprob": -0.2})
        self.assertTrue(keep)

    def test_empty_dropped(self):
        keep, why = local_asr._classify_segment({"text": "   "})
        self.assertFalse(keep)
        self.assertEqual(why, "empty")


class RepeatRunTests(unittest.TestCase):
    """跨段複讀迴圈：逐段 compression_ratio 抓不到的幻覺型態（每段各自
    壓縮率正常），只有跨段視角看得到。"""

    def setUp(self):
        p = mock.patch.object(local_asr, "_REPEAT_RUN_MIN", 3)
        p.start()
        self.addCleanup(p.stop)

    def test_run_of_three_flagged_whole_run(self):
        segs = [{"text": "謝謝大家收看"}] * 3 + [{"text": "正常收尾句"}]
        self.assertEqual(local_asr._repeat_run_indexes(segs), {0, 1, 2})

    def test_run_of_two_kept(self):
        segs = [{"text": "謝謝大家收看"}, {"text": "謝謝大家收看"},
                {"text": "別的句子"}]
        self.assertEqual(local_asr._repeat_run_indexes(segs), set())

    def test_normalization_ignores_punct_space_case(self):
        segs = [{"text": "謝謝大家收看。"}, {"text": "謝謝 大家收看"},
                {"text": "謝謝大家收看！"}]
        self.assertEqual(local_asr._repeat_run_indexes(segs), {0, 1, 2})

    def test_run_at_tail_flagged(self):
        segs = [{"text": "正常開頭"}] + [{"text": "循環句"}] * 4
        self.assertEqual(local_asr._repeat_run_indexes(segs), {1, 2, 3, 4})

    def test_interleaved_repeats_not_flagged(self):
        segs = [{"text": "甲句"}, {"text": "乙句"},
                {"text": "甲句"}, {"text": "乙句"}, {"text": "甲句"}]
        self.assertEqual(local_asr._repeat_run_indexes(segs), set())

    def test_empty_segments_never_flagged(self):
        segs = [{"text": "。。。"}] * 5  # 正規化後為空 → 交給 empty 分類處理
        self.assertEqual(local_asr._repeat_run_indexes(segs), set())


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        self._tmp.write(b"fake")
        self._tmp.close()
        self.addCleanup(os.unlink, self._tmp.name)
        # 免疫宿主 shell 的 RED_ASR_*（空字串 → 程式回落預設值）。
        env = mock.patch.dict(os.environ, {
            "RED_ASR_MODEL_PATH": "", "RED_ASR_LANGUAGE": "",
            "RED_ASR_DISABLE": "", "RED_ASR_BIN": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def _run(self, raw, **kwargs):
        run_engine = mock.Mock(return_value=raw)
        with mock.patch.object(local_asr, "is_available", return_value=True), \
                mock.patch.object(local_asr, "_find_binary",
                                  return_value="/bin/whisper-cli"), \
                mock.patch.object(local_asr, "_model_path",
                                  return_value="/m/ggml-large-v3.bin"), \
                mock.patch.object(local_asr, "_to_wav16k",
                                  return_value="/tmp/fake.wav"), \
                mock.patch.object(local_asr, "_run_engine", run_engine), \
                mock.patch.object(local_asr, "_get_opencc",
                                  return_value=_FakeCC()):
            result = local_asr.transcribe_local(self._tmp.name, **kwargs)
        return result, run_engine

    def test_unavailable_returns_none(self):
        with mock.patch.object(local_asr, "is_available", return_value=False):
            self.assertIsNone(local_asr.transcribe_local(self._tmp.name))

    def test_missing_file_returns_none(self):
        with mock.patch.object(local_asr, "is_available", return_value=True):
            self.assertIsNone(local_asr.transcribe_local("/no/such/file.mp4"))

    def test_wav_conversion_failure_returns_none(self):
        with mock.patch.object(local_asr, "is_available", return_value=True), \
                mock.patch.object(local_asr, "_find_binary",
                                  return_value="/bin/whisper-cli"), \
                mock.patch.object(local_asr, "_to_wav16k", return_value=None):
            self.assertIsNone(local_asr.transcribe_local(self._tmp.name))

    def test_engine_failure_returns_none(self):
        result, _ = self._run(None)
        self.assertIsNone(result)

    def test_prompt_and_lang_passed_to_engine(self):
        raw = _engine_json([(0.0, 1.0, "hi", [0.9])])
        _, run_engine = self._run(raw, glossary_terms=["FTE_570"])
        args = run_engine.call_args[0]
        # (_binary, model, wav, lang, prompt, out_dir)
        self.assertEqual(args[3], "zh")
        self.assertIn("FTE_570", args[4])

    def test_filtering_conversion_and_flags(self):
        raw = _engine_json([
            (0.0, 3.0, "打开收櫃软件然後執行入庫作業流程", [0.9, 0.85]),
            (3.0, 6.0, "謝謝謝謝謝謝謝謝謝謝謝謝謝謝謝謝謝謝謝謝謝謝謝謝", [0.5]),
            (6.0, 9.0, "模糊不清的一段話啊", [0.2, 0.25]),
        ])
        result, _ = self._run(raw)
        self.assertEqual(result["engine"], "whisper.cpp")
        self.assertEqual(result["model"], "ggml-large-v3.bin")
        segs = result["segments"]
        self.assertEqual(len(segs), 3)
        self.assertIn("軟體", segs[0]["text"])            # 簡→繁
        self.assertTrue(segs[0]["kept"])
        self.assertEqual(segs[1]["drop_reason"], "repetition_loop")
        self.assertTrue(segs[2]["low_confidence"])         # ln(0.2)≈-1.6 < -1.0
        self.assertEqual(result["dropped"], 1)
        self.assertIn("軟體", result["text"])
        self.assertNotIn("謝謝謝謝", result["text"])        # 被濾段不進全文

    def test_forty_repeated_segments_all_filtered(self):
        # 最典型的 Whisper 幻覺：同一短句跨段複讀 — 每段各自壓縮率正常、
        # 逐段過濾全數放行，只有跨段規則抓得到。40 段全數要被濾。
        raw = _engine_json([
            (float(i), float(i + 1), "謝謝大家收看", [0.9]) for i in range(40)
        ])
        with mock.patch.object(local_asr, "_REPEAT_RUN_MIN", 3):
            result, _ = self._run(raw)
        self.assertEqual(result["dropped"], 40)
        self.assertEqual(result["text"], "")
        self.assertTrue(all(s["drop_reason"] == "repetition_run"
                            for s in result["segments"]))

    def test_isolated_pair_of_repeats_survives(self):
        raw = _engine_json([
            (0.0, 1.0, "先開收櫃畫面", [0.9]),
            (1.0, 2.0, "先開收櫃畫面", [0.9]),  # 講兩次是人話，不是幻覺
            (2.0, 3.0, "然後執行入庫", [0.9]),
        ])
        with mock.patch.object(local_asr, "_REPEAT_RUN_MIN", 3):
            result, _ = self._run(raw)
        self.assertEqual(result["dropped"], 0)
        self.assertEqual(sum(1 for s in result["segments"] if s["kept"]), 3)


class FormatTests(unittest.TestCase):
    def test_format_transcript(self):
        result = {"segments": [
            {"start": 5, "end": 8, "text": "開啟畫面", "kept": True},
            {"start": 65, "end": 70, "text": "低信度段", "kept": True,
             "low_confidence": True},
            {"start": 70, "end": 75, "text": "幻覺段", "kept": False,
             "drop_reason": "repetition_loop"},
        ]}
        out = local_asr.format_transcript(result)
        self.assertIn("[00:05] 開啟畫面", out)
        self.assertIn("[01:05]（低信度） 低信度段", out)
        self.assertNotIn("幻覺段", out)
        full = local_asr.format_transcript(result, include_dropped=True)
        self.assertIn("（已濾:repetition_loop） 幻覺段", full)


if __name__ == "__main__":
    unittest.main()
