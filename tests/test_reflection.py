"""反思層（Phase 2）：intake 追蹤 + 每日歸納 + 檢索/撤銷工具。

不碰真的 ChromaDB / Gemini：intake 用 tmpdir、store 用假物件、
_gemini_generate 用 mock。unittest discover（非 pytest），隔離放 setUp。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from agent_core import reflection as refl_mod
from agent_core.ingest import reflection_intake as ri_mod


def _iso(hours_ago: float = 0.0) -> str:
    t = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return t.isoformat(timespec="seconds")


class _IntakeTmpMixin(unittest.TestCase):
    """把 intake / watermark 檔導到 tmpdir——測試不得碰主 checkout 的
    var/ 活資料；RED_REFLECTION_* 環境變數也要清（live 機器 export 調參
    不准讓測試翻紅）。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.intake_path = os.path.join(self._tmp.name, "reflection_intake.jsonl")
        self.watermark_path = os.path.join(self._tmp.name, "reflection_watermark.json")
        for target, attr, value in (
            (ri_mod, "INTAKE_FILE", self.intake_path),
            (ri_mod, "DATA_DIR", self._tmp.name),
            (refl_mod, "_WATERMARK_FILE", self.watermark_path),
        ):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        env_p = mock.patch.dict(os.environ)
        env_p.start()
        self.addCleanup(env_p.stop)
        for key in list(os.environ):
            if key.startswith("RED_REFLECTION"):
                del os.environ[key]

    def _lines(self) -> list[dict]:
        if not os.path.exists(self.intake_path):
            return []
        with open(self.intake_path, encoding="utf-8") as fh:
            return [json.loads(x) for x in fh if x.strip()]


class RecordBatchTests(_IntakeTmpMixin):
    def test_dedupes_chunks_to_docs_and_keeps_first_three_chunk_ids(self):
        metas = [{"doc_id": "docA", "title": "報價單", "drive_id": "dr1"}] * 5
        ri_mod.record_batch("drive_docs", [f"docA__c{i}" for i in range(5)], metas)
        (line,) = self._lines()
        self.assertEqual(line["c"], "drive_docs")
        (doc,) = line["docs"]
        self.assertEqual(doc["d"], "docA")
        self.assertEqual(doc["g"], "dr1")
        self.assertEqual(doc["ids"], ["docA__c0", "docA__c1", "docA__c2"])

    def test_group_keys_per_collection(self):
        ri_mod.record_batch(
            "google_chat_messages", ["s__c0"],
            [{"doc_id": "spaces/x", "space_name": "spaces/x", "display_name": "業務群"}],
        )
        ri_mod.record_batch(
            "gmail_threads", ["t__c0"],
            [{"doc_id": "thr1", "mailbox_email": "sales@x.com", "title": "PO"}],
        )
        chat_line, gmail_line = self._lines()
        self.assertEqual(chat_line["docs"][0]["g"], "spaces/x")
        self.assertEqual(gmail_line["docs"][0]["g"], "sales@x.com")

    def test_untracked_collection_skipped(self):
        ri_mod.record_batch("xiaohong_reflections", ["r__c0"], [{"doc_id": "refl_x"}])
        ri_mod.record_batch("xiaohong_memory", ["m__c0"], [{"doc_id": "m"}])
        self.assertEqual(self._lines(), [], "反思產物與記憶不追蹤——避免反思讀到自己的輸出")

    def test_kill_switch_env(self):
        with mock.patch.dict(os.environ, {"RED_REFLECTION_INTAKE": "0"}):
            ri_mod.record_batch("drive_docs", ["a__c0"], [{"doc_id": "a"}])
        self.assertEqual(self._lines(), [])

    def test_never_raises_on_garbage(self):
        # 熱路徑鐵則：metadatas 比 ids 短、None、缺 doc_id——全都只准吞
        ri_mod.record_batch("drive_docs", ["a__c0", "a__c1"], [None])
        ri_mod.record_batch("drive_docs", ["b__c0"], [{}])
        ri_mod.record_batch("drive_docs", [], [])

    def test_docs_per_line_cap_records_dropped(self):
        ids = [f"d{i}__c0" for i in range(ri_mod._MAX_DOCS_PER_LINE + 5)]
        metas = [{"doc_id": f"d{i}"} for i in range(ri_mod._MAX_DOCS_PER_LINE + 5)]
        ri_mod.record_batch("drive_docs", ids, metas)
        (line,) = self._lines()
        self.assertEqual(len(line["docs"]), ri_mod._MAX_DOCS_PER_LINE)
        self.assertEqual(line["dropped"], 5)


class ReadRecentAndPruneTests(_IntakeTmpMixin):
    def _write_line(self, hours_ago: float, doc: str = "d1"):
        with open(self.intake_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": _iso(hours_ago), "c": "drive_docs",
                "docs": [{"d": doc, "g": "", "t": "", "ids": [f"{doc}__c0"]}],
            }) + "\n")

    def test_window_filter(self):
        self._write_line(hours_ago=72, doc="old")
        self._write_line(hours_ago=1, doc="new")
        got = ri_mod.read_recent(window_days=2)
        self.assertEqual([e["docs"][0]["d"] for e in got], ["new"])

    def test_bad_lines_skipped(self):
        with open(self.intake_path, "a", encoding="utf-8") as fh:
            fh.write("not-json\n")
            fh.write(json.dumps({"ts": "bad-ts", "c": "drive_docs", "docs": []}) + "\n")
        self._write_line(hours_ago=1)
        self.assertEqual(len(ri_mod.read_recent(window_days=2)), 1)

    def test_prune_rewrites_old_and_bad_lines(self):
        self._write_line(hours_ago=24 * 30, doc="ancient")
        self._write_line(hours_ago=1, doc="fresh")
        with open(self.intake_path, "a", encoding="utf-8") as fh:
            fh.write("garbage\n")
        removed = ri_mod.prune(keep_days=7)
        self.assertEqual(removed, 2)
        (line,) = self._lines()
        self.assertEqual(line["docs"][0]["d"], "fresh")

    def test_missing_file_ok(self):
        self.assertEqual(ri_mod.read_recent(), [])
        self.assertEqual(ri_mod.prune(), 0)

    def test_torn_utf8_tail_tolerated_and_pruned(self):
        # 撕裂寫入/斷電可能留半個 CJK 多位元組字——UnicodeDecodeError 是
        # ValueError 子類、except OSError 接不到，沒 errors="replace" 的話
        # 反思 daemon 會進 KeepAlive 永久 crash-loop（審查實測復現）。
        self._write_line(hours_ago=1, doc="good")
        with open(self.intake_path, "ab") as fh:
            fh.write("《標題".encode("utf-8")[:4])  # 切在多位元組字中間、無換行
        got = ri_mod.read_recent(window_days=2)
        self.assertEqual([e["docs"][0]["d"] for e in got], ["good"])
        removed = ri_mod.prune(keep_days=7)  # prune 自癒：壞行清掉
        self.assertGreaterEqual(removed, 1)
        (line,) = self._lines()
        self.assertEqual(line["docs"][0]["d"], "good")

    def test_naive_timestamp_line_skipped_not_crash(self):
        with open(self.intake_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": "2026-07-01T10:00:00",  # naive——跟 aware cutoff 比較會 TypeError
                "c": "drive_docs", "docs": [{"d": "naive", "g": "", "t": "", "ids": []}],
            }) + "\n")
        self._write_line(hours_ago=1, doc="good")
        got = ri_mod.read_recent(window_days=2)
        self.assertEqual([e["docs"][0]["d"] for e in got], ["good"])


class _FakeStore:
    """假 VectorStore：get_by_ids / upsert_batch / query / count / delete_by_doc_id。"""

    def __init__(self, texts_by_id: dict[str, str] | None = None):
        self.texts_by_id = texts_by_id or {}
        self.upserts: list[tuple[list, list, list]] = []
        self.deleted: list[str] = []
        self.query_hits: list[dict] = []

    def get_by_ids(self, chunk_ids):
        ids = [i for i in chunk_ids if i in self.texts_by_id]
        return {"ids": ids, "documents": [self.texts_by_id[i] for i in ids],
                "metadatas": [{} for _ in ids]}

    def upsert_batch(self, ids, docs, metas):
        self.upserts.append((list(ids), list(docs), list(metas)))

    def query(self, text, n_results=5, where=None):
        return self.query_hits[:n_results]

    def count(self):
        return len(self.upserts) or len(self.query_hits)

    def delete_by_doc_id(self, doc_id):
        self.deleted.append(doc_id)

    def delete_stale_chunks(self, doc_id, chunk_count):
        self.stale_cleanups = getattr(self, "stale_cleanups", [])
        self.stale_cleanups.append((doc_id, chunk_count))


class RunDailyReflectionTests(_IntakeTmpMixin):
    def setUp(self):
        super().setUp()
        # rag_sync 沒在跑、糾正清單為空（各測試可覆蓋）
        p1 = mock.patch.object(refl_mod, "_rag_sync_running", return_value=False)
        p1.start()
        self.addCleanup(p1.stop)
        p2 = mock.patch(
            "agent_core.mistake_ledger.recent_factual_correction_entries",
            return_value=[],
        )
        p2.start()
        self.addCleanup(p2.stop)

    def _seed_intake(self, n_docs: int = 4, collection: str = "drive_docs", group: str = "dr1"):
        docs = [{"d": f"doc{i}", "g": group, "t": f"文件{i}", "ids": [f"doc{i}__c0"]}
                for i in range(n_docs)]
        with open(self.intake_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _iso(1), "c": collection, "docs": docs}) + "\n")
        return {f"doc{i}__c0": f"客戶A 訂單 {i} 因面料延誤改期" for i in range(n_docs)}

    def test_skips_when_rag_sync_running(self):
        with mock.patch.object(refl_mod, "_rag_sync_running", return_value=True):
            result = refl_mod.run_daily_reflection()
        self.assertEqual(result.get("skipped"), "rag_sync_running")

    def test_no_candidates_below_min_docs(self):
        self._seed_intake(n_docs=1)
        with mock.patch("agent_core.ingest.vector_store.get_store") as gs:
            result = refl_mod.run_daily_reflection()
        self.assertEqual(result["groups_reflected"], 0)
        gs.assert_not_called()

    def test_happy_path_writes_owner_only_reflection_with_provenance(self):
        texts = self._seed_intake(n_docs=4)
        source = _FakeStore(texts)
        sink = _FakeStore()
        stores = {"drive_docs": source, refl_mod._COLLECTION: sink}
        fake_resp = mock.Mock(text=(
            "- 客戶A 兩天內 4 張訂單全部因面料延誤改期，且延誤來源都是同一家供應商，"
            "建議追供應商交期而非逐單催（來源：文件0；文件1；文件2；文件3）"
        ))
        with mock.patch("agent_core.ingest.vector_store.get_store", side_effect=stores.__getitem__), \
             mock.patch("agent_core.gemini_client._gemini_generate",
                        return_value=fake_resp) as gen:
            result = refl_mod.run_daily_reflection()
        self.assertEqual(result["groups_reflected"], 1, result)
        (ids, docs, metas), = sink.upserts
        meta = metas[0]
        self.assertTrue(meta["doc_id"].startswith("refl_"))
        self.assertEqual(meta["source_collection"], "drive_docs")
        self.assertIn("doc0", meta["provenance_doc_ids"])
        # owner_only 等級：只有 red 看得到（新洞察不外洩給部門 bot）
        self.assertTrue(meta["access_red"])
        self.assertFalse(meta.get("access_orange", False))
        # 歸納呼叫帶顯式 caller 標籤（成本歸戶）
        self.assertEqual(gen.call_args.kwargs.get("caller"), "reflection.run_daily_reflection")

    def test_no_insights_response_skips_group(self):
        texts = self._seed_intake(n_docs=3)
        source = _FakeStore(texts)
        sink = _FakeStore()
        stores = {"drive_docs": source, refl_mod._COLLECTION: sink}
        with mock.patch("agent_core.ingest.vector_store.get_store", side_effect=stores.__getitem__), \
             mock.patch("agent_core.gemini_client._gemini_generate",
                        return_value=mock.Mock(text="NO_INSIGHTS")):
            result = refl_mod.run_daily_reflection()
        self.assertEqual(result["groups_reflected"], 0)
        self.assertEqual(sink.upserts, [])

    def test_group_error_collected_not_raised(self):
        texts = self._seed_intake(n_docs=3)
        source = _FakeStore(texts)
        sink = _FakeStore()
        stores = {"drive_docs": source, refl_mod._COLLECTION: sink}
        with mock.patch("agent_core.ingest.vector_store.get_store", side_effect=stores.__getitem__), \
             mock.patch("agent_core.gemini_client._gemini_generate",
                        side_effect=RuntimeError("503")):
            result = refl_mod.run_daily_reflection()
        self.assertEqual(result["groups_reflected"], 0)
        self.assertEqual(len(result["errors"]), 1)

    def test_stale_chunk_cleanup_called_after_upsert(self):
        texts = self._seed_intake(n_docs=4)
        source = _FakeStore(texts)
        sink = _FakeStore()
        stores = {"drive_docs": source, refl_mod._COLLECTION: sink}
        long_insight = "- 洞察" + "內容" * 50
        with mock.patch("agent_core.ingest.vector_store.get_store", side_effect=stores.__getitem__), \
             mock.patch("agent_core.gemini_client._gemini_generate",
                        return_value=mock.Mock(text=long_insight)):
            refl_mod.run_daily_reflection()
        (ids, _docs, _metas), = sink.upserts
        self.assertEqual(sink.stale_cleanups, [(_metas[0]["doc_id"], len(ids))],
                         "同日重跑洞察變短時要清舊尾塊——先寫後清")

    def test_watermark_prevents_double_reflection(self):
        # 固定 48h 窗 × 每日跑 = 同批文件連兩天被歸納成近重複洞察；
        # watermark 推進後第二輪不該再反思同一批。
        texts = self._seed_intake(n_docs=4)
        source = _FakeStore(texts)
        sink = _FakeStore()
        stores = {"drive_docs": source, refl_mod._COLLECTION: sink}
        insight = mock.Mock(text="- 客戶A 連續 4 張單因面料延誤改期，來源集中同一供應商（來源：文件0；文件1）")
        with mock.patch("agent_core.ingest.vector_store.get_store", side_effect=stores.__getitem__), \
             mock.patch("agent_core.gemini_client._gemini_generate", return_value=insight):
            first = refl_mod.run_daily_reflection()
            second = refl_mod.run_daily_reflection()
        self.assertEqual(first["groups_reflected"], 1)
        self.assertEqual(second["groups_reflected"], 0,
                         "watermark 之後沒有新文件——不准重複歸納同一批")

    def test_total_failure_keeps_watermark_for_retry(self):
        texts = self._seed_intake(n_docs=4)
        source = _FakeStore(texts)
        sink = _FakeStore()
        stores = {"drive_docs": source, refl_mod._COLLECTION: sink}
        good = mock.Mock(text="- 客戶A 連續 4 張單因面料延誤改期，供應商相同，建議整批追蹤（來源：文件0）")
        with mock.patch("agent_core.ingest.vector_store.get_store", side_effect=stores.__getitem__):
            with mock.patch("agent_core.gemini_client._gemini_generate",
                            side_effect=RuntimeError("Gemini 全面 503")):
                failed = refl_mod.run_daily_reflection()
            # prune 已把 intake 清掉？不——keep_days 7 天，48h 內的行還在。
            with mock.patch("agent_core.gemini_client._gemini_generate", return_value=good):
                retry = refl_mod.run_daily_reflection()
        self.assertEqual(failed["groups_reflected"], 0)
        self.assertEqual(len(failed["errors"]), 1)
        self.assertEqual(retry["groups_reflected"], 1,
                         "全軍覆沒不推 watermark——重試要能處理同一窗")

    def test_provenance_only_claims_docs_fed_to_llm(self):
        texts = self._seed_intake(n_docs=4)
        source = _FakeStore(texts)
        sink = _FakeStore()
        stores = {"drive_docs": source, refl_mod._COLLECTION: sink}
        insight = mock.Mock(text="- 三份文件都指向同一面料供應商延誤，建議升級追蹤層級（來源：文件0；文件1；文件2）")
        # 把 prompt 預算壓小到只塞得下 3 份文件
        budget = sum(len(f"文件{i}") + len(texts[f"doc{i}__c0"]) + 8 for i in range(3)) + 1
        with mock.patch.object(refl_mod, "_MAX_PROMPT_CHARS", budget), \
             mock.patch("agent_core.ingest.vector_store.get_store", side_effect=stores.__getitem__), \
             mock.patch("agent_core.gemini_client._gemini_generate", return_value=insight):
            result = refl_mod.run_daily_reflection()
        self.assertEqual(result["groups_reflected"], 1, result)
        (_ids, _docs, metas), = sink.upserts
        self.assertEqual(metas[0]["source_doc_count"], 3,
                         "provenance 只能宣稱真的餵給 LLM 的文件")
        self.assertNotIn("doc3", metas[0]["provenance_doc_ids"])

    def test_corrections_injected_into_prompt(self):
        texts = self._seed_intake(n_docs=3)
        source = _FakeStore(texts)
        sink = _FakeStore()
        stores = {"drive_docs": source, refl_mod._COLLECTION: sink}
        with mock.patch(
            "agent_core.mistake_ledger.recent_factual_correction_entries",
            return_value=[{"user_said": "LURCHI 交期不是 30 天"}],
        ), mock.patch("agent_core.ingest.vector_store.get_store", side_effect=stores.__getitem__), \
             mock.patch("agent_core.gemini_client._gemini_generate",
                        return_value=mock.Mock(text="- 洞察內容夠長" * 10)) as gen:
            refl_mod.run_daily_reflection()
        prompt = gen.call_args.kwargs.get("contents", gen.call_args[1].get("contents"))[0]
        self.assertIn("LURCHI 交期不是 30 天", prompt)
        self.assertIn("不准與這些矛盾", prompt)


class SearchAndRevokeTests(unittest.TestCase):
    def test_search_empty_collection_message(self):
        empty = _FakeStore()
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=empty):
            out = refl_mod.search_reflections("交期")
        self.assertIn("反思庫還是空的", out)

    def test_search_formats_hits_with_provenance_warning(self):
        store = _FakeStore()
        store.query_hits = [{
            "text": "客戶A 兩週 3 次改期",
            "metadata": {"title": "[反思] Drive 2026-07-02", "doc_id": "refl_x",
                         "source_doc_count": 4},
            "distance": 0.2,
        }]
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store):
            out = refl_mod.search_reflections("改期")
        self.assertIn("客戶A", out)
        self.assertIn("歸納自 4 份文件", out)
        self.assertIn("機器歸納、可能有誤", out)

    def test_revoke_validates_prefix(self):
        out = refl_mod.revoke_reflection("drive_doc_123")
        self.assertIn("錯誤", out)

    def test_revoke_deletes_by_doc_id(self):
        store = _FakeStore()
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store):
            out = refl_mod.revoke_reflection("refl_2026-07-02_abc123")
        self.assertIn("✅", out)
        self.assertEqual(store.deleted, ["refl_2026-07-02_abc123"])


class VectorStoreIntakeHookTests(_IntakeTmpMixin):
    def test_valid_collections_includes_reflections(self):
        from agent_core.ingest.vector_store import _VALID_COLLECTIONS
        self.assertIn("xiaohong_reflections", _VALID_COLLECTIONS)

    def test_upsert_batch_records_intake(self):
        from agent_core.ingest.vector_store import VectorStore

        class _FakeCol:
            def upsert(self, *, ids, documents, metadatas):
                pass

        vs = VectorStore("drive_docs")
        vs._col = _FakeCol()
        vs.upsert_batch(
            ["docZ__c0"], ["內文"], [{"doc_id": "docZ", "title": "T", "drive_id": "dr9"}],
        )
        (line,) = self._lines()
        self.assertEqual(line["docs"][0]["d"], "docZ")

    def test_upsert_batch_survives_intake_failure(self):
        from agent_core.ingest.vector_store import VectorStore

        class _FakeCol:
            def upsert(self, *, ids, documents, metadatas):
                pass

        vs = VectorStore("drive_docs")
        vs._col = _FakeCol()
        with mock.patch.object(ri_mod, "record_batch", side_effect=RuntimeError("boom")):
            vs.upsert_batch(["a__c0"], ["x"], [{"doc_id": "a"}])  # 不准 raise


class DaemonWiringTests(unittest.TestCase):
    def test_reflection_task_registered(self):
        import agent_daemon
        self.assertTrue(callable(agent_daemon.task_reflection))

    def test_tier_and_sensitive_registration(self):
        from agent_core.tool_tiers import get_tier
        from agent_core.tg_auth import _SENSITIVE_TOOLS
        self.assertEqual(get_tier("search_reflections"), "safe")
        self.assertEqual(get_tier("revoke_reflection"), "confirm")
        self.assertIn("revoke_reflection", _SENSITIVE_TOOLS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
