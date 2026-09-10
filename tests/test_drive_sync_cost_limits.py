"""drive_sync 每日成本斷路器：程式預設與 plist 部署值必須一致、且在美元刻度。

2026-08-05 踩到：`cost.jsonl` 的刻度換過三個紀元（① _PRICING 嚴重低估 →
② 08-01 帳單數字但把新台幣當美元、放大 ~32× → ③ 08-05 官方 USD 牌價），而這兩條
斷路器比對的正是那本帳的當日金額。期間程式預設被機械換算過（$1→$100→$3.0），
plist 卻始終停在 1.00/5.00 —— 兩邊誰都對不上誰，而且因為 image ingest 預設關著
（RAG_DRIVE_ENABLE_IMAGE_INGEST），沒有人會發現。

這裡釘三件事：
  1. 程式預設 == plist 部署值（漂移即紅）
  2. 兩者都在美元刻度（TWD 刻度會是 ~32 倍，>50 就是有人又寫錯幣別）
  3. media 比 image 緊（單件成本高得多，這是刻意的設計意圖）

注意（unittest discover）：conftest fixture 不生效；不 hardcode 任何 /Users/... 路徑。
"""
import os
import plistlib
import unittest
from pathlib import Path

from agent_core.path_safety import _REPO_ROOT

_PLIST = (Path(_REPO_ROOT) / "launchd" / "templates"
          / "com.xiaohong.rag_sync_daily.plist")

# (plist / env key, drive_sync 模組屬性)
_LIMITS = (
    ("RAG_IMAGE_DAILY_COST_LIMIT_USD", "_IMAGE_DAILY_COST_LIMIT_USD"),
    ("RAG_MEDIA_DAILY_COST_LIMIT_USD", "_MEDIA_DAILY_COST_LIMIT_USD"),
)

# 美元刻度的上界。真實日燒約 US$10–13（全艦隊），單一 ingest 路徑的斷路器
# 不可能需要 >$50；TWD 刻度會落在 32× 附近，一定會撞破這條。
_USD_SCALE_CEILING = 50.0


def _plist_env() -> dict:
    with open(_PLIST, "rb") as f:
        return plistlib.load(f).get("EnvironmentVariables", {})


class DriveSyncCostLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = _plist_env()

    def test_plist_defines_both_limits(self):
        """守門本身要有牙齒：key 被改名/刪掉時，下面的比對會假綠。"""
        for env_key, _attr in _LIMITS:
            self.assertIn(env_key, self.env,
                          f"{_PLIST.name} 少了 {env_key}")

    def test_code_default_matches_plist_value(self):
        from agent_core.ingest import drive_sync
        for env_key, attr in _LIMITS:
            with self.subTest(env_key):
                if os.environ.get(env_key):
                    self.skipTest(f"{env_key} 被環境覆蓋，測不到程式預設")
                plist_val = float(self.env[env_key])
                self.assertAlmostEqual(
                    getattr(drive_sync, attr), plist_val, places=4,
                    msg=(f"{attr} 的程式預設與 plist 的 {env_key} 不一致 —— "
                         "兩邊要嘛一起改、要嘛就別在 plist 覆蓋"))

    def test_limits_are_usd_scale_not_twd(self):
        """幣別回歸：帳本曾經是新台幣（~32×），這兩條門檻跟著錯過一輪。"""
        from agent_core.ingest import drive_sync
        for env_key, attr in _LIMITS:
            with self.subTest(env_key):
                plist_val = float(self.env[env_key])
                self.assertLess(
                    plist_val, _USD_SCALE_CEILING,
                    f"plist 的 {env_key}={plist_val} 看起來是新台幣刻度")
                if not os.environ.get(env_key):
                    self.assertLess(
                        getattr(drive_sync, attr), _USD_SCALE_CEILING,
                        f"{attr} 看起來是新台幣刻度")

    def test_media_budget_is_tighter_than_image(self):
        """媒體走 flash 抽幀 + pro deep 分析，單件比圖片貴得多 —— 這個相對關係
        是刻意的，反過來就代表有人搞錯了哪條該緊。"""
        image = float(self.env["RAG_IMAGE_DAILY_COST_LIMIT_USD"])
        media = float(self.env["RAG_MEDIA_DAILY_COST_LIMIT_USD"])
        self.assertLessEqual(media, image)

    def test_limits_are_positive(self):
        """0 = 停用斷路器。真的要停用得是明確決定，不該由漂移造成。"""
        for env_key, _attr in _LIMITS:
            with self.subTest(env_key):
                self.assertGreater(float(self.env[env_key]), 0.0)


if __name__ == "__main__":
    unittest.main()
