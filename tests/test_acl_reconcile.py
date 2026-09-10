"""acl_reconcile — 存量 chunk ACL 重標的單元測試（不碰 live chroma/sqlite）。

策略：patch 模組自己的 sqlite/segment/id-iter helper + 傳假 Chroma client；
ACL 語義走真的 rag_gateway（用 RED_RAG_ACCESS_CONFIG 指向暫存規則檔）。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from agent_core.ingest import acl_reconcile as ar


class _FakeCollection:
    """get(ids) 回預設 metadata；update(ids, metadatas) 記錄呼叫。"""

    def __init__(self, md_by_id: dict[str, dict]):
        self._md = md_by_id
        self.updates: list[tuple[list[str], list[dict]]] = []

    def get(self, ids=None, include=None):
        ids = ids or []
        return {"ids": list(ids), "metadatas": [self._md.get(i, {}) for i in ids]}

    def update(self, ids=None, metadatas=None):
        self.updates.append((list(ids or []), list(metadatas or [])))


class _FakeClient:
    def __init__(self, col: _FakeCollection):
        self._col = col

    def get_collection(self, name):
        return self._col


class AclReconcileTests(unittest.TestCase):
    def setUp(self):
        # 規則檔：drive 業務→orange（授非 red）；gmail gm→red（red-only）
        self._cfg = {
            "drive_sources": [
                {"drive_id": "DRV_SALES", "owner_color": "orange",
                 "department": "業務", "allowed_colors": ["orange"]},
            ],
            "gmail_mailboxes": [
                {"mailbox_email": "gm@company.example", "owner_color": "red",
                 "department": "jaifung", "allowed_colors": ["red"]},
            ],
        }
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(self._cfg, self._tmp)
        self._tmp.close()
        self._env = mock.patch.dict(
            os.environ, {"RED_RAG_ACCESS_CONFIG": self._tmp.name}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(lambda: os.unlink(self._tmp.name))
        # rag_gateway 對 config 有 mtime 快取；清掉避免測試間污染
        from agent_core import rag_gateway
        rag_gateway._ACCESS_CONFIG_CACHE.clear()
        self.addCleanup(rag_gateway._ACCESS_CONFIG_CACHE.clear)

    def _run(self, col, *, ids_for, **kwargs):
        """patch 掉 sqlite/segment/id-iter，跑 reconcile_acl。ids_for: (key,value)->list[id]"""
        def fake_iter(conn, segment_id, key, value):
            yield from ids_for.get((key, value), [])

        with mock.patch.object(ar.os.path, "exists", return_value=True), \
             mock.patch.object(ar.sqlite3, "connect", return_value=mock.MagicMock()), \
             mock.patch.object(ar, "_metadata_segment", return_value="seg"), \
             mock.patch.object(ar, "_iter_embedding_ids", side_effect=fake_iter):
            return ar.reconcile_acl(client=_FakeClient(col), log=lambda m: None, **kwargs)

    def test_red_only_rule_makes_no_update(self):
        col = _FakeCollection({})
        # 只有 gmail gm（red-only）；drive 規則的 id 給空
        summary = self._run(col, ids_for={("mailbox_email", "gm@company.example"): ["g1"]})
        self.assertEqual(col.updates, [])
        self.assertEqual(summary["updated"], 0)

    def test_mismatched_chunk_updated_with_expected_flags(self):
        # 業務 drive 的 chunk 目前 access_orange=False → 應被更新
        col = _FakeCollection({
            "d1": {"doc_id": "x", "drive_id": "DRV_SALES", "access_orange": False,
                   "access_red": True},
        })
        summary = self._run(col, ids_for={("drive_id", "DRV_SALES"): ["d1"]})
        self.assertEqual(len(col.updates), 1)
        upd_ids, upd_mds = col.updates[0]
        self.assertEqual(upd_ids, ["d1"])
        self.assertTrue(upd_mds[0]["access_orange"])   # 補上 orange
        self.assertTrue(upd_mds[0]["access_red"])       # red 恆真
        self.assertEqual(summary["updated"], 1)

    def test_already_matching_chunk_not_updated(self):
        from agent_core.rag_gateway import metadata_access_fields
        expected = metadata_access_fields("drive", drive_id="DRV_SALES")
        col = _FakeCollection({"d1": {"doc_id": "x", **expected}})
        summary = self._run(col, ids_for={("drive_id", "DRV_SALES"): ["d1"]})
        self.assertEqual(col.updates, [])           # 冪等：已一致不重寫
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(summary["scanned"], 1)

    def test_dry_run_scans_but_does_not_write(self):
        col = _FakeCollection({"d1": {"doc_id": "x", "drive_id": "DRV_SALES"}})
        summary = self._run(
            col, ids_for={("drive_id", "DRV_SALES"): ["d1"]}, dry_run=True)
        self.assertEqual(col.updates, [])
        self.assertEqual(summary["updated"], 1)      # 算「會更新」但沒寫

    def test_batch_failure_is_non_fatal(self):
        # _apply_batch 拋錯（模擬逾時／server wedge）→ 記 failed_batches、不中斷
        col = _FakeCollection({"d1": {"doc_id": "x", "drive_id": "DRV_SALES"}})
        with mock.patch.object(ar, "_apply_batch",
                               side_effect=RuntimeError("timeout")):
            summary = self._run(col, ids_for={("drive_id", "DRV_SALES"): ["d1"]})
        self.assertEqual(summary["failed_batches"], 1)
        self.assertEqual(summary["updated"], 0)  # 失敗批不計更新，但整體不崩

    def test_time_budget_stops_at_batch_boundary(self):
        # 一條規則給 >1 個 batch 的 id；budget≈0 → 第一批後停手
        ids = [f"d{i}" for i in range(ar._BATCH + 50)]
        col = _FakeCollection({i: {"doc_id": "x", "drive_id": "DRV_SALES"} for i in ids})
        summary = self._run(
            col, ids_for={("drive_id", "DRV_SALES"): ids}, time_budget_s=1e-9)
        self.assertTrue(summary["stopped_early"])
        self.assertEqual(summary["scanned"], ar._BATCH)   # 只掃了第一批


if __name__ == "__main__":
    unittest.main()
