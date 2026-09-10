"""build_product_photo_index 的「客戶鞋照片」樹納入（Phase C，2026-09-01）——純函式部分。

背景：Richter 185 張實照整棵樹（客戶鞋照片/Richter/FC-Husky 2.0/22w…）夾名不含
鞋型詞、_PROD 掃不到，整批不在索引。_customer_tree_labels 把命中 _CUST_ROOT 的根
之下所有子孫夾收進來、label 記相對路徑；_target_folders 合併「款號夾 ∪ 客戶樹」。
只測純函式（不打 Drive）；注意（unittest discover）：隔離全在 setUp/tearDown。
"""
import importlib.util
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "bppi_under_test",
        os.path.join(_REPO_ROOT, "scripts", "build_product_photo_index.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _f(fid, name, parent=None):
    return {"id": fid, "name": name, "parents": [parent] if parent else []}


class CustomerTreeLabelsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_script()
        cls.folders = [
            _f("R", "客戶鞋照片"),                 # 根：本身不收（放的是子夾）
            _f("r1", "Richter", "R"),
            _f("h", "FC-Husky 2.0", "r1"),
            _f("w", "22w", "h"),
            _f("img", "Image", "r1"),              # _SKIP 命中 → 連子樹剪掉
            _f("imgsub", "雜圖", "img"),
            _f("prod", "JDM131-雪靴"),             # 既有款號夾邏輯照舊
            _f("junk", "Logo"),                    # 雜夾照舊排除
        ]

    def test_descendants_get_path_labels(self):
        labels = self.mod._customer_tree_labels(self.folders)
        self.assertEqual(labels, {
            "r1": "Richter",
            "h": "Richter/FC-Husky 2.0",
            "w": "Richter/FC-Husky 2.0/22w",
        })

    def test_skip_prunes_whole_subtree(self):
        labels = self.mod._customer_tree_labels(self.folders)
        self.assertNotIn("img", labels)
        self.assertNotIn("imgsub", labels)

    def test_target_folders_union_and_labels(self):
        targets = self.mod._target_folders(self.folders)
        by_id = {f["id"]: label for f, label in targets}
        self.assertEqual(by_id, {
            "r1": "Richter",
            "h": "Richter/FC-Husky 2.0",
            "w": "Richter/FC-Husky 2.0/22w",
            "prod": "JDM131-雪靴",                 # 款號夾 label＝夾名，行為不變
        })

    def test_no_customer_root_means_old_behavior(self):
        targets = self.mod._target_folders([_f("prod", "JDM131-雪靴"), _f("junk", "Logo")])
        self.assertEqual([(f["id"], label) for f, label in targets],
                         [("prod", "JDM131-雪靴")])


if __name__ == "__main__":
    unittest.main()
