"""生圖記帳 + soffice 轉檔韌性。

2026-08-04 查帳發現：cost.jsonl **全歷史 0 筆生圖**（nano-banana/imagen），不是
沒生過圖，是生圖工具拿 `_get_gemini_client()` 直接呼 `client.models.generate_content`
繞過了 `_gemini_generate_once` 那層 record_call。查「yellow 昨天花很多是不是生圖」
時，「帳本查無生圖」差點被當成「沒生圖」的證據。

同一輪實測還踩到：殘留的 LibreOffice instance 佔住共用 profile 鎖 → 樣品單抽圖
卡 120s 後 TimeoutExpired **直接從工具裡噴出去**（check=False 不擋逾時）。

注意（unittest discover）：conftest fixture 不生效，隔離全在 setUp/tearDown。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


class ImagePricingTests(unittest.TestCase):
    """生圖 SKU 要有自己的費率，不能被文字模型前綴撈走。"""

    def test_flash_image_not_priced_as_flash_text(self):
        from agent_core.cost_tracker import _price_for_model
        img = _price_for_model("gemini-2.5-flash-image")
        txt = _price_for_model("gemini-2.5-flash")
        self.assertNotEqual(img, txt)
        self.assertGreater(img[1], txt[1] * 10)

    def test_one_nano_banana_image_lands_near_list_price(self):
        """牌價 ~$0.134/張；實測 API 回報一張圖 ~1,357 output tokens。"""
        from agent_core.cost_tracker import compute_cost_usd
        cost = compute_cost_usd("nano-banana-pro-preview", 467, 1357)
        self.assertGreater(cost, 0.05)
        self.assertLess(cost, 0.30)


class GenerateContentTrackedTests(unittest.TestCase):
    """公開入口要真的把 usage 餵進 cost_tracker，且不換 fallback model。"""

    def test_records_cost_with_explicit_caller(self):
        from agent_core import gemini_client as gc
        usage = mock.MagicMock(prompt_token_count=467, candidates_token_count=1357,
                               cached_content_token_count=0, thoughts_token_count=0,
                               tool_use_prompt_token_count=0, total_token_count=1824)
        resp = mock.MagicMock(usage_metadata=usage, model_version="nano-banana-pro-preview")
        fake = mock.MagicMock()
        fake.models.generate_content.return_value = resp
        with mock.patch.object(gc, "_get_gemini_client", return_value=fake), \
                mock.patch("agent_core.cost_tracker.record_call") as rec:
            out = gc.generate_content_tracked(
                model="nano-banana-pro-preview", contents=["x"],
                caller="sample_order.generate_from_order")
        self.assertIs(out, resp)
        rec.assert_called_once()
        self.assertEqual(rec.call_args.kwargs["caller"], "sample_order.generate_from_order")
        self.assertEqual(rec.call_args.kwargs["model"], "nano-banana-pro-preview")

    def test_does_not_fall_back_to_a_text_model(self):
        """生圖失敗換文字 fallback 只會拿到不能用的回應 —— 要直接拋。"""
        from agent_core import gemini_client as gc
        fake = mock.MagicMock()
        fake.models.generate_content.side_effect = RuntimeError("400 invalid image request")
        # 放棄路徑會呼 cost_tracker.record_api_error 寫 api_errors.jsonl（上面那個
        # 成功案例 mock 了 record_call，這裡漏 mock 對應的失敗版）。不擋的話每跑
        # 一次測試就在真的錯誤帳裡多一筆 gemini/nano-banana 假失敗，而那個檔是
        # dashboard_alerts「外部 API 錯誤率」紅線的分子。
        with mock.patch.object(gc, "_get_gemini_client", return_value=fake), \
                mock.patch("agent_core.cost_tracker.record_api_error") as err, \
                mock.patch.object(gc, "_gemini_fallback_model") as fb:
            with self.assertRaises(RuntimeError):
                gc.generate_content_tracked(model="nano-banana-pro-preview",
                                            contents=["x"], caller="t")
        fb.assert_not_called()
        err.assert_called_once()  # 仍要記一筆（只是不落到真的檔案）


class ImageToolsGoThroughTrackedPathTests(unittest.TestCase):
    """釘死：生圖工具不准再直接呼 client.models.generate_content。"""

    def test_no_direct_generate_content_in_image_paths(self):
        for rel in ("skills/image_gen.py", "skills/sample_order.py",
                    "agent_core/erp_executor.py"):
            with open(os.path.join(_REPO_ROOT, rel), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("client.models.generate_content", src, rel)
            self.assertIn("generate_content_tracked", src, rel)


class SofficeConvertTests(unittest.TestCase):
    """轉檔失敗一律回 ""，絕不讓例外從工具裡噴出去。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "image1.emf")
        with open(self.src, "wb") as f:
            f.write(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_timeout_returns_empty_not_raise(self):
        from skills import sample_order as so
        with mock.patch.object(so, "_find_soffice", return_value="/bin/soffice"), \
                mock.patch("subprocess.run",
                           side_effect=subprocess.TimeoutExpired(cmd="soffice", timeout=120)):
            self.assertEqual(so._soffice_convert(self.src, "png", self.tmp.name), "")

    def test_missing_binary_returns_empty(self):
        from skills import sample_order as so
        with mock.patch.object(so, "_find_soffice", return_value=""):
            self.assertEqual(so._soffice_convert(self.src, "png", self.tmp.name), "")

    def test_silent_failure_without_output_file_returns_empty(self):
        """soffice 有 exit 0 卻沒產出檔的靜默失敗模式 —— 認產出檔不認 exit code。"""
        from skills import sample_order as so
        with mock.patch.object(so, "_find_soffice", return_value="/bin/soffice"), \
                mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0)):
            self.assertEqual(so._soffice_convert(self.src, "png", self.tmp.name), "")

    def test_uses_isolated_user_profile(self):
        """共用 profile 有單一 instance 鎖：GUI/夜跑轉檔開著就會卡到逾時。"""
        from skills import sample_order as so
        captured = {}

        def _run(cmd, **kwargs):
            captured["cmd"] = cmd
            with open(os.path.join(self.tmp.name, "image1.png"), "wb") as f:
                f.write(b"png")
            return mock.MagicMock(returncode=0)

        with mock.patch.object(so, "_find_soffice", return_value="/bin/soffice"), \
                mock.patch("subprocess.run", side_effect=_run):
            out = so._soffice_convert(self.src, "png", self.tmp.name)
        self.assertTrue(out.endswith("image1.png"))
        self.assertTrue(any(str(a).startswith("-env:UserInstallation=file://")
                            for a in captured["cmd"]),
                        f"沒帶獨立 profile：{captured['cmd']}")

    def test_extract_order_images_survives_soffice_failure(self):
        """抽圖整條不能因為轉檔失敗就炸掉（emf 抽不到就少一張，不是整個工具死）。"""
        import zipfile
        from skills import sample_order as so
        xlsx = os.path.join(self.tmp.name, "order.xlsx")
        with zipfile.ZipFile(xlsx, "w") as z:
            z.writestr("xl/media/image1.emf", b"emf-bytes")
            z.writestr("xl/media/image2.png", b"png-bytes")
        with mock.patch.object(so, "_soffice_convert", return_value=""):
            pngs = so._extract_order_images(xlsx)
        self.assertEqual([os.path.basename(p) for p in pngs], ["image2.png"])


if __name__ == "__main__":
    unittest.main()
