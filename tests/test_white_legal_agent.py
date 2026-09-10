"""Phase 3a — WhiteLegalAgent + shim 測試.

驗證：
  - 舊 import path（agent_core.specs）仍可用
  - WhiteLegalAgent 接 specs/，read intents 走得通
  - SoT 矩陣特性：所有 color 皆可查 White；White 不能主動查任何 color
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


class TestSpecsShim(unittest.TestCase):
    def test_old_import_path_still_works(self):
        from agent_core import specs as old
        for name in (
            "_SPECS_DIR", "_load_spec_version", "_specs_key", "_specs_dir_for",
            "parse_spec_sheet", "list_specs", "compare_specs",
        ):
            self.assertTrue(hasattr(old, name), f"shim 缺 {name}")

    def test_shim_and_real_share_state(self):
        from agent_core import specs as via_shim
        from agent_core.agents.white_legal import specs as via_real
        self.assertIs(via_shim.list_specs, via_real.list_specs)
        self.assertIs(via_shim._load_spec_version, via_real._load_spec_version)
        # _SPECS_DIR 是常數（同字串值）
        self.assertEqual(via_shim._SPECS_DIR, via_real._SPECS_DIR)


class TestWhiteLegalAgent(unittest.TestCase):
    def setUp(self):
        from agent_core.agents import (
            Agent, AgentRegistry, PermissionMiddleware,
        )
        from agent_core.agents.white_legal import WhiteLegalAgent

        self.Agent = Agent
        self.registry = AgentRegistry()
        self.middleware = PermissionMiddleware(self.registry)
        self.registry.bind_middleware(self.middleware)
        self.white = WhiteLegalAgent()
        self.registry.register(self.white)

        # specs/ 目錄導向 tmp，避免污染真實 specs/
        self._tmpdir = tempfile.mkdtemp(prefix="red_white_test_")
        from agent_core.agents.white_legal import specs as sp
        self._orig_dir = sp._SPECS_DIR
        sp._SPECS_DIR = self._tmpdir

    def tearDown(self):
        from agent_core.agents.white_legal import specs as sp
        sp._SPECS_DIR = self._orig_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _seed_spec(self, customer: str, product_model: str, version: str, payload: dict):
        from agent_core.agents.white_legal import specs as sp
        d = os.path.join(sp._SPECS_DIR, sp._specs_key(customer, product_model))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{version}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def test_query_profile(self):
        from agent_core.agents import AgentRequest
        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.WHITE,
            intent="query.profile", payload={},
        ))
        self.assertEqual(result["profile"]["color"], "white")
        self.assertIn("Legal SoT", result["profile"]["department"])
        self.assertIn("query.search_docs", result["profile"]["query_intents"])

    def test_query_list_specs_empty(self):
        from agent_core.agents import AgentRequest
        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.WHITE,
            intent="query.list_specs", payload={},
        ))
        self.assertIn("text", result)
        # 空 specs/（dir 存在但空）→ "找不到符合..."；
        # specs/ 不存在 → "尚未解析"。兩種都算 empty state。
        self.assertTrue(
            "尚未解析" in result["text"] or "找不到符合" in result["text"],
            f"預期 empty-state 訊息，實際：{result['text']!r}",
        )

    def test_query_list_specs_with_data(self):
        self._seed_spec("Richter", "5001-4292", "v20260420_100000",
                        {"customer": "Richter", "product_model": "5001-4292"})
        from agent_core.agents import AgentRequest
        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.WHITE,
            intent="query.list_specs", payload={},
        ))
        self.assertIn("Richter", result["text"])
        self.assertIn("5001-4292", result["text"])

    def test_query_get_latest_spec(self):
        self._seed_spec("Richter", "5001-4292", "v20260420_100000",
                        {"customer": "Richter", "product_model": "5001-4292",
                         "specs": {"outsole": {"hardness_shore_A": 65}}})
        self._seed_spec("Richter", "5001-4292", "v20260421_100000",
                        {"customer": "Richter", "product_model": "5001-4292",
                         "specs": {"outsole": {"hardness_shore_A": 70}}})
        from agent_core.agents import AgentRequest
        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.ORANGE, target=self.Agent.WHITE,
            intent="query.get_latest_spec",
            payload={"customer": "Richter", "product_model": "5001-4292"},
        ))
        self.assertTrue(result["found"])
        self.assertEqual(result["spec"]["specs"]["outsole"]["hardness_shore_A"], 70)

    def test_query_get_latest_spec_missing_args(self):
        from agent_core.agents import AgentRequest
        result = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.WHITE,
            intent="query.get_latest_spec", payload={"customer": ""},
        ))
        self.assertIn("error", result)

    def test_query_get_spec_version_specific(self):
        self._seed_spec("R", "M1", "v20260420_100000", {"v": 1})
        self._seed_spec("R", "M1", "v20260421_100000", {"v": 2})
        from agent_core.agents import AgentRequest
        # latest
        latest = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.WHITE,
            intent="query.get_spec_version",
            payload={"customer": "R", "product_model": "M1", "version": "latest"},
        ))
        self.assertEqual(latest["spec"]["v"], 2)
        # previous
        prev = self.middleware.dispatch(AgentRequest(
            caller=self.Agent.RED, target=self.Agent.WHITE,
            intent="query.get_spec_version",
            payload={"customer": "R", "product_model": "M1", "version": "previous"},
        ))
        self.assertEqual(prev["spec"]["v"], 1)

    def test_unknown_intent_raises(self):
        from agent_core.agents import AgentRequest
        with self.assertRaises(ValueError):
            self.middleware.dispatch(AgentRequest(
                caller=self.Agent.RED, target=self.Agent.WHITE,
                intent="totally.unknown", payload={},
            ))

    # ---- SoT 矩陣特性 ----

    def test_all_colors_can_query_white(self):
        """White 是 SoT — 矩陣裡每個 color (除 White 自己) 都應能查 White。"""
        from agent_core.agents import AgentRequest
        for color in self.Agent:
            if color is self.Agent.WHITE:
                continue
            # 應該不 raise PermissionDenied
            result = self.middleware.dispatch(AgentRequest(
                caller=color, target=self.Agent.WHITE,
                intent="query.list_specs", payload={},
            ))
            self.assertIn("text", result, f"{color.value} → White 應通過")

    def test_white_cannot_query_any_color(self):
        """White 矩陣為空 — 不能主動查任何人。"""
        from agent_core.agents import AgentRequest, PermissionDenied
        for color in self.Agent:
            if color is self.Agent.WHITE:
                continue
            with self.assertRaises(PermissionDenied,
                                   msg=f"White → {color.value} 應被擋"):
                self.middleware.dispatch(AgentRequest(
                    caller=self.Agent.WHITE, target=color,
                    intent="query.x", payload={},
                ))


class TestWhiteLegalAgentRAG(unittest.TestCase):
    """query.search_docs + command.sync_drive — mock Drive/ChromaDB。"""

    def setUp(self):
        from agent_core.agents import AgentRegistry, PermissionMiddleware
        from agent_core.agents.white_legal import WhiteLegalAgent

        self.Agent = __import__("agent_core.agents.permission_matrix", fromlist=["Agent"]).Agent
        self.registry = AgentRegistry()
        self.middleware = PermissionMiddleware(self.registry)
        self.registry.bind_middleware(self.middleware)
        self.registry.register(WhiteLegalAgent())

    def _dispatch(self, intent, payload, caller=None):
        from agent_core.agents import AgentRequest
        return self.middleware.dispatch(AgentRequest(
            caller=caller or self.Agent.RED, target=self.Agent.WHITE,
            intent=intent, payload=payload,
        ))

    def test_search_docs_empty_query_returns_error(self):
        result = self._dispatch("query.search_docs", {"query": ""})
        self.assertIn("error", result)

    def test_search_docs_calls_vector_store(self):
        from unittest.mock import MagicMock, patch

        fake_store = MagicMock()
        fake_store.query.return_value = [
            {"text": "契約條款", "metadata": {"title": "ABC合約"}, "distance": 0.12}
        ]
        with patch("agent_core.ingest.vector_store.get_store", return_value=fake_store):
            result = self._dispatch("query.search_docs", {"query": "保固", "n_results": 3})

        # 大王沒有 ACL 限制，但語意檢索一律排除小紅自產內容（排程報表）。
        fake_store.query.assert_called_once_with(
            "保固", n_results=3, where={"generated_by_red": {"$ne": True}},
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["hits"][0]["text"], "契約條款")

    def test_search_docs_department_caller_gets_acl_filter(self):
        from unittest.mock import MagicMock, patch

        fake_store = MagicMock()
        fake_store.count.return_value = 1
        fake_store.query.return_value = []
        with patch("agent_core.ingest.vector_store.get_store", return_value=fake_store):
            result = self._dispatch(
                "query.search_docs",
                {"query": "保固", "n_results": 3},
                caller=self.Agent.ORANGE,
            )

        fake_store.query.assert_called_once_with(
            "保固",
            n_results=3,
            where={"$and": [
                {"generated_by_red": {"$ne": True}},
                {"access_orange": {"$eq": True}},
            ]},
        )
        self.assertEqual(result["total"], 0)

    def test_sync_drive_no_args_returns_status(self):
        from unittest.mock import patch

        with patch("agent_core.ingest.drive_sync.sync_status") as mock_status:
            mock_status.return_value = {"collection": "drive_docs", "total_chunks": 42}
            result = self._dispatch("command.sync_drive", {})

        mock_status.assert_called_once()
        self.assertEqual(result["total_chunks"], 42)

    def test_sync_drive_file_id_calls_sync_file(self):
        from unittest.mock import patch

        with patch("agent_core.ingest.drive_sync.sync_file") as mock_sync:
            mock_sync.return_value = {"file_id": "abc123", "chunks": 5}
            result = self._dispatch("command.sync_drive", {"file_id": "abc123"})

        mock_sync.assert_called_once_with("abc123")
        self.assertEqual(result["chunks"], 5)

    def test_sync_drive_folder_id_calls_sync_folder(self):
        from unittest.mock import patch

        with patch("agent_core.ingest.drive_sync.sync_folder") as mock_sync:
            mock_sync.return_value = {"total": 3, "synced": 3, "skipped": 0}
            result = self._dispatch("command.sync_drive", {"folder_id": "folder-xyz"})

        mock_sync.assert_called_once_with("folder-xyz", recursive=False)
        self.assertEqual(result["synced"], 3)


if __name__ == "__main__":
    unittest.main()
