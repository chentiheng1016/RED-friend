"""深度影片分析（混合關鍵幀路徑）模型選擇：deep 用 pro 級模型，不打 API。"""
import unittest

from agent_core import video_understanding as vu


class VideoUpgradeTests(unittest.TestCase):
    def test_deep_uses_pro_model(self):
        self.assertIn("pro", vu._DEEP_MODEL)            # pro 級而非 flash


if __name__ == "__main__":
    unittest.main()
