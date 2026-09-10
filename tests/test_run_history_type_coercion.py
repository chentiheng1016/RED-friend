from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import run_history  # noqa: E402


def _write_index(path: str) -> None:
    entries = [
        {
            "id": "run-success",
            "tool": "system_status",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "status": "success",
            "elapsed_sec": 0.1,
            "short_result": "ok",
        },
        {
            "id": "run-error",
            "tool": "browser_screenshot",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "status": "error",
            "elapsed_sec": 0.2,
            "short_result": "boom",
        },
    ]
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class RunHistoryTypeCoercionTests(unittest.TestCase):
    def test_list_runs_accepts_llm_shaped_numeric_args(self):
        with tempfile.TemporaryDirectory() as d:
            index = os.path.join(d, "index.jsonl")
            _write_index(index)
            with mock.patch.object(run_history, "RUNS_INDEX", index):
                cases = [
                    {"limit": "20"},
                    {"limit": {"n": "1"}},
                    {"since_hours": "24"},
                    {"since_hours": None},
                    {"tool_name": ["system_status"]},
                    {"status": {"status": "error"}},
                ]
                for kwargs in cases:
                    with self.subTest(kwargs=kwargs):
                        out = run_history.list_runs(**kwargs)
                        self.assertIsInstance(out, str)
                        self.assertGreater(len(out), 20)

    def test_find_past_actions_accepts_llm_shaped_args(self):
        with tempfile.TemporaryDirectory() as d:
            index = os.path.join(d, "index.jsonl")
            _write_index(index)
            with mock.patch.object(run_history, "RUNS_INDEX", index):
                cases = [
                    {"query": {"query": "system_status"}, "days": "7"},
                    {"query": ["browser"], "limit": {"count": "5"}},
                    {"query": "system_status", "days": None, "limit": "20"},
                ]
                for kwargs in cases:
                    with self.subTest(kwargs=kwargs):
                        out = run_history.find_past_actions(**kwargs)
                        self.assertIsInstance(out, str)
                        self.assertGreater(len(out), 20)


if __name__ == "__main__":
    unittest.main()
