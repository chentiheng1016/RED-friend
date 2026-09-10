from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock


class FactualCorrectionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        # Each test gets a fresh mistakes.json so order doesn't matter.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._path = os.path.join(self._tmp.name, "mistakes.json")
        from agent_core import mistake_ledger
        self._patcher = mock.patch.object(mistake_ledger, "MISTAKES_FILE", self._path)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        # Reset in-memory state — module-level dict survives across tests.
        mistake_ledger._mistake_ledger = {"corrections": {}, "log": []}

    def test_record_persists_entry_with_factual_correction_type(self):
        from agent_core.mistake_ledger import record_factual_correction

        record_factual_correction(
            user_correction="我查過 jalas 沒有用防水膜",
            prior_model_reply="Jalas 確實有在使用防水膜",
            matched_pattern="user_independent_check",
        )

        with open(self._path, encoding="utf-8") as f:
            data = json.load(f)
        log = data["log"]
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["type"], "factual_correction")
        self.assertIn("沒有用防水膜", log[0]["user_said"])
        self.assertIn("防水膜", log[0]["detail"])
        self.assertEqual(log[0]["resolution"], "user_independent_check")

    def test_recent_returns_only_factual_correction_entries(self):
        from agent_core import mistake_ledger
        from agent_core.mistake_ledger import (
            _log_mistake,
            record_factual_correction,
            recent_factual_corrections,
        )

        # Mix with an unrelated ASR-mishear log so we can prove the filter works.
        _log_mistake(
            mistake_type="asr_mishear",
            user_said="開啟雞蛋",
            detail="should be GitHub",
            resolution="GitHub",
        )
        record_factual_correction(
            user_correction="我查過 jalas 沒有用防水膜",
            prior_model_reply="Jalas 用 Sympatex 防水膜",
            matched_pattern="user_independent_check",
        )
        record_factual_correction(
            user_correction="1155 報價不對",
            prior_model_reply="1155 報價 25 EUR",
            matched_pattern="user_short_denial_with_fact",
        )

        out = recent_factual_corrections(limit=10)
        self.assertIn("最近 2 筆", out)
        self.assertIn("沒有用防水膜", out)
        self.assertIn("1155 報價不對", out)
        # The ASR-mishear entry must NOT leak in.
        self.assertNotIn("雞蛋", out)
        # Closing nudge so the agent re-queries instead of repeating.
        self.assertIn("query_bom", out)
        # In-memory state is consistent with the persisted file.
        self.assertEqual(
            sum(1 for e in mistake_ledger._mistake_ledger["log"]
                if e.get("type") == "factual_correction"),
            2,
        )

    def test_recent_with_empty_ledger_returns_clear_message(self):
        from agent_core.mistake_ledger import recent_factual_corrections

        out = recent_factual_corrections()
        self.assertIn("還沒有", out)
        self.assertIn("還沒被", out)

    def test_record_ignores_blank_correction(self):
        from agent_core.mistake_ledger import record_factual_correction

        record_factual_correction("", "anything", "x")
        self.assertFalse(os.path.exists(self._path))

    def test_record_truncates_giant_payloads(self):
        """Forwarded emails / log dumps shouldn't bloat mistakes.json."""
        from agent_core.mistake_ledger import record_factual_correction

        record_factual_correction(
            user_correction="x" * 5000,
            prior_model_reply="y" * 5000,
            matched_pattern="z" * 200,
        )

        with open(self._path, encoding="utf-8") as f:
            data = json.load(f)
        entry = data["log"][0]
        # Hard caps from record_factual_correction's truncation rules.
        self.assertLessEqual(len(entry["user_said"]), 300)
        self.assertLessEqual(len(entry["detail"]), 600)
        self.assertLessEqual(len(entry["resolution"]), 60)


if __name__ == "__main__":
    unittest.main()
