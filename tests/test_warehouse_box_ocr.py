"""外箱手寫嘜頭 OCR（skills/warehouse_box_ocr.py）的密封測試。

引擎已改**純本機 PaddleOCR PP-OCRv6（onnxruntime、專用 venv）**、無 LLM：
不跑真模型 — `_run_local_ocr` / subprocess 皆 mock。重點防回歸：
  - 規則式後處理 `_extract_marks`：OCR 行 → 三欄 schema 的對映
    （全形正規化、拆行配對、印刷雜訊過濾、低信心丟棄、對不到回 null）。
  - runner 協定：RESULT_JSON marker 解析（paddle 往 stdout 噴 log）、
    引擎未安裝/失敗的錯誤路徑。
  - 員工（部門色）session 路徑圈地：只能讀 Telegram 上傳目錄、禁資料夾批次。
  - 工具接線：SKILL_TOOLS 匯出、tier=SAFE、indigo 員工白名單有列。
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from agent_core import path_safety


def _load_skill():
    """skills/ 不是 package — 按路徑載入。"""
    skill_path = os.path.join(path_safety._REPO_ROOT, "skills", "warehouse_box_ocr.py")
    spec = importlib.util.spec_from_file_location("warehouse_box_ocr_under_test", skill_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_skill = _load_skill()


def _ln(text, score=0.99, box=None):
    return {"text": text, "score": score, "box": box}


# 標準嘜頭：名美（頂）/ KH:2384 / 圈內 22 / 印刷尺寸雜訊
# （合成測試值 — 出廠 lexicon 為空、白名單不含這些值，不觸發校正）
_FULL_LINES = [
    _ln("名美", box=[293, 44, 493, 164]),
    _ln("KH:2384", box=[250, 247, 514, 338]),
    _ln("22", box=[386, 441, 488, 520]),
    _ln("70X50X56.6 CM", box=[575, 643, 787, 676]),
]

_FULL_MARKS = {
    "customer_name": "名美",
    "reference_entry": {"key": "KH", "value": "2384"},
    "circled_count": 22,
}


class ExtractMarksTests(unittest.TestCase):
    """純規則後處理（不碰引擎）。"""

    def test_happy_path(self):
        self.assertEqual(_skill._extract_marks(_FULL_LINES), _FULL_MARKS)

    def test_english_key_ref(self):
        got = _skill._extract_marks([_ln("KH:2384", box=[0, 0, 200, 60])])
        self.assertEqual(got["reference_entry"], {"key": "KH", "value": "2384"})

    def test_fullwidth_colon_and_digits_normalized(self):
        got = _skill._extract_marks([_ln("何：２３８０", box=[0, 100, 200, 150])])
        self.assertEqual(got["reference_entry"], {"key": "何", "value": "2380"})

    def test_split_ref_tokens_paired_by_row(self):
        # OCR 把「何:」跟「2380」拆成同列兩行 → 應配對，且 2380 不再被當件數
        got = _skill._extract_marks([
            _ln("何:", box=[100, 200, 180, 260]),
            _ln("2380", box=[200, 205, 360, 258]),
            _ln("22", box=[150, 400, 220, 460]),
        ])
        self.assertEqual(got["reference_entry"], {"key": "何", "value": "2380"})
        self.assertEqual(got["circled_count"], 22)

    def test_split_ref_without_boxes_only_pairs_forward(self):
        # 無座標 fallback：「999」在「何:」之前，不得被配成 value
        got = _skill._extract_marks([
            _ln("999", box=None),
            _ln("何:", box=None),
            _ln("2380", box=None),
        ])
        self.assertEqual(got["reference_entry"], {"key": "何", "value": "2380"})

    def test_warning_labels_not_customer_name(self):
        # 箱面警示標語（印刷）不可誤判為客戶名
        got = _skill._extract_marks([
            _ln("易碎小心輕放", box=[0, 10, 100, 60]),
            _ln("名媛", box=[0, 100, 100, 150]),
        ])
        self.assertEqual(got["customer_name"], "名媛")

    def test_low_score_lines_dropped(self):
        got = _skill._extract_marks([_ln("名媛", score=0.3, box=[0, 0, 10, 10])])
        self.assertIsNone(got["customer_name"])

    def test_printed_noise_filtered(self):
        got = _skill._extract_marks([
            _ln("70X50X56.6 CM", box=[0, 0, 10, 10]),
            _ln("CAC專用", box=[0, 20, 10, 30]),
            _ln("QTY:20", box=[0, 40, 10, 50]),
        ])
        self.assertEqual(got, {"customer_name": None,
                               "reference_entry": {"key": None, "value": None},
                               "circled_count": None})

    def test_customer_name_topmost_cjk_line(self):
        got = _skill._extract_marks([
            _ln("易碎小心", box=[0, 300, 100, 350]),
            _ln("名媛", box=[0, 40, 100, 90]),
        ])
        self.assertEqual(got["customer_name"], "名媛")

    def test_count_prefers_highest_score_and_max_4_digits(self):
        got = _skill._extract_marks([
            _ln("7", score=0.6, box=[0, 0, 10, 10]),
            _ln("22", score=0.98, box=[0, 20, 10, 30]),
            _ln("20260721", score=0.99, box=[0, 40, 10, 50]),  # 超過 4 位 → 不是件數
        ])
        self.assertEqual(got["circled_count"], 22)

    def test_empty_lines_all_null(self):
        self.assertEqual(_skill._extract_marks([]), {
            "customer_name": None,
            "reference_entry": {"key": None, "value": None},
            "circled_count": None,
        })


class LexiconTests(unittest.TestCase):
    """封閉集合校正：白名單吸附 / 混淆表 / 數字候選驗證。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lex_file = os.path.join(self.tmp.name, "box_ocr_lexicon.json")
        p = mock.patch.object(_skill, "_lexicon_path", return_value=self.lex_file)
        p.start()
        self.addCleanup(p.stop)

    def _write_lexicon(self, data):
        with open(self.lex_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def _marks(self, name=None, key=None, value=None, count=None):
        return {"customer_name": name,
                "reference_entry": {"key": key, "value": value},
                "circled_count": count}

    def test_default_lexicon_is_empty_and_file_created(self):
        # 出廠必須為空 — lexicon 只該長「自家」值，不得內建任何外部樣本
        lex = _skill._load_lexicon()
        self.assertTrue(os.path.exists(self.lex_file))
        self.assertEqual(lex["customers"], [])
        self.assertEqual(lex["ref_keys"], [])
        self.assertEqual(lex["confusions"], {"customer": {}, "ref_key": {}})

    def test_confusion_pair_customer_and_key(self):
        # 合成樣例（非真實客戶）：混淆表直接對映
        self._write_lexicon({"customers": ["名美"], "ref_keys": ["KH"],
                             "ref_values": [],
                             "confusions": {"customer": {"名媛": "名美"},
                                            "ref_key": {"何": "KH"}}})
        marks, corr = _skill._apply_lexicon(self._marks(name="名媛", key="何", value="2384"))
        self.assertEqual(marks["customer_name"], "名美")
        self.assertEqual(marks["reference_entry"]["key"], "KH")
        self.assertEqual(len(corr), 2)

    def test_fuzzy_snap_within_distance_1(self):
        self._write_lexicon({"customers": ["大明鞋業"], "ref_keys": [], "ref_values": [],
                             "confusions": {}})
        marks, corr = _skill._apply_lexicon(self._marks(name="大朋鞋業"))
        self.assertEqual(marks["customer_name"], "大明鞋業")
        self.assertTrue(any("白名單" in c for c in corr))

    def test_ambiguous_whitelist_tie_flagged_not_snapped(self):
        # 同距離命中多個白名單值 → 保留原樣 + 標人工確認，不亂吸第一個
        self._write_lexicon({"customers": ["名美", "名媛"], "ref_keys": [],
                             "ref_values": [], "confusions": {}})
        marks, corr = _skill._apply_lexicon(self._marks(name="名笑"))
        self.assertEqual(marks["customer_name"], "名笑")
        self.assertTrue(any("人工確認" in c for c in corr))

    def test_unknown_name_kept_unchanged(self):
        # 白名單是校正不是過濾：距離 >1 的新客戶名保留原樣
        marks, corr = _skill._apply_lexicon(self._marks(name="新客戶王"))
        self.assertEqual(marks["customer_name"], "新客戶王")
        self.assertEqual(corr, [])

    def test_value_snapped_to_unique_candidate(self):
        # 0↔4 形近權重 0.5：2380 → 唯一候選 2384
        self._write_lexicon({"customers": [], "ref_keys": [], "ref_values": ["2384"],
                             "confusions": {}})
        marks, corr = _skill._apply_lexicon(self._marks(value="2380"))
        self.assertEqual(marks["reference_entry"]["value"], "2384")
        self.assertTrue(any("合法清單" in c for c in corr))

    def test_value_ambiguous_flagged_not_guessed(self):
        self._write_lexicon({"customers": [], "ref_keys": [],
                             "ref_values": ["2384", "2386"], "confusions": {}})
        marks, corr = _skill._apply_lexicon(self._marks(value="2380"))
        self.assertEqual(marks["reference_entry"]["value"], "2380")  # 不亂猜
        self.assertTrue(any("人工確認" in c for c in corr))

    def test_value_without_valid_list_untouched(self):
        marks, corr = _skill._apply_lexicon(self._marks(value="9999"))
        self.assertEqual(marks["reference_entry"]["value"], "9999")
        self.assertEqual(corr, [])

    def test_digit_distance_weighting(self):
        self.assertEqual(_skill._digit_distance("2380", "2384"), 0.5)  # 0↔4 形近
        self.assertEqual(_skill._digit_distance("2380", "2386"), 1.0)  # 0↔6 非形近
        self.assertEqual(_skill._digit_distance("22", "22"), 0.0)


def _load_runner():
    runner_path = os.path.join(path_safety._REPO_ROOT, "scripts", "box_ocr_runner.py")
    spec = importlib.util.spec_from_file_location("box_ocr_runner_under_test", runner_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RunnerRotationVoteTests(unittest.TestCase):
    """runner 的旋轉投票純邏輯（不碰 paddle — module 頂層只 import stdlib）。"""

    def setUp(self):
        self.runner = _load_runner()

    def test_default_rotations(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RED_BOX_OCR_ROTATIONS", None)
            self.assertEqual(self.runner._rotations(), [0, 90, 180, 270])

    def test_rotations_env_override_and_garbage(self):
        with mock.patch.dict(os.environ, {"RED_BOX_OCR_ROTATIONS": "0"}):
            self.assertEqual(self.runner._rotations(), [0])
        with mock.patch.dict(os.environ, {"RED_BOX_OCR_ROTATIONS": "abc,,"}):
            self.assertEqual(self.runner._rotations(), [0])

    def test_rotation_score_counts_confident_lines_only(self):
        lines = [{"score": 0.9}, {"score": 0.6}, {"score": 0.4}]
        self.assertAlmostEqual(self.runner._rotation_score(lines), 1.5)

    def test_candidate_image_zero_deg_respects_exif(self):
        # 手機 JPEG 帶 EXIF 方向：0° 候選也要轉正，不能直用原檔
        from PIL import Image
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = os.path.join(tmp.name, "exif.jpg")
        ex = Image.Exif()
        ex[0x0112] = 6  # Rotate 90 CW
        Image.new("RGB", (100, 40), (200, 170, 130)).save(src, exif=ex)
        out = self.runner._candidate_image(src, 0, tmp.name)
        self.assertNotEqual(out, src)
        with Image.open(out) as im:
            self.assertEqual(im.size, (40, 100))  # 已轉正（寬高互換）

    def test_candidate_image_zero_deg_no_exif_uses_original(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = os.path.join(tmp.name, "plain.jpg")
        from PIL import Image
        Image.new("RGB", (100, 40)).save(src)
        self.assertEqual(self.runner._candidate_image(src, 0, tmp.name), src)


class RunnerProtocolTests(unittest.TestCase):
    """_run_local_ocr 的 subprocess 協定（mock subprocess.run）。"""

    def setUp(self):
        # 引擎直譯器存在性檢查 → 用 sys.executable 充當
        p = mock.patch.object(_skill, "_box_ocr_python", return_value=sys.executable)
        p.start()
        self.addCleanup(p.stop)

    def _proc(self, stdout="", stderr="", returncode=0):
        return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    def test_marker_parsed_from_noisy_stdout(self):
        payload = {"ok": True, "results": [{"file": "a.jpg", "lines": _FULL_LINES}]}
        noisy = ("Creating model: PP-OCRv6_medium_det\nFetching 5 files...\n"
                 + _skill._RESULT_MARKER + json.dumps(payload, ensure_ascii=False) + "\n")
        with mock.patch("subprocess.run", return_value=self._proc(stdout=noisy)):
            results = _skill._run_local_ocr(["a.jpg"])
        self.assertEqual(results[0]["file"], "a.jpg")

    def test_runner_not_ok_raises(self):
        out = _skill._RESULT_MARKER + json.dumps({"ok": False, "error": "engine init failed"})
        with mock.patch("subprocess.run", return_value=self._proc(stdout=out)):
            with self.assertRaisesRegex(RuntimeError, "engine init failed"):
                _skill._run_local_ocr(["a.jpg"])

    def test_no_marker_raises_with_tail(self):
        with mock.patch("subprocess.run",
                        return_value=self._proc(stdout="garbage", stderr="boom", returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                _skill._run_local_ocr(["a.jpg"])

    def test_missing_engine_venv_mentions_setup(self):
        with mock.patch.object(_skill, "_box_ocr_python",
                               return_value="/nonexistent/box_ocr/bin/python"):
            with self.assertRaisesRegex(RuntimeError, "setup-box-ocr"):
                _skill._run_local_ocr(["a.jpg"])


class ReadBoxShippingMarksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.img = self._write_img("box1.jpg")
        lex = mock.patch.object(
            _skill, "_lexicon_path",
            return_value=os.path.join(self.tmp.name, "lexicon.json"))
        lex.start()
        self.addCleanup(lex.stop)

    def _write_img(self, name, size=16):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as f:
            f.write(b"\xff\xd8" + b"x" * size)
        return p

    def _patch_ocr(self, fn):
        p = mock.patch.object(_skill, "_run_local_ocr", side_effect=fn)
        m = p.start()
        self.addCleanup(p.stop)
        return m

    def test_single_image_returns_schema_json(self):
        self._patch_ocr(lambda paths: [{"file": paths[0], "lines": _FULL_LINES}])
        out = _skill.read_box_shipping_marks(self.img)
        self.assertEqual(json.loads(out), _FULL_MARKS)

    def test_lexicon_corrections_included_in_output(self):
        # 使用者建過的 lexicon 生效：誤讀 → 校正並列出 corrections（合成樣例）
        with open(os.path.join(self.tmp.name, "lexicon.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"customers": ["名美"], "ref_keys": ["KH"], "ref_values": [],
                       "confusions": {"customer": {"名媛": "名美"},
                                      "ref_key": {"何": "KH"}}},
                      f, ensure_ascii=False)
        bad = [_ln("名媛", box=[0, 0, 100, 60]),
               _ln("何:2384", box=[0, 100, 200, 160])]
        self._patch_ocr(lambda paths: [{"file": paths[0], "lines": bad}])
        out = json.loads(_skill.read_box_shipping_marks(self.img))
        self.assertEqual(out["customer_name"], "名美")
        self.assertEqual(out["reference_entry"]["key"], "KH")
        self.assertEqual(len(out["corrections"]), 2)

    def test_all_null_adds_reshoot_note(self):
        self._patch_ocr(lambda paths: [{"file": paths[0], "lines": []}])
        out = json.loads(_skill.read_box_shipping_marks(self.img))
        self.assertIsNone(out["customer_name"])
        self.assertIn("重拍", out["note"])

    def test_runner_failure_reported(self):
        def boom(paths):
            raise RuntimeError("引擎未安裝")
        self._patch_ocr(boom)
        out = _skill.read_box_shipping_marks(self.img)
        self.assertIn("嘜頭辨識失敗", out)
        self.assertIn("引擎未安裝", out)

    def test_missing_file(self):
        out = _skill.read_box_shipping_marks(os.path.join(self.tmp.name, "nope.jpg"))
        self.assertIn("找不到", out)

    def test_directory_batch_isolates_per_file_error(self):
        self._write_img("box2.jpg")
        with open(os.path.join(self.tmp.name, "notes.txt"), "w") as f:
            f.write("skip me")

        def per_file(paths):
            out = []
            for p in paths:
                if p.endswith("box1.jpg"):
                    out.append({"file": p, "error": "unreadable image"})
                else:
                    out.append({"file": p, "lines": _FULL_LINES})
            return out
        self._patch_ocr(per_file)
        out = json.loads(_skill.read_box_shipping_marks(self.tmp.name))
        results = {r["file"]: r for r in out["results"]}
        self.assertEqual(set(results), {"box1.jpg", "box2.jpg"})
        self.assertFalse(results["box1.jpg"]["ok"])
        self.assertIn("unreadable", results["box1.jpg"]["error"])
        self.assertTrue(results["box2.jpg"]["ok"])
        self.assertEqual(results["box2.jpg"]["marks"], _FULL_MARKS)

    def test_oversized_file_skips_engine_in_batch(self):
        big = os.path.join(self.tmp.name, "big.jpg")
        with open(big, "wb") as f:
            f.seek(_skill._MAX_IMAGE_MB * 1024 * 1024 + 1)
            f.write(b"\0")
        calls = []

        def record(paths):
            calls.extend(paths)
            return [{"file": p, "lines": _FULL_LINES} for p in paths]
        self._patch_ocr(record)
        out = json.loads(_skill.read_box_shipping_marks(self.tmp.name))
        results = {r["file"]: r for r in out["results"]}
        self.assertFalse(results["big.jpg"]["ok"])
        self.assertIn("太大", results["big.jpg"]["error"])
        self.assertTrue(results["box1.jpg"]["ok"])
        self.assertNotIn(big, calls)  # 超大檔不該進引擎

    def test_directory_without_images(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.makedirs(empty)
        out = _skill.read_box_shipping_marks(empty)
        self.assertIn("沒有可辨識的圖片", out)


class EmployeeScopeTests(unittest.TestCase):
    """部門色 session 的路徑圈地：只放行 Telegram 上傳目錄、禁資料夾。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.upload_root = os.path.join(self.tmp.name, "uploads")
        os.makedirs(self.upload_root)
        env = mock.patch.dict(os.environ,
                              {"RED_TELEGRAM_UPLOAD_DIR": self.upload_root})
        env.start()
        self.addCleanup(env.stop)
        p = mock.patch.object(
            _skill, "_run_local_ocr",
            side_effect=lambda paths: [{"file": q, "lines": _FULL_LINES} for q in paths])
        p.start()
        self.addCleanup(p.stop)
        lex = mock.patch.object(
            _skill, "_lexicon_path",
            return_value=os.path.join(self.tmp.name, "lexicon.json"))
        lex.start()
        self.addCleanup(lex.stop)

    def _indigo_context(self):
        from agent_core.agents.middleware import AgentRequest, agent_request_context
        from agent_core.agents.permission_matrix import Agent
        return agent_request_context(AgentRequest(
            caller=Agent.INDIGO, target=Agent.INDIGO,
            intent="telegram.freeform.read_box_shipping_marks", payload={},
        ))

    def _write_img(self, directory, name="box.jpg"):
        p = os.path.join(directory, name)
        with open(p, "wb") as f:
            f.write(b"\xff\xd8fakejpeg")
        return p

    def test_employee_blocked_outside_upload_root(self):
        outside = self._write_img(self.tmp.name)
        with self._indigo_context():
            out = _skill.read_box_shipping_marks(outside)
        self.assertIn("員工帳號", out)

    def test_employee_allowed_inside_upload_root(self):
        inside = self._write_img(self.upload_root)
        with self._indigo_context():
            out = _skill.read_box_shipping_marks(inside)
        self.assertEqual(json.loads(out), _FULL_MARKS)

    def test_employee_directory_batch_blocked_even_inside_root(self):
        day_dir = os.path.join(self.upload_root, "2026-07-21")
        os.makedirs(day_dir)
        self._write_img(day_dir)
        with self._indigo_context():
            out = _skill.read_box_shipping_marks(day_dir)
        self.assertIn("一次辨識一張", out)

    def test_owner_context_unrestricted(self):
        outside = self._write_img(self.tmp.name)
        out = _skill.read_box_shipping_marks(outside)
        self.assertEqual(json.loads(out), _FULL_MARKS)


class CorrectionFeedbackTests(unittest.TestCase):
    """糾正回饋迴圈：lexicon 即時更新 + 訓練樣本累積。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, val in [
            ("_lexicon_path", os.path.join(self.tmp.name, "lexicon.json")),
            ("_training_dir", os.path.join(self.tmp.name, "training")),
        ]:
            p = mock.patch.object(_skill, target, return_value=val)
            p.start()
            self.addCleanup(p.stop)

    def _lexicon(self):
        with open(os.path.join(self.tmp.name, "lexicon.json"), encoding="utf-8") as f:
            return json.load(f)

    def _labels(self):
        path = os.path.join(self.tmp.name, "training", "labels.jsonl")
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_customer_correction_learns_and_applies(self):
        out = _skill.record_box_ocr_correction("customer", "宏達", wrong_text="宏遠")
        self.assertIn("✅", out)
        lex = self._lexicon()
        self.assertIn("宏達", lex["customers"])
        self.assertEqual(lex["confusions"]["customer"]["宏遠"], "宏達")
        # 下一張立即生效：誤讀宏遠 → 校正回宏達
        marks, corr = _skill._apply_lexicon(
            {"customer_name": "宏遠",
             "reference_entry": {"key": None, "value": None}, "circled_count": None})
        self.assertEqual(marks["customer_name"], "宏達")
        self.assertTrue(corr)

    def test_field_alias_chinese(self):
        out = _skill.record_box_ocr_correction("客戶名", "名美", wrong_text="名媛")
        self.assertIn("✅", out)

    def test_ref_value_must_be_digits(self):
        self.assertIn("純數字", _skill.record_box_ocr_correction("ref_value", "abc"))
        out = _skill.record_box_ocr_correction("ref_value", "2384")
        self.assertIn("✅", out)
        self.assertIn("2384", self._lexicon()["ref_values"])

    def test_count_archived_but_not_whitelisted(self):
        out = _skill.record_box_ocr_correction("count", "22", wrong_text="23")
        self.assertIn("訓練標註", out)
        self.assertNotIn("counts", self._lexicon())
        self.assertEqual(self._labels()[0]["correct"], "22")

    def test_invalid_field_rejected(self):
        self.assertIn("❌", _skill.record_box_ocr_correction("nope", "x"))

    def test_empty_correct_rejected(self):
        self.assertIn("不能空", _skill.record_box_ocr_correction("customer", "  "))

    def test_image_archived_with_hash_dedupe(self):
        img = os.path.join(self.tmp.name, "box.jpg")
        with open(img, "wb") as f:
            f.write(b"\xff\xd8samejpeg")
        _skill.record_box_ocr_correction("customer", "名美", wrong_text="名媛",
                                         image_path=img)
        _skill.record_box_ocr_correction("ref_key", "KH", wrong_text="何",
                                         image_path=img)
        labels = self._labels()
        self.assertEqual(len(labels), 2)
        images = os.listdir(os.path.join(self.tmp.name, "training", "images"))
        self.assertEqual(len(images), 1)  # 同一張照片只存一份
        self.assertTrue(all(e["image"] for e in labels))

    def test_missing_image_path_still_records_text(self):
        out = _skill.record_box_ocr_correction(
            "customer", "名美", image_path=os.path.join(self.tmp.name, "gone.jpg"))
        self.assertIn("✅", out)
        self.assertIn("找不到", out)
        self.assertEqual(self._labels()[0]["image"], "")

    def test_correction_text_sanitized_before_persist(self):
        # 更正文字入庫前必須過 sanitize_for_llm（lexicon 值會回流 LLM context）
        with mock.patch("agent_core.prompt_injection.sanitize_for_llm",
                        side_effect=lambda s: s.replace("Ｘ", "")):
            _skill.record_box_ocr_correction("customer", "宏Ｘ達", wrong_text="宏Ｘ遠")
        lex = self._lexicon()
        self.assertIn("宏達", lex["customers"])
        self.assertNotIn("宏Ｘ達", lex["customers"])
        self.assertEqual(lex["confusions"]["customer"].get("宏遠"), "宏達")

    def test_default_lexicon_not_mutated_by_record(self):
        # 回歸：_load_lexicon 淺拷貝時代，record 會把值 append 進模組層級種子
        before = json.dumps(_skill._DEFAULT_LEXICON, ensure_ascii=False, sort_keys=True)
        _skill.record_box_ocr_correction("ref_value", "7777")
        after = json.dumps(_skill._DEFAULT_LEXICON, ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after)

    def test_employee_context_refused(self):
        from agent_core.agents.middleware import AgentRequest, agent_request_context
        from agent_core.agents.permission_matrix import Agent
        req = AgentRequest(caller=Agent.INDIGO, target=Agent.INDIGO,
                           intent="telegram.freeform.record_box_ocr_correction",
                           payload={})
        with agent_request_context(req):
            out = _skill.record_box_ocr_correction("customer", "名美")
        self.assertIn("回報給大王", out)


class WiringTests(unittest.TestCase):
    def test_exported_in_skill_tools(self):
        self.assertIn(_skill.read_box_shipping_marks, _skill.SKILL_TOOLS)
        self.assertIn(_skill.record_box_ocr_correction, _skill.SKILL_TOOLS)

    def test_tier_is_safe(self):
        from agent_core.tool_tiers import TIER_SAFE, get_tier
        self.assertEqual(get_tier("read_box_shipping_marks"), TIER_SAFE)

    def test_correction_tool_tier_confirm_and_not_employee_whitelisted(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        from agent_core.tool_tiers import TIER_CONFIRM, get_tier
        self.assertEqual(get_tier("record_box_ocr_correction"), TIER_CONFIRM)
        self.assertNotIn("record_box_ocr_correction",
                         allowed_tool_names_for_color("indigo"))

    def test_indigo_home_tools_whitelisted(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        self.assertIn("read_box_shipping_marks",
                      allowed_tool_names_for_color("indigo"))

    def test_runner_script_exists_and_has_marker(self):
        runner = os.path.join(path_safety._REPO_ROOT, "scripts", "box_ocr_runner.py")
        self.assertTrue(os.path.isfile(runner))
        with open(runner, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('RESULT_MARKER = "RESULT_JSON:"', src)
        self.assertNotIn("agent_core", src)  # 專用 venv 跑，不得 import repo 內模組


if __name__ == "__main__":
    unittest.main()
