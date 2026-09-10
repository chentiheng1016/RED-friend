"""skill_cards：分級、抽取、落庫治理（uncertain 進 pending）、搜尋格式、
人工核可流程、ledger 跨行程鎖的單元測試。ChromaDB 與 Gemini 全 mock。"""
import fcntl
import json
import os
import tempfile
import unittest
from unittest import mock

from agent_core import skill_cards as sc


def _resp(text: str):
    r = mock.MagicMock()
    r.text = text
    return r


def _card(claim="收櫃資料在 FTE_570 畫面建立", tier="likely", conf=0.7,
          video_id="vidA", start_s=65, **kw):
    base = {
        "card_id": sc._card_id(claim),
        "claim": claim,
        "detail": "功能路徑：倉庫作業 > 收櫃",
        "kind": "step",
        "confidence": conf,
        "tier": tier,
        "department": "倉庫",
        "sources": [{"video_id": video_id, "video_name": f"{video_id}.mp4",
                     "start_s": start_s, "end_s": start_s + 30, "quote": "引句"}],
        "corroborations": 1,
        "status": "pending" if tier == "uncertain" else "active",
        "created_at": "2026-07-05T00:00:00+00:00",
        "updated_at": "2026-07-05T00:00:00+00:00",
    }
    base.update(kw)
    return base


class _LedgerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(sc, "_CARDS_PATH",
                              os.path.join(self._tmp.name, "cards.json")),
            mock.patch.object(sc, "_PENDING_PATH",
                              os.path.join(self._tmp.name, "pending.json")),
            mock.patch.object(sc, "metadata_access_fields",
                              return_value={"rag_source": "sop",
                                            "owner_color": "red",
                                            "access_red": True}),
        ]
        for p in self._patches:
            p.start()
        self.store = mock.MagicMock()
        # 語意近似合併預設查不到近鄰（各測試自行覆寫 query 模擬命中）
        self.store.query.return_value = []
        self._store_patch = mock.patch(
            "agent_core.ingest.vector_store.get_store", return_value=self.store)
        self._store_patch.start()

    def tearDown(self):
        self._store_patch.stop()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class TierTests(unittest.TestCase):
    def test_confirmed_needs_both_evidence_and_high_conf(self):
        self.assertEqual(sc.assign_tier(0.9, "both"), "confirmed")
        self.assertEqual(sc.assign_tier(0.9, "narration"), "likely")
        self.assertEqual(sc.assign_tier(0.7, "both"), "likely")
        self.assertEqual(sc.assign_tier(0.3, "both"), "uncertain")
        self.assertEqual(sc.assign_tier("garbage", "both"), "uncertain")

    def test_claim_key_normalization(self):
        a = sc.claim_key("收櫃資料在 FTE_570 畫面建立。")
        b = sc.claim_key("收櫃資料在  fte_570 畫面建立")
        self.assertEqual(a, b)
        self.assertEqual(sc._card_id("收櫃資料在 FTE_570 畫面建立。"),
                         sc._card_id("收櫃資料在 fte_570 畫面建立"))


class ExtractTests(_LedgerBase):
    def test_extract_parses_and_assigns_tiers(self):
        payload = json.dumps([
            {"claim": "收櫃在 FTE_570 建立", "detail": "路徑A", "kind": "step",
             "confidence": 0.9, "start_s": 65, "end_s": 90,
             "evidence": "both", "quote": "q1"},
            {"claim": "數量欄預設 0", "detail": "", "kind": "param",
             "confidence": 0.7, "start_s": None, "end_s": None,
             "evidence": "narration", "quote": "q2"},
            {"claim": "不確定的主張", "kind": "怪類別",
             "confidence": 0.2, "evidence": "screen", "quote": ""},
            {"claim": ""},
        ], ensure_ascii=False)
        with mock.patch("agent_core.gemini_client._gemini_generate",
                        return_value=_resp(payload)) as gen:
            r = sc.extract_skill_cards("vidA", "收櫃.mp4", "分析文字",
                                       transcript="[01:05] 開收櫃",
                                       department="倉庫")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["cards"]), 3)
        self.assertEqual(r["tiers"], {"confirmed": 1, "likely": 1, "uncertain": 1})
        c0 = r["cards"][0]
        self.assertEqual(c0["tier"], "confirmed")
        self.assertEqual(c0["sources"][0]["start_s"], 65)
        self.assertEqual(c0["sources"][0]["video_id"], "vidA")
        self.assertEqual(r["cards"][1]["sources"][0]["start_s"], None)
        self.assertEqual(r["cards"][2]["kind"], "other")
        self.assertEqual(r["cards"][2]["status"], "pending")
        self.assertEqual(gen.call_args.kwargs["caller"], "skill_cards.extract")
        # 逐字稿要進抽取素材
        self.assertIn("[01:05] 開收櫃", gen.call_args.kwargs["contents"][0])

    def test_extract_empty_and_unparseable(self):
        self.assertFalse(sc.extract_skill_cards("", "n", "text")["ok"])
        with mock.patch("agent_core.gemini_client._gemini_generate",
                        return_value=_resp("不是 JSON")):
            r = sc.extract_skill_cards("vidA", "n", "text")
        self.assertFalse(r["ok"])


class IngestTests(_LedgerBase):
    def test_active_goes_to_ledger_and_store_uncertain_to_pending(self):
        r = sc.ingest_skill_cards([
            _card(claim="主張一", tier="confirmed", conf=0.9),
            _card(claim="主張二", tier="likely", conf=0.7),
            _card(claim="主張三", tier="uncertain", conf=0.3),
        ])
        self.assertEqual(r["confirmed"], 1)
        self.assertEqual(r["likely"], 1)
        self.assertEqual(r["uncertain"], 1)
        self.assertEqual(r["total_active"], 2)
        self.assertEqual(r["total_pending"], 1)
        # 只有 active 卡進搜尋索引
        self.assertEqual(self.store.upsert_batch.call_count, 2)
        ids, docs, metas = self.store.upsert_batch.call_args[0]
        self.assertTrue(ids[0].startswith("sc_"))
        self.assertEqual(metas[0]["mime_type"], "text/x-skill-card")
        self.assertIn("tier", metas[0])
        self.assertEqual(metas[0]["start_s"], 65)
        self.assertTrue(metas[0]["access_red"])

    def test_same_claim_from_second_video_merges_sources(self):
        sc.ingest_skill_cards([_card(video_id="vidA", tier="likely")])
        r = sc.ingest_skill_cards([_card(video_id="vidB", tier="likely",
                                         start_s=120)])
        self.assertEqual(r["merged"], 1)
        ledger = sc._load(sc._CARDS_PATH)
        card = next(iter(ledger.values()))
        self.assertEqual(len(card["sources"]), 2)
        self.assertEqual(card["corroborations"], 2)

    def test_skips_garbage(self):
        r = sc.ingest_skill_cards([{"claim": ""}, "not a dict", None])
        self.assertEqual(r["total_active"], 0)
        self.store.upsert_batch.assert_not_called()


class SearchTests(_LedgerBase):
    def test_empty_collection(self):
        self.store.count.return_value = 0
        with mock.patch.object(sc, "access_where", return_value=None), \
                mock.patch.object(sc, "current_request_caller", return_value="red"), \
                mock.patch.object(sc, "log_rag_access_event"), \
                mock.patch.object(sc, "current_request_trace_id", return_value=""):
            self.assertIn("還是空的", sc.search_skill_cards("收櫃"))

    def test_hit_formatting_with_tier_and_provenance(self):
        self.store.count.return_value = 5
        self.store.query.return_value = [{
            "text": "收櫃資料在 FTE_570 畫面建立\n功能路徑：倉庫作業 > 收櫃",
            "metadata": {"tier": "confirmed", "confidence": 0.92,
                         "video_id": "vidA", "start_s": 65,
                         "corroborations": 2},
            "distance": 0.15,
        }]
        with mock.patch.object(sc, "access_where", return_value=None), \
                mock.patch.object(sc, "current_request_caller", return_value="red"), \
                mock.patch.object(sc, "log_rag_access_event"), \
                mock.patch.object(sc, "current_request_trace_id", return_value=""):
            out = sc.search_skill_cards("收櫃怎麼建", k=3)
        self.assertIn("✅", out)
        self.assertIn("tier=confirmed", out)
        self.assertIn("conf=0.92", out)
        self.assertIn("佐證影片×2", out)
        self.assertIn("影片id=vidA @01:05", out)
        self.assertIn("FTE_570", out)

    def test_empty_query(self):
        self.assertIn("不能為空", sc.search_skill_cards(" "))


class PendingFlowTests(_LedgerBase):
    def test_list_pending_and_approve(self):
        sc.ingest_skill_cards([_card(claim="待核主張", tier="uncertain", conf=0.4)])
        out = sc.list_pending_skill_cards()
        self.assertIn("待核主張", out)
        self.assertIn("sc_", out)
        self.assertIn("resolve_skill_card", out)

        cid = next(iter(sc._load(sc._PENDING_PATH)))
        msg = sc.resolve_skill_card(cid, "approve")
        self.assertIn("已核可", msg)
        ledger = sc._load(sc._CARDS_PATH)
        self.assertEqual(ledger[cid]["tier"], "likely")   # 人工核過 = likely
        self.assertEqual(ledger[cid]["status"], "active")
        self.assertEqual(sc._load(sc._PENDING_PATH), {})
        self.store.upsert_batch.assert_called_once()      # 核可才進索引

    def test_reject_removes_without_store_write(self):
        sc.ingest_skill_cards([_card(claim="錯誤主張", tier="uncertain", conf=0.3)])
        cid = next(iter(sc._load(sc._PENDING_PATH)))
        msg = sc.resolve_skill_card(cid, "reject")
        self.assertIn("已退回", msg)
        self.assertEqual(sc._load(sc._PENDING_PATH), {})
        self.assertEqual(sc._load(sc._CARDS_PATH), {})
        self.store.upsert_batch.assert_not_called()

    def test_resolve_guards(self):
        self.assertIn("只接受", sc.resolve_skill_card("sc_x", "yolo"))
        self.assertIn("沒有", sc.resolve_skill_card("sc_missing", "approve"))

    def test_empty_pending_message(self):
        self.assertIn("沒有待核可", sc.list_pending_skill_cards())


class LedgerLockTests(_LedgerBase):
    """ledger R-M-W 跨行程鎖：watcher／CLI 重學／Telegram 核卡並發互蓋防護。
    flock 以 open file description 為單位 — 同行程另開 fd 搶鎖也會衝突，
    可以在單元測試裡驗證互斥而不用真的 fork。"""

    def _lock_probe(self) -> bool:
        """回 True = 鎖正被別的 fd 持有。"""
        fd = open(sc._CARDS_PATH + ".lock", "w")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            return False
        except OSError:
            return True
        finally:
            fd.close()

    def test_lock_exclusive_while_held_and_released_after(self):
        with sc._ledger_lock():
            self.assertTrue(self._lock_probe())
        self.assertFalse(self._lock_probe())

    def _spying_load(self, held: list):
        real_load = sc._load

        def spy(path):
            held.append(self._lock_probe())
            return real_load(path)

        return spy

    def test_ingest_runs_under_lock(self):
        held: list[bool] = []
        with mock.patch.object(sc, "_load",
                               side_effect=self._spying_load(held)):
            sc.ingest_skill_cards([_card(tier="likely")])
        self.assertTrue(held)
        self.assertTrue(all(held))  # load→mutate→save 全程持鎖

    def test_resolve_runs_under_lock(self):
        sc.ingest_skill_cards([_card(claim="待核主張", tier="uncertain",
                                     conf=0.4)])
        cid = next(iter(sc._load(sc._PENDING_PATH)))
        held: list[bool] = []
        with mock.patch.object(sc, "_load",
                               side_effect=self._spying_load(held)):
            sc.resolve_skill_card(cid, "approve")
        self.assertTrue(held)
        self.assertTrue(all(held))

    def test_purge_runs_under_lock(self):
        sc.ingest_skill_cards([_card(tier="likely")])
        held: list[bool] = []
        with mock.patch.object(sc, "_load",
                               side_effect=self._spying_load(held)):
            sc.purge_video_cards("vidA")
        self.assertTrue(held)
        self.assertTrue(all(held))


class OperationSopMetadataTests(unittest.TestCase):
    """operation_sops 的品質欄位升級（向後相容）。"""

    def _fake_access(self):
        return {"rag_source": "sop", "owner_color": "red", "access_red": True}

    def _ingest(self, **kw):
        from agent_core import operation_sops as ops
        store = mock.MagicMock()
        text = "[00:05] 開啟收櫃畫面，執行入庫。" + "步驟。" * 30
        with mock.patch("agent_core.ingest.vector_store.get_store",
                        return_value=store), \
                mock.patch.object(ops, "metadata_access_fields",
                                  return_value=self._fake_access()):
            r = ops.ingest_operation_sop("vid1", "收櫃.mp4", text, **kw)
        self.assertTrue(r["ok"])
        return store.upsert_batch.call_args[0][2]

    def test_quality_fields_written_when_given(self):
        metas = self._ingest(confidence=0.9, tier="confirmed",
                             asr_engine="mlx-whisper")
        self.assertEqual(metas[0]["confidence"], 0.9)
        self.assertEqual(metas[0]["tier"], "confirmed")
        self.assertEqual(metas[0]["asr_engine"], "mlx-whisper")
        self.assertEqual(metas[0]["start_s"], 5)  # [00:05] 抽出

    def test_backward_compatible_when_omitted(self):
        metas = self._ingest()
        self.assertNotIn("confidence", metas[0])
        self.assertNotIn("tier", metas[0])
        self.assertNotIn("asr_engine", metas[0])
        self.assertEqual(metas[0]["start_s"], 5)

    def test_timestamp_parsing(self):
        from agent_core import operation_sops as ops
        self.assertEqual(ops._first_timestamp_s("[00:05] x"), 5)
        self.assertEqual(ops._first_timestamp_s("前言 [12:34] x"), 754)
        self.assertEqual(ops._first_timestamp_s("[1:02:03] x"), 3723)
        # deep 模式 SOP 整合輸出用全形【時間】格式 — 也要收
        self.assertEqual(ops._first_timestamp_s("**【00:58】 1. 進入作業畫面**"), 58)
        self.assertEqual(ops._first_timestamp_s("【1:02:03】x"), 3723)
        self.assertIsNone(ops._first_timestamp_s("沒有時間戳"))
        # 時間區間（取起始時間）與全形冒號 — LLM 輸出格式不受控的常見變體
        self.assertEqual(ops._first_timestamp_s("【00:58–01:30】步驟"), 58)
        self.assertEqual(ops._first_timestamp_s("【00：58】步驟"), 58)
        self.assertEqual(ops._first_timestamp_s("[00:58-01:30] 步驟"), 58)
        self.assertEqual(ops._first_timestamp_s("【00:58 ~ 01:30】步驟"), 58)
        self.assertEqual(ops._first_timestamp_s("【00:58至01:30】步驟"), 58)
        self.assertEqual(ops._first_timestamp_s("【1:02:03–1:05:00】x"), 3723)


class CollectionRegistrationTests(unittest.TestCase):
    def test_skill_cards_in_vector_store_whitelist(self):
        # 迴歸：skill_cards 一度沒註冊進 _VALID_COLLECTIONS——單元測試全 mock 了
        # get_store 所以沒抓到，一上真 chroma 就 ValueError。這裡不 mock 白名單：
        # VectorStore.__init__ 只驗名不連線，可以安全實例化。
        from agent_core.ingest import vector_store as vs
        self.assertIn("skill_cards", vs._VALID_COLLECTIONS)
        vs.VectorStore("skill_cards")  # 不得 raise Unknown collection


class ToolRegistrationTests(unittest.TestCase):
    def test_registered_in_catalog(self):
        from agent_core import tool_registry_catalog as cat
        names = {getattr(t, "__name__", "") for t in cat.BASE_BUILTIN_TOOLS}
        for tool in ("search_skill_cards", "list_pending_skill_cards",
                     "resolve_skill_card"):
            self.assertIn(tool, names)

    def test_resolve_is_confirm_tier(self):
        from agent_core.tool_tiers import TIER_CONFIRM, TIER_SAFE, get_tier
        self.assertEqual(get_tier("resolve_skill_card"), TIER_CONFIRM)
        self.assertEqual(get_tier("search_skill_cards"), TIER_SAFE)
        self.assertEqual(get_tier("list_pending_skill_cards"), TIER_SAFE)


if __name__ == "__main__":
    unittest.main()
