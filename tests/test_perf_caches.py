"""效能優化回歸測試：

* employee_registry：file backend mtime 快取命中 + 改檔失效。
* 部門 agent _entities：優先用 email_timeline 預解析的 _entities 欄、回 copy。
* rag_gateway：_load_access_config 回唯讀快取（不再 per-檔 deepcopy）。
* vision_ops：import 時不拉 pandas / cv2（延後到首次呼叫）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import MappingProxyType
from unittest import mock

from agent_core.path_safety import _REPO_ROOT


class EmployeeRegistryCacheTests(unittest.TestCase):
    def setUp(self):
        from agent_core.web_server import employee_registry as er
        self.er = er
        er._reg_cache.update(stamp=None, registry=None, by_line=None, by_telegram=None)

    def tearDown(self):
        self.er._reg_cache.update(stamp=None, registry=None, by_line=None, by_telegram=None)

    def test_cache_hits_then_invalidates_on_file_change(self):
        er = self.er
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"RED_EMPLOYEE_REGISTRY_FILE": os.path.join(d, "reg.json")}):
            os.environ.pop("RED_EMPLOYEE_REGISTRY_BACKEND", None)
            path = os.path.join(d, "reg.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"a@x.com": {"name": "A", "color": "green"}}, f)
            # wraps= 真實函數：patch 模組內真實名稱，_file_registry_snapshot 內呼叫得到。
            with mock.patch.object(er, "_load_file_registry",
                                   wraps=er._load_file_registry) as spy:
                er.load_registry()
                er.load_registry()
                er.load_registry()
                self.assertEqual(spy.call_count, 1)  # 三次只讀檔一次
                # 改檔（size 變 → stamp 變 → 失效）
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"a@x.com": {"name": "A", "color": "green"},
                               "b@x.com": {"name": "B", "color": "blue"}}, f)
                er.load_registry()
                self.assertEqual(spy.call_count, 2)

    def test_reverse_indexes_resolve_after_rebind(self):
        er = self.er
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"RED_EMPLOYEE_REGISTRY_FILE": os.path.join(d, "reg.json")}):
            os.environ.pop("RED_EMPLOYEE_REGISTRY_BACKEND", None)
            er.register_employee("a@x.com", "A", "green",
                                 line_user_id="U_a", telegram_user_id="111")
            self.assertEqual(er.get_employee_by_telegram_user_id("111")["email"], "a@x.com")
            self.assertEqual(er.get_employee_by_line_user_id("U_a")["email"], "a@x.com")
            # 索引不串味：telegram 索引查 line id 應 miss
            self.assertIsNone(er.get_employee_by_telegram_user_id("U_a"))
            er.register_employee("b@x.com", "B", "blue", telegram_user_id="222")
            self.assertEqual(er.get_employee_by_telegram_user_id("222")["email"], "b@x.com")

    def test_load_registry_returns_copy_not_cached_object(self):
        er = self.er
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"RED_EMPLOYEE_REGISTRY_FILE": os.path.join(d, "reg.json")}):
            os.environ.pop("RED_EMPLOYEE_REGISTRY_BACKEND", None)
            with open(os.path.join(d, "reg.json"), "w", encoding="utf-8") as f:
                json.dump({"a@x.com": {"name": "A", "color": "green"}}, f)
            out = er.load_registry()
            out["a@x.com"]["name"] = "MUTATED"
            out["zzz@x.com"] = {"name": "Z", "color": "red"}
            fresh = er.load_registry()  # 同 mtime → 命中快取，但不可被上面污染
            self.assertEqual(fresh["a@x.com"]["name"], "A")
            self.assertNotIn("zzz@x.com", fresh)


class DeptEntitiesCacheTests(unittest.TestCase):
    def _modules(self):
        from agent_core.agents.blue_shipping import shipping
        from agent_core.agents.purple_accounting import accounting
        from agent_core.agents.indigo_warehouse import warehouse
        return (shipping, accounting, warehouse)

    def test_prefers_precomputed_entities_column(self):
        for mod in self._modules():
            # 帶 _entities 快取欄 + 故意給壞 entities_json：應走快取欄、不踩壞 JSON。
            row = {"_entities": {"po_numbers": ["PO1"]}, "entities_json": "NOT_JSON"}
            self.assertEqual(mod._entities(row), {"po_numbers": ["PO1"]}, mod.__name__)

    def test_falls_back_to_json_when_no_column(self):
        for mod in self._modules():
            row = {"entities_json": json.dumps({"po_numbers": ["PO2"]})}
            self.assertEqual(mod._entities(row), {"po_numbers": ["PO2"]}, mod.__name__)

    def test_returns_copy_not_shared_with_cache_column(self):
        for mod in self._modules():
            cached = {"po_numbers": ["PO1"]}
            row = {"_entities": cached}
            out = mod._entities(row)
            out["x"] = 1
            self.assertNotIn("x", cached, mod.__name__)  # 不污染共享 df 的欄


class RagGatewayConfigCacheTests(unittest.TestCase):
    def setUp(self):
        from agent_core import rag_gateway as rg
        self.rg = rg
        rg._ACCESS_CONFIG_CACHE.clear()

    def tearDown(self):
        self.rg._ACCESS_CONFIG_CACHE.clear()

    def test_returns_readonly_cached_object(self):
        rg = self.rg
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"RED_RAG_ACCESS_CONFIG": os.path.join(d, "rag_access.json")}):
            with open(os.path.join(d, "rag_access.json"), "w", encoding="utf-8") as f:
                json.dump({"drive_sources": [{"drive_id": "D1", "owner_color": "green"}]}, f)
            c1 = rg._load_access_config()
            c2 = rg._load_access_config()
            self.assertIs(c1, c2)  # 命中快取、同一物件（不再 per-檔 deepcopy）
            self.assertIsInstance(c1, MappingProxyType)
            with self.assertRaises(TypeError):
                c1["x"] = 1  # 唯讀，防污染快取
            # 功能仍正確：drive 規則查得到 owner
            fields = rg.metadata_access_fields("drive", drive_id="D1")
            self.assertEqual(fields["owner_color"], "green")


class VisionOpsLazyImportTests(unittest.TestCase):
    def test_import_does_not_pull_pandas_or_cv2(self):
        code = (
            "import sys; import agent_core.vision_ops as vo; "
            "assert 'pandas' not in sys.modules, 'pandas leaked at import'; "
            "assert 'cv2' not in sys.modules, 'cv2 leaked at import'; "
            "assert vo.cv2 is None and not vo._deps_loaded, 'deps loaded eagerly'; "
            "print('ok')"
        )
        env = {**os.environ, "PYTHONPATH": _REPO_ROOT, "AGENT_DAEMON_MODE": "1"}
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, env=env, cwd=_REPO_ROOT)
        self.assertIn("ok", r.stdout, msg=r.stderr)


class RagToolLazyImportTests(unittest.TestCase):
    def test_catalog_import_does_not_load_chromadb(self):
        # drive_search + chat_search 都不在 module top import vector_store，故
        # catalog eager import 它們不會把 chromadb 拉進每個 daemon。
        code = (
            "import sys; import agent_core.tool_registry_catalog; "
            "chroma=[m for m in sys.modules if m=='chromadb' or m.startswith('chromadb.')]; "
            "assert not chroma, f'chromadb loaded: {len(chroma)} modules'; "
            "assert 'agent_core.ingest.vector_store' not in sys.modules, 'vector_store loaded'; "
            "print('ok')"
        )
        env = {**os.environ, "PYTHONPATH": _REPO_ROOT, "AGENT_DAEMON_MODE": "1"}
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, env=env, cwd=_REPO_ROOT)
        self.assertIn("ok", r.stdout, msg=r.stderr)


class DeptDatePrefilterTests(unittest.TestCase):
    """日期窗口 vectorized 預篩必須與舊的逐列 str(date or "")<cutoff 完全等價：
    窗口內保留、窗口外篩掉、NaN 視為 "nan"（>= cutoff）保留、空字串篩掉。"""

    def test_window_nan_and_empty_boundaries(self):
        import pandas as pd
        from datetime import datetime, timedelta
        from agent_core.agents.blue_shipping import shipping

        recent = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
        df = pd.DataFrame([
            {"date": recent, "id": "R"},
            {"date": old, "id": "O"},
            {"date": float("nan"), "id": "N"},
            {"date": "", "id": "E"},
        ])
        with mock.patch.object(shipping, "_load_df", return_value=df), \
                mock.patch.object(shipping, "_shipping_row", return_value=True), \
                mock.patch.object(shipping, "_matches", return_value=True), \
                mock.patch.object(
                    shipping, "_row_to_shipment",
                    side_effect=lambda s: {
                        "date": str(shipping._get(s, "date", "") or ""),
                        "shipment_id": shipping._get(s, "id", ""),
                    }):
            rows = shipping._search_shipments(days_back=180, limit=50)
        ids = {r["shipment_id"] for r in rows}
        self.assertIn("R", ids)       # 窗口內 → 保留
        self.assertNotIn("O", ids)    # 窗口外 → 篩掉
        self.assertIn("N", ids)       # NaN → "nan" >= cutoff → 保留（等價舊邏輯）
        self.assertNotIn("E", ids)    # 空字串 → "" < cutoff → 篩掉

    def test_no_cutoff_keeps_all(self):
        import pandas as pd
        from agent_core.agents.blue_shipping import shipping

        df = pd.DataFrame([{"date": "2000-01-01", "id": "A"},
                           {"date": "2026-01-01", "id": "B"}])
        with mock.patch.object(shipping, "_load_df", return_value=df), \
                mock.patch.object(shipping, "_shipping_row", return_value=True), \
                mock.patch.object(shipping, "_matches", return_value=True), \
                mock.patch.object(
                    shipping, "_row_to_shipment",
                    side_effect=lambda s: {"date": str(shipping._get(s, "date", "")),
                                           "shipment_id": shipping._get(s, "id", "")}):
            rows = shipping._search_shipments(days_back=None, limit=50)
        self.assertEqual({r["shipment_id"] for r in rows}, {"A", "B"})


if __name__ == "__main__":
    unittest.main()
