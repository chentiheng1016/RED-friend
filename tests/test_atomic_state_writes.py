"""torn-file 家族：持久狀態檔一律原子寫（tmp + os.replace），損毀不得靜默歸零。

案史：google_auth 的 token.json 曾因非 atomic 直寫被斷電/併發撞爛（torn JSON），
整艦隊每次啟動炸 JSONDecodeError = OAuth 假死。daemon 被 SIGKILL（launchd 5 秒
寬限到期）時，任何直寫中的檔都可能撕裂。

2026-08-29 全 repo 掃描（AST 找 open(...,"w") 且無 os.replace/_atomic 手法、
路徑像持久狀態）剩三個站點，其中 eval_rag 是雙重家族命中：
  ①save_golden_set 直寫；②load_golden_set 把「損毀」和「不存在」摺疊成同一個
  return [] —— 下一次 add_to_golden_set 會拿空清單覆寫回去，**人工策展的
  golden set 靜默歸零**，連可救援的位元組都沒了。

這裡釘的不變式：
  1. 三個站點的寫入都走 _atomic_write_text。
  2. golden set 損毀時：原檔搬到 .corrupt-<ts> 保全、回 []；之後的 add 不會
     蓋掉保全檔（資料可人工救回）。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class GoldenSetCorruptionTests(unittest.TestCase):
    """load 端的損毀分流 —— 「讀不到 ≠ 空的」家族。"""

    def setUp(self):
        from agent_core import eval_rag
        self.er = eval_rag
        os.makedirs(os.path.dirname(eval_rag._GOLDEN_FILE), exist_ok=True)
        # 清掉前一測試的殘留
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        d = os.path.dirname(self.er._GOLDEN_FILE)
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.startswith(os.path.basename(self.er._GOLDEN_FILE)):
                    os.remove(os.path.join(d, name))

    def _corrupt_files(self):
        d = os.path.dirname(self.er._GOLDEN_FILE)
        base = os.path.basename(self.er._GOLDEN_FILE)
        return [n for n in os.listdir(d) if n.startswith(base + ".corrupt-")]

    def test_missing_file_is_plain_empty(self):
        self.assertEqual(self.er.load_golden_set(), [])
        self.assertEqual(self._corrupt_files(), [])

    def test_corrupt_file_is_quarantined_not_silently_emptied(self):
        with open(self.er._GOLDEN_FILE, "w", encoding="utf-8") as f:
            f.write('{"torn": [truncated…')          # 模擬 torn write
        out = self.er.load_golden_set()
        self.assertEqual(out, [])
        cf = self._corrupt_files()
        self.assertEqual(len(cf), 1, "損毀原檔必須被搬去保全，不能留在原地等著被覆寫")
        self.assertFalse(os.path.exists(self.er._GOLDEN_FILE))

    def test_add_after_corruption_does_not_destroy_evidence(self):
        """原始 bug 的完整劇本：損毀 → add → 舊版會把災難固化成 1 筆的新檔。"""
        with open(self.er._GOLDEN_FILE, "w", encoding="utf-8") as f:
            f.write('{"torn": [truncated…')
        self.er.load_golden_set()                     # 觸發保全
        self.er.add_to_golden_set("測試查詢", ["thread123"], category="t")
        # 新檔只有這一筆（預期），但損毀位元組仍在保全檔裡可救
        with open(self.er._GOLDEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)
        cf = self._corrupt_files()
        self.assertEqual(len(cf), 1)
        with open(os.path.join(os.path.dirname(self.er._GOLDEN_FILE), cf[0]),
                  encoding="utf-8") as f:
            self.assertIn("torn", f.read())

    def test_save_load_roundtrip(self):
        entries = [{"id": "q001", "query": "浩筌 LOT03", "expected_thread_ids": ["a"]}]
        self.er.save_golden_set(entries)
        self.assertEqual(self.er.load_golden_set(), entries)


class AtomicRoutingTests(unittest.TestCase):
    """三個站點的寫入必須走 _atomic_write_text（不是裸 open("w")）。"""

    def test_save_golden_set_routes_through_atomic_writer(self):
        from agent_core import eval_rag
        with mock.patch.object(eval_rag, "_atomic_write_text") as aw:
            eval_rag.save_golden_set([{"id": "q001"}])
        aw.assert_called_once()
        path, text = aw.call_args.args[:2]
        self.assertEqual(path, eval_rag._GOLDEN_FILE)
        self.assertEqual(json.loads(text), [{"id": "q001"}])

    def test_alias_table_source_uses_atomic_writer(self):
        """build_alias_table 要吃整個 email lake，不宜整隻跑 —— 用結構斷言釘住
        寫入站點（同 tests/test_calendar_sanitization.py 的手法）。"""
        import inspect
        from agent_core import entity_resolver
        src = inspect.getsource(entity_resolver)
        self.assertIn("_atomic_write_text(_ALIAS_FILE", src)
        self.assertNotIn('open(_ALIAS_FILE, "w"', src)

    def test_probe_latest_files_use_atomic_writer(self):
        from agent_core import erp_schema_probe as probe
        import tempfile
        report = {
            "owner": "TESTOWNER", "generated_at": "2026-08-29",
            "tables": [], "views": [], "summary": {},
        }
        out = tempfile.mkdtemp(prefix="probe_test_")
        with mock.patch.object(probe, "render_markdown", return_value="# md"), \
                mock.patch.object(probe, "_atomic_write_text") as aw:
            probe.write_report(report, out_dir=out)
        latest = [c.args[0] for c in aw.call_args_list if "latest" in c.args[0]]
        self.assertEqual(len(latest), 2, "latest.md + latest.json 都要走原子寫")


if __name__ == "__main__":
    unittest.main()
