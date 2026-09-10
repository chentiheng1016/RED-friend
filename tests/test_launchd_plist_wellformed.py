"""launchd plist 模板必須是嚴格合法的 XML。

2026-08-05 踩到：21 個模板的註解裡寫著 `./bin/redeploy-daemons <name> --force`，
而 **XML 規格禁止註解內出現 `--`**。後果是分裂的——

  - `plutil -lint` 說 OK、launchd 照常載入（兩者的 parser 寬鬆）
  - Python 的 `plistlib.load` 直接 `xml.parsers.expat.ExpatError:
    not well-formed (invalid token)`

所以壞掉的只有「用 Python 讀 plist」這條路：手動補跑 SOP（讀 plist 的
EnvironmentVariables 整包灌進 os.environ 再跑 pipeline）、以及本 repo 已有的
3 個 plistlib 測試（test_rag_sync_guard / test_factory_warehouse_rebuild_daemon
/ test_training_video_watch_daemon——它們剛好讀到沒中招的那幾個檔）。

修法是把註解裡的 `--force` 換成 `-f`：`bin/redeploy-daemons` 的 arg parser 本來
就是 `--force|-f`，行為完全相同，而且仍可直接複製貼上執行。

這兩個測試是守門：光修好現況沒用，下次有人在註解裡寫 `--force` 又會靜默壞掉，
而且因為 plutil 說 OK、daemon 也跑得動，不會有人發現。
"""
import plistlib
import re
import unittest
from pathlib import Path

from agent_core.path_safety import _REPO_ROOT

_TEMPLATES = sorted((Path(_REPO_ROOT) / "launchd" / "templates").glob("*.plist"))


class LaunchdPlistWellFormedTests(unittest.TestCase):
    def test_templates_exist(self):
        # 守門本身要有牙齒：glob 抓不到檔案時下面兩個測試會假綠。
        self.assertGreater(len(_TEMPLATES), 20,
                           f"只找到 {len(_TEMPLATES)} 個 plist 模板，路徑推導可能壞了")

    def test_every_template_parses_with_plistlib(self):
        """嚴格 XML 解析——比 plutil -lint 嚴，剛好卡住 plutil 放行的那類問題。"""
        failures = []
        for path in _TEMPLATES:
            try:
                with open(path, "rb") as f:
                    plistlib.load(f)
            except Exception as e:  # noqa: BLE001 — 要把全部壞檔一次列出來
                failures.append(f"{path.name}: {type(e).__name__}: {e}")
        self.assertEqual(failures, [], "plist 模板無法被 plistlib 解析：\n  " +
                                       "\n  ".join(failures))

    def test_no_double_hyphen_inside_xml_comments(self):
        """直接釘住成因，讓失敗訊息能指出怎麼修（只看 plistlib 報錯很難聯想）。"""
        offenders = []
        for path in _TEMPLATES:
            text = path.read_text(encoding="utf-8")
            for comment in re.findall(r"<!--.*?-->", text, re.S):
                inner = comment[4:-3]
                for m in re.finditer(r"--+", inner):
                    frag = inner[max(0, m.start() - 30):m.end() + 15]
                    offenders.append(f"{path.name}: …{frag.strip()}…")
        self.assertEqual(
            offenders, [],
            "XML 註解內不得出現 `--`（規格禁止，plutil/launchd 寬鬆放行但 "
            "plistlib 會炸）。指令旗標請寫 `-f` 而非 `--force`"
            "（bin/redeploy-daemons 的 arg parser 是 `--force|-f`，等價）：\n  "
            + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
