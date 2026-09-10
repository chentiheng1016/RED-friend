"""agent_core/vision.analyze_uploaded_image —— 路徑限縮看圖工具（員工通道）。

2026-07-29 UserA OZ18/OZ19 案：員工白名單無看圖工具，「依圖列出扣件總需求量」
LLM 看不到圖只能腦補數量（真實款號配虛構 10,000 雙）。此工具開給員工，但只能
讀 Telegram 上傳目錄裡的圖片檔——路徑逃逸/非圖片副檔名都要 fail-closed。
不打真 Gemini：analyze_image 一律 mock。
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import agent_core.vision as vision


class AnalyzeUploadedImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tg_uploads_")
        self.outside = tempfile.mkdtemp(prefix="outside_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)

        p = mock.patch("agent_core.exchange_policy.telegram_upload_root",
                       return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)

        self.seen = []

        def _fake_analyze(path, prompt):
            self.seen.append((path, prompt))
            return "視覺解析結果：OK"

        p2 = mock.patch.object(vision, "analyze_image",
                               side_effect=_fake_analyze)
        p2.start()
        self.addCleanup(p2.stop)

    def _touch(self, root, name):
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x")
        return path

    def test_inside_root_image_delegates(self):
        path = self._touch(self.tmp, "2026-07-29/photo_abc.jpg")
        out = vision.analyze_uploaded_image(path, "圖中雙數是多少？")
        self.assertIn("OK", out)
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.seen[0][0], os.path.realpath(path))
        # prompt 必含逐字抄錄指示與問題本文
        self.assertIn("逐字逐格抄錄", self.seen[0][1])
        self.assertIn("圖中雙數是多少？", self.seen[0][1])

    def test_outside_root_rejected(self):
        path = self._touch(self.outside, "photo_abc.jpg")
        out = vision.analyze_uploaded_image(path)
        self.assertIn("錯誤", out)
        self.assertEqual(self.seen, [])

    def test_path_traversal_rejected(self):
        self._touch(self.outside, "secret.png")
        sneaky = os.path.join(
            self.tmp, "..", os.path.basename(self.outside), "secret.png")
        out = vision.analyze_uploaded_image(sneaky)
        self.assertIn("錯誤", out)
        self.assertEqual(self.seen, [])

    def test_symlink_escape_rejected(self):
        # 上傳目錄裡的 symlink 指向外部 → realpath 解掉後必拒
        target = self._touch(self.outside, "secret.jpg")
        link = os.path.join(self.tmp, "link.jpg")
        os.symlink(target, link)
        out = vision.analyze_uploaded_image(link)
        self.assertIn("錯誤", out)
        self.assertEqual(self.seen, [])

    def test_non_image_extension_rejected(self):
        # 文件保留原檔名（可猜）——pdf/xlsx 不在此工具射程
        path = self._touch(self.tmp, "2026-07-29/財報.pdf")
        out = vision.analyze_uploaded_image(path)
        self.assertIn("錯誤", out)
        self.assertEqual(self.seen, [])

    def test_empty_path_usage_message(self):
        out = vision.analyze_uploaded_image("")
        self.assertIn("路徑", out)
        self.assertEqual(self.seen, [])

    def test_root_resolution_failure_fails_closed(self):
        with mock.patch("agent_core.exchange_policy.telegram_upload_root",
                        side_effect=RuntimeError("boom")):
            out = vision.analyze_uploaded_image(
                os.path.join(self.tmp, "a.jpg"))
        self.assertIn("錯誤", out)
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
