"""Tests for agent_core.daemons.telegram.history.

This module was extracted from daemon_telegram.py with zero behavior
change; adding direct coverage now so future tweaks (turn-cap, TTL,
legacy migration) don't silently regress.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("AGENT_DAEMON_MODE", "1")


class ChatHistoryPathSafetyTests(unittest.TestCase):
    """Filename derivation must keep arbitrary chat_id input inside the dir."""

    def test_alphanumeric_chat_id_writes_digits_only_filename(self):
        from agent_core.daemons.telegram import history as h
        # Even if someone passes "12345abc", only the digits remain.
        with mock.patch.object(h, "_TG_CHAT_HISTORY_DIR", "/tmp/red-test"):
            path = h._chat_history_path("12345abc")
            self.assertEqual(os.path.basename(path), "12345.json")

    def test_traversal_chars_stripped(self):
        from agent_core.daemons.telegram import history as h
        with mock.patch.object(h, "_TG_CHAT_HISTORY_DIR", "/tmp/red-test"):
            path = h._chat_history_path("../../../etc/passwd")
            # Slashes and dots are dropped — result lives under our dir.
            self.assertTrue(os.path.basename(path).endswith(".json"))
            self.assertNotIn("..", path)
            self.assertNotIn("etc", path)

    def test_empty_chat_id_becomes_unknown(self):
        from agent_core.daemons.telegram import history as h
        with mock.patch.object(h, "_TG_CHAT_HISTORY_DIR", "/tmp/red-test"):
            path = h._chat_history_path("")
            self.assertEqual(os.path.basename(path), "unknown.json")

    def test_group_chat_negative_id_preserved(self):
        from agent_core.daemons.telegram import history as h
        with mock.patch.object(h, "_TG_CHAT_HISTORY_DIR", "/tmp/red-test"):
            path = h._chat_history_path("-1001234567890")
            self.assertEqual(os.path.basename(path), "-1001234567890.json")


class RecordTurnTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        from agent_core.daemons.telegram import history as h
        self._patchers = [
            mock.patch.object(h, "_TG_CHAT_HISTORY_DIR", self.tmpdir),
            mock.patch.object(h, "_TG_CHAT_HISTORY_FILE_LEGACY",
                              os.path.join(self.tmpdir, "_legacy.json")),
        ]
        for p in self._patchers:
            p.start()

    def _cleanup(self):
        for p in self._patchers:
            p.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_record_and_rebuild_round_trips(self):
        from agent_core.daemons.telegram import history as h
        h._record_tg_chat_turn("123", "hi", "hello")
        h._record_tg_chat_turn("123", "qty?", "12 boxes")
        rebuilt = h._load_tg_chat_history_for_rebuild("123")
        self.assertEqual(len(rebuilt), 4)  # 2 turns × (user + model)
        self.assertEqual(rebuilt[0]["role"], "user")
        self.assertEqual(rebuilt[0]["parts"][0]["text"], "hi")
        self.assertEqual(rebuilt[3]["role"], "model")
        self.assertEqual(rebuilt[3]["parts"][0]["text"], "12 boxes")

    def test_turn_cap_drops_oldest(self):
        from agent_core.daemons.telegram import history as h
        with mock.patch.object(h, "_TG_CHAT_HISTORY_MAX_TURNS_PER_CHAT", 3):
            for i in range(5):
                h._record_tg_chat_turn("123", f"q{i}", f"a{i}")
            rebuilt = h._load_tg_chat_history_for_rebuild("123")
            # Cap = 3 turn-pairs = 6 entries. Oldest pairs (q0, q1) dropped.
            self.assertEqual(len(rebuilt), 6)
            self.assertEqual(rebuilt[0]["parts"][0]["text"], "q2")

    def test_ttl_expired_history_returns_empty(self):
        from agent_core.daemons.telegram import history as h
        h._record_tg_chat_turn("123", "hi", "hello")
        # Tamper with persisted updated_ts to be ancient.
        path = h._chat_history_path("123")
        with open(path) as f:
            data = json.load(f)
        data["updated_ts"] = time.time() - h._TG_CHAT_HISTORY_TTL_S - 1
        with open(path, "w") as f:
            json.dump(data, f)
        self.assertEqual(h._load_tg_chat_history_for_rebuild("123"), [])

    def test_clear_history_removes_file(self):
        from agent_core.daemons.telegram import history as h
        h._record_tg_chat_turn("123", "hi", "hello")
        path = h._chat_history_path("123")
        self.assertTrue(os.path.exists(path))
        h._clear_tg_chat_history("123")
        self.assertFalse(os.path.exists(path))

    def test_empty_chat_id_record_is_noop(self):
        from agent_core.daemons.telegram import history as h
        h._record_tg_chat_turn("", "hi", "hello")  # must not crash, must not create file
        self.assertEqual(h._load_tg_chat_history_for_rebuild(""), [])

    def test_corrupt_history_file_treated_as_empty(self):
        from agent_core.daemons.telegram import history as h
        path = h._chat_history_path("999")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("<<not json>>")
        self.assertEqual(h._load_tg_chat_history_for_rebuild("999"), [])


class LegacyMigrationTests(unittest.TestCase):
    """First touch of a chat with legacy entries should migrate + delete."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.legacy_path = os.path.join(self.tmpdir, "_legacy.json")
        from agent_core.daemons.telegram import history as h
        self._patchers = [
            mock.patch.object(h, "_TG_CHAT_HISTORY_DIR", self.tmpdir),
            mock.patch.object(h, "_TG_CHAT_HISTORY_FILE_LEGACY", self.legacy_path),
            # Reset cache so the patched legacy path actually gets read.
            mock.patch.object(h, "_tg_chat_history_legacy_cache",
                              {"mtime": 0.0, "data": {}}),
        ]
        for p in self._patchers:
            p.start()

    def _cleanup(self):
        for p in self._patchers:
            p.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_legacy_entry_migrates_to_per_chat_then_removed(self):
        from agent_core.daemons.telegram import history as h
        legacy = {
            "123": {
                "updated_ts": time.time(),
                "turns": [{"role": "user", "text": "from-legacy"},
                          {"role": "model", "text": "ok"}],
            },
            "999": {
                "updated_ts": time.time(),
                "turns": [{"role": "user", "text": "other-chat-untouched"}],
            },
        }
        with open(self.legacy_path, "w") as f:
            json.dump(legacy, f)

        # Touching chat 123 should migrate it; chat 999 should be untouched.
        rebuilt = h._load_tg_chat_history_for_rebuild("123")
        self.assertEqual(len(rebuilt), 2)
        self.assertEqual(rebuilt[0]["parts"][0]["text"], "from-legacy")
        # Per-chat file now exists.
        self.assertTrue(os.path.exists(h._chat_history_path("123")))
        # Legacy file still has chat 999 only.
        with open(self.legacy_path) as f:
            remaining = json.load(f)
        self.assertNotIn("123", remaining)
        self.assertIn("999", remaining)


if __name__ == "__main__":
    unittest.main()


class ChatHistoryCharBudgetTests(unittest.TestCase):
    """餵回 session 的歷史要吃字元預算，不是只數則數。

    2026-08-04 查帳：telegram_chat.yellow 一天 $75.92／17 次呼叫，rebuild 後的
    prompt 底盤就 6.1 萬 tokens —— 因為 40 對話 × 每則 8,000 字元的上限只管則數。
    實測 UserA 的歷史 71,504 字元 ≈ 3.6 萬 tokens 全被餵回去。
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        from agent_core.daemons.telegram import history as h
        self._patchers = [
            mock.patch.object(h, "_TG_CHAT_HISTORY_DIR", self.tmpdir),
            mock.patch.object(h, "_TG_CHAT_HISTORY_FILE_LEGACY",
                              os.path.join(self.tmpdir, "_legacy.json")),
        ]
        for p in self._patchers:
            p.start()

    def _cleanup(self):
        for p in self._patchers:
            p.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed(self, pairs: int, chars: int) -> None:
        from agent_core.daemons.telegram import history as h
        for i in range(pairs):
            h._record_tg_chat_turn("123", f"q{i}" + "x" * chars, f"a{i}" + "y" * chars)

    def test_budget_keeps_newest_and_drops_oldest(self):
        from agent_core.daemons.telegram import history as h
        self._seed(pairs=10, chars=1000)          # 20 則 × ~1,001 字元
        rebuilt = h._load_tg_chat_history_for_rebuild("123", max_chars=3000)
        self.assertLessEqual(sum(len(t["parts"][0]["text"]) for t in rebuilt), 3000)
        # 保留的是最新那幾則
        self.assertTrue(rebuilt[-1]["parts"][0]["text"].startswith("a9"))
        self.assertFalse(any(t["parts"][0]["text"].startswith("q0") for t in rebuilt))

    def test_keeps_chronological_order_without_holes(self):
        """預算滿了要整段停手，不能跳過大 turn 再撿更舊的小 turn（讀起來會跳針）。"""
        from agent_core.daemons.telegram import history as h
        h._record_tg_chat_turn("123", "old-small", "a")
        h._record_tg_chat_turn("123", "huge", "b" * 5000)
        h._record_tg_chat_turn("123", "new", "c")
        rebuilt = h._load_tg_chat_history_for_rebuild("123", max_chars=1000)
        texts = [t["parts"][0]["text"] for t in rebuilt]
        self.assertNotIn("old-small", texts)   # 被大 turn 擋在預算外就不再往前撿

    def test_single_oversized_turn_still_returned(self):
        """至少保留最新一則，否則超長答案會讓記憶整個歸零。"""
        from agent_core.daemons.telegram import history as h
        h._record_tg_chat_turn("123", "q", "z" * 5000)
        rebuilt = h._load_tg_chat_history_for_rebuild("123", max_chars=100)
        self.assertEqual(len(rebuilt), 1)
        self.assertTrue(rebuilt[0]["parts"][0]["text"].startswith("z"))

    def test_employee_budget_is_tighter_than_default(self):
        from agent_core.daemons.telegram import history as h
        self.assertLess(h._TG_CHAT_HISTORY_MAX_CHARS_EMPLOYEE,
                        h._TG_CHAT_HISTORY_MAX_CHARS)

    def test_default_budget_applies_when_not_specified(self):
        from agent_core.daemons.telegram import history as h
        self._seed(pairs=6, chars=3000)   # ~36,000 字元 > 預設 24,000
        rebuilt = h._load_tg_chat_history_for_rebuild("123")
        self.assertLessEqual(sum(len(t["parts"][0]["text"]) for t in rebuilt),
                             h._TG_CHAT_HISTORY_MAX_CHARS)
