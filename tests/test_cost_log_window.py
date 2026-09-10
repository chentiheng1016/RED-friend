"""健檢 Medium regression: cost.jsonl / api_errors.jsonl 的 bounded-window 查詢
不再全掃整個歷史——_load_jsonl_window 反向讀、一過 cutoff(含 grace)就早停。
驗證：反向讀正確（含跨 block 的 tail-carry）、視窗結果與舊前向過濾等價、hours=None 全量。
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from agent_core import cost_tracker as ct


class CostLogWindowTests(unittest.TestCase):
    def _write(self, lines):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.addCleanup(os.remove, path)
        return path

    def test_read_lines_reverse_newest_first(self):
        path = self._write(["a", "b", "c"])
        got = [x for x in ct._read_lines_reverse(path, block_size=4) if x]
        self.assertEqual(got, ["c", "b", "a"])

    def test_tiny_block_recovers_all_lines(self):
        raw = [f'{{"i": {i}}}' for i in range(50)]  # append order: i=0 oldest … i=49 newest
        path = self._write(raw)
        got = [json.loads(x)["i"] for x in ct._read_lines_reverse(path, block_size=7) if x.strip()]
        self.assertEqual(sorted(got), list(range(50)), "tail-carry must lose/corrupt nothing")
        self.assertEqual(got, list(reversed(range(50))), "newest(last line)-first")

    def test_bounded_window_equals_forward_filter(self):
        now = datetime.now()
        # +30min offset so no entry lands exactly on the 5h boundary — otherwise the
        # microsecond drift between this now() and the function's own now() flips it.
        raw = [json.dumps({"ts": (now - timedelta(hours=200 - i, minutes=30)).isoformat(), "i": i})
               for i in range(200)]  # oldest→newest, 1h apart
        path = self._write(raw)
        got = ct._load_jsonl_window(path, hours=5)
        cutoff = now - timedelta(hours=5)
        expect = [json.loads(r) for r in raw
                  if datetime.fromisoformat(json.loads(r)["ts"]) >= cutoff]
        self.assertEqual([e["i"] for e in got], [e["i"] for e in expect])
        self.assertEqual([e["i"] for e in got], sorted(e["i"] for e in got),
                         "chronological order preserved like the old forward read")
        self.assertLess(len(got), 12, "scan bounded to the window, not all 200")

    def test_hours_none_returns_all_forward(self):
        path = self._write([json.dumps({"ts": "2026-01-01T00:00:00", "i": 1}),
                            json.dumps({"ts": "2026-06-01T00:00:00", "i": 2})])
        self.assertEqual([e["i"] for e in ct._load_jsonl_window(path, None)], [1, 2])

    def test_missing_file_returns_empty(self):
        self.assertEqual(ct._load_jsonl_window("/no/such/file.jsonl", 5), [])
        self.assertEqual(ct._load_jsonl_window("/no/such/file.jsonl", None), [])


if __name__ == "__main__":
    unittest.main()
