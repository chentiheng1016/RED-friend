"""skill_cards Phase 6：跨影片佐證自動升級與數值衝突降級的單元測試。"""
import os
import tempfile
import unittest
from unittest import mock

from agent_core import skill_cards as sc
from tests.test_skill_cards import _card


class _Base(unittest.TestCase):
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
        self.store.query.return_value = []
        self._store_patch = mock.patch(
            "agent_core.ingest.vector_store.get_store", return_value=self.store)
        self._store_patch.start()

    def tearDown(self):
        self._store_patch.stop()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class ConflictHeuristicTests(unittest.TestCase):
    def test_disjoint_numbers_conflict(self):
        self.assertTrue(sc._details_conflict("數量=120", "數量=150"))

    def test_subset_is_not_conflict(self):
        # 一邊只是多講幾個數字（超集）→ 不算矛盾
        self.assertFalse(sc._details_conflict("數量=120", "數量=120、上限 999"))
        self.assertFalse(sc._details_conflict("數量=120、上限 999", "數量=120"))

    def test_no_numbers_no_conflict(self):
        self.assertFalse(sc._details_conflict("走倉庫作業選單", "數量=150"))
        self.assertFalse(sc._details_conflict("", ""))


class CorroborationPromotionTests(_Base):
    def test_second_video_promotes_likely_to_confirmed(self):
        sc.ingest_skill_cards([_card(video_id="vidA", tier="likely", conf=0.7)])
        r = sc.ingest_skill_cards([_card(video_id="vidB", tier="likely",
                                         conf=0.7, start_s=200)])
        self.assertEqual(r["auto_confirmed"], 1)
        card = next(iter(sc._load(sc._CARDS_PATH).values()))
        self.assertEqual(card["tier"], "confirmed")
        self.assertEqual(card["promoted_by"], "corroboration")
        self.assertEqual(card["corroborations"], 2)
        # 升級後的 tier 要寫回搜尋索引
        metas = self.store.upsert_batch.call_args[0][2]
        self.assertEqual(metas[0]["tier"], "confirmed")

    def test_same_video_reingest_does_not_promote(self):
        sc.ingest_skill_cards([_card(video_id="vidA", tier="likely")])
        r = sc.ingest_skill_cards([_card(video_id="vidA", tier="likely",
                                         start_s=999)])
        self.assertEqual(r["auto_confirmed"], 0)
        card = next(iter(sc._load(sc._CARDS_PATH).values()))
        self.assertEqual(card["tier"], "likely")
        self.assertEqual(card["corroborations"], 1)  # 同影片不算第二佐證

    def test_two_uncertain_from_different_videos_promote_to_likely(self):
        sc.ingest_skill_cards([_card(claim="待證主張", tier="uncertain",
                                     conf=0.4, video_id="vidA")])
        self.store.upsert_batch.assert_not_called()   # uncertain 不進索引
        r = sc.ingest_skill_cards([_card(claim="待證主張", tier="uncertain",
                                         conf=0.5, video_id="vidB", start_s=88)])
        self.assertEqual(r["auto_promoted"], 1)
        self.assertEqual(sc._load(sc._PENDING_PATH), {})  # 離開待核佇列
        card = next(iter(sc._load(sc._CARDS_PATH).values()))
        self.assertEqual(card["tier"], "likely")
        self.assertEqual(card["status"], "active")
        self.store.upsert_batch.assert_called_once()      # 升級後進索引

    def test_two_uncertain_same_video_stay_pending(self):
        sc.ingest_skill_cards([_card(claim="待證主張", tier="uncertain",
                                     conf=0.4, video_id="vidA")])
        r = sc.ingest_skill_cards([_card(claim="待證主張", tier="uncertain",
                                         conf=0.4, video_id="vidA",
                                         start_s=300)])
        self.assertEqual(r["auto_promoted"], 0)
        self.assertEqual(len(sc._load(sc._PENDING_PATH)), 1)
        self.assertEqual(sc._load(sc._CARDS_PATH), {})


class ConflictFlowTests(_Base):
    def test_numeric_conflict_demotes_to_pending_and_delists(self):
        sc.ingest_skill_cards([_card(video_id="vidA", tier="likely",
                                     detail="數量欄填 120")])
        r = sc.ingest_skill_cards([_card(video_id="vidB", tier="likely",
                                         detail="數量欄填 150", start_s=77)])
        self.assertEqual(r["conflict"], 1)
        self.assertEqual(sc._load(sc._CARDS_PATH), {})      # 下架出 ledger
        pending = sc._load(sc._PENDING_PATH)
        card = next(iter(pending.values()))
        self.assertEqual(card["status"], "conflict")
        self.assertEqual(card["tier"], "uncertain")
        self.assertEqual(card["conflicts"][0]["video_id"], "vidB")
        self.assertIn("150", card["conflicts"][0]["detail"])
        self.store.delete_by_doc_id.assert_called_once_with(card["card_id"])

    def test_conflict_surfaces_in_pending_list(self):
        sc.ingest_skill_cards([_card(video_id="vidA", tier="likely",
                                     detail="數量欄填 120")])
        sc.ingest_skill_cards([_card(video_id="vidB", tier="likely",
                                     detail="數量欄填 150")])
        out = sc.list_pending_skill_cards()
        self.assertIn("數值衝突", out)
        self.assertIn("vidB", out)
        self.assertIn("150", out)

    def test_conflict_resolution_via_approve(self):
        sc.ingest_skill_cards([_card(video_id="vidA", tier="likely",
                                     detail="數量欄填 120")])
        sc.ingest_skill_cards([_card(video_id="vidB", tier="likely",
                                     detail="數量欄填 150")])
        cid = next(iter(sc._load(sc._PENDING_PATH)))
        msg = sc.resolve_skill_card(cid, "approve")
        self.assertIn("已核可", msg)
        card = sc._load(sc._CARDS_PATH)[cid]
        self.assertEqual(card["status"], "active")
        self.assertEqual(card["tier"], "likely")

    def test_superset_numbers_merge_without_conflict(self):
        sc.ingest_skill_cards([_card(video_id="vidA", tier="likely",
                                     detail="數量欄填 120")])
        r = sc.ingest_skill_cards([_card(video_id="vidB", tier="likely",
                                         detail="數量欄填 120，上限 999",
                                         start_s=50)])
        self.assertEqual(r["conflict"], 0)
        self.assertEqual(r["auto_confirmed"], 1)  # 不衝突 → 正常佐證升級
        card = next(iter(sc._load(sc._CARDS_PATH).values()))
        self.assertEqual(card["tier"], "confirmed")

    def test_delete_failure_does_not_crash_ingest(self):
        self.store.delete_by_doc_id.side_effect = RuntimeError("chroma down")
        sc.ingest_skill_cards([_card(video_id="vidA", tier="likely",
                                     detail="數量欄填 120")])
        r = sc.ingest_skill_cards([_card(video_id="vidB", tier="likely",
                                         detail="數量欄填 150")])
        self.assertTrue(r["ok"])
        self.assertEqual(r["conflict"], 1)


class ConflictQuarantineRegressionTests(_Base):
    """Review 修正迴歸：conflict 隔離不可被任何 re-ingest 路徑繞過。"""

    def _make_conflict(self):
        sc.ingest_skill_cards([_card(video_id="vidA", tier="likely",
                                     detail="數量欄填 120")])
        sc.ingest_skill_cards([_card(video_id="vidB", tier="likely",
                                     detail="數量欄填 150")])
        self.store.reset_mock()

    def test_likely_reingest_after_conflict_stays_quarantined(self):
        # 管線重跑是常態：conflict 後同主張的 likely 卡再進來，
        # 必須併進 pending 隔離，不得重回 ledger／搜尋索引。
        self._make_conflict()
        r = sc.ingest_skill_cards([_card(video_id="vidB", tier="likely",
                                         detail="數量欄填 150")])
        self.assertTrue(r["ok"])
        self.assertEqual(sc._load(sc._CARDS_PATH), {})       # 不回 ledger
        card = next(iter(sc._load(sc._PENDING_PATH).values()))
        self.assertEqual(card["status"], "conflict")          # 沒有雙棲殭屍
        self.store.upsert_batch.assert_not_called()           # 不回索引

    def test_uncertain_resight_does_not_release_conflict_card(self):
        # conflict 卡 corroborations 已=2（兩支矛盾影片都算來源）——
        # 一張 uncertain 再目擊不得觸發佐證升級放行。
        self._make_conflict()
        r = sc.ingest_skill_cards([_card(video_id="vidA", tier="uncertain",
                                         conf=0.4, detail="數量欄填 120")])
        self.assertEqual(r["auto_promoted"], 0)
        self.assertEqual(sc._load(sc._CARDS_PATH), {})
        card = next(iter(sc._load(sc._PENDING_PATH).values()))
        self.assertEqual(card["status"], "conflict")
        self.store.upsert_batch.assert_not_called()

    def test_same_batch_conflict_not_reupserted(self):
        # 同一批餵入兩支矛盾影片：先 append 進 to_store 的 stale 參照
        # 不得在 delete 之後又被 upsert 回索引。
        r = sc.ingest_skill_cards([
            _card(video_id="vidA", tier="likely", detail="數量欄填 120"),
            _card(video_id="vidB", tier="likely", detail="數量欄填 150",
                  start_s=99),
        ])
        self.assertEqual(r["conflict"], 1)
        self.assertEqual(sc._load(sc._CARDS_PATH), {})
        self.store.upsert_batch.assert_not_called()  # 衝突卡沒有任何 upsert
        # delete 照樣要下（同主張可能在更早的批次進過索引；刪不存在的 doc 是 no-op）
        self.store.delete_by_doc_id.assert_called_once()

    def test_likely_absorbs_pending_uncertain_and_corroborates(self):
        # uncertain 先進 pending，第二支影片以 likely 抽出同主張：
        # 要吸收 pending 份（來源合併、佐證=2 → 直升 confirmed），佇列清空。
        sc.ingest_skill_cards([_card(claim="待證主張", tier="uncertain",
                                     conf=0.4, video_id="vidA")])
        r = sc.ingest_skill_cards([_card(claim="待證主張", tier="likely",
                                         conf=0.7, video_id="vidB",
                                         start_s=88)])
        self.assertEqual(sc._load(sc._PENDING_PATH), {})      # 不留殭屍
        card = next(iter(sc._load(sc._CARDS_PATH).values()))
        self.assertEqual(len(card["sources"]), 2)             # 來源有併
        self.assertEqual(card["corroborations"], 2)
        self.assertEqual(card["tier"], "confirmed")           # 2 支影片 → 升級
        self.assertEqual(r["auto_confirmed"], 1)

    def test_likely_conflicting_with_pending_uncertain_quarantines(self):
        # pending 的 uncertain「數量=120」＋新影片 likely「數量=150」＝
        # 數值矛盾 → 進 conflict 隔離，不得以 likely 入庫。
        sc.ingest_skill_cards([_card(claim="待證主張", tier="uncertain",
                                     conf=0.4, video_id="vidA",
                                     detail="數量欄填 120")])
        r = sc.ingest_skill_cards([_card(claim="待證主張", tier="likely",
                                         conf=0.7, video_id="vidB",
                                         detail="數量欄填 150")])
        self.assertEqual(r["conflict"], 1)
        card = next(iter(sc._load(sc._PENDING_PATH).values()))
        self.assertEqual(card["status"], "conflict")
        self.assertEqual(sc._load(sc._CARDS_PATH), {})
        self.store.upsert_batch.assert_not_called()


class SemanticMergeTests(_Base):
    """語意近似合併：措辭不同的同一主張要併卡，跨影片佐證才會真的觸發。"""

    def _hit(self, cid, distance):
        return [{"text": "x", "metadata": {"doc_id": cid}, "distance": distance}]

    def test_paraphrase_from_second_video_merges_and_corroborates(self):
        sc.ingest_skill_cards([_card(claim="收櫃資料在 FTE_570 畫面建立",
                                     video_id="vidA", tier="likely")])
        cid_a = next(iter(sc._load(sc._CARDS_PATH)))
        self.store.query.return_value = self._hit(cid_a, 0.05)  # sim 0.95
        r = sc.ingest_skill_cards([_card(claim="FTE_570 是建立收櫃資料的畫面",
                                         video_id="vidB", tier="likely",
                                         start_s=200)])
        self.assertEqual(r["semantic_merged"], 1)
        ledger = sc._load(sc._CARDS_PATH)
        self.assertEqual(len(ledger), 1)                     # 沒有分裂成兩張卡
        card = ledger[cid_a]
        self.assertEqual(card["corroborations"], 2)          # 佐證真的動了
        self.assertEqual(card["tier"], "confirmed")
        self.assertEqual(r["auto_confirmed"], 1)

    def test_below_threshold_stays_separate(self):
        sc.ingest_skill_cards([_card(claim="收櫃資料在 FTE_570 畫面建立",
                                     video_id="vidA", tier="likely")])
        cid_a = next(iter(sc._load(sc._CARDS_PATH)))
        self.store.query.return_value = self._hit(cid_a, 0.2)  # sim 0.80 < 0.93
        r = sc.ingest_skill_cards([_card(claim="出櫃申請單在 P4TF_560 建立",
                                         video_id="vidB", tier="likely")])
        self.assertEqual(r["semantic_merged"], 0)
        self.assertEqual(len(sc._load(sc._CARDS_PATH)), 2)

    def test_env_zero_disables(self):
        sc.ingest_skill_cards([_card(claim="主張甲", video_id="vidA",
                                     tier="likely")])
        self.store.query.reset_mock()
        with mock.patch.object(sc, "_SEM_SIM", 0.0):
            sc.ingest_skill_cards([_card(claim="主張乙", video_id="vidB",
                                         tier="likely")])
        self.store.query.assert_not_called()

    def test_semantic_match_with_numeric_conflict_quarantines(self):
        sc.ingest_skill_cards([_card(claim="數量欄預設值說明", video_id="vidA",
                                     tier="likely", detail="數量欄填 120")])
        cid_a = next(iter(sc._load(sc._CARDS_PATH)))
        self.store.query.return_value = self._hit(cid_a, 0.04)
        r = sc.ingest_skill_cards([_card(claim="數量欄位的預設值",
                                         video_id="vidB", tier="likely",
                                         detail="數量欄填 150")])
        self.assertEqual(r["conflict"], 1)                   # 語意合併後衝突照抓
        self.assertEqual(sc._load(sc._CARDS_PATH), {})
        card = next(iter(sc._load(sc._PENDING_PATH).values()))
        self.assertEqual(card["status"], "conflict")

    def test_query_error_treated_as_new_card(self):
        sc.ingest_skill_cards([_card(claim="主張甲", video_id="vidA",
                                     tier="likely")])
        self.store.query.side_effect = RuntimeError("chroma down")
        r = sc.ingest_skill_cards([_card(claim="主張乙", video_id="vidB",
                                         tier="likely")])
        self.assertTrue(r["ok"])
        self.assertEqual(len(sc._load(sc._CARDS_PATH)), 2)


class MergeGateTests(_Base):
    """語意合併硬閘：同句型、不同代碼/數字的近鄰相似度再高也不併 —
    錯併會假造跨影片佐證、自動升 confirmed。"""

    def _hit(self, cid, distance):
        return [{"text": "x", "metadata": {"doc_id": cid}, "distance": distance}]

    def test_mergeable_claims_heuristic(self):
        # 同代碼換句話說 → 可併
        self.assertTrue(sc._mergeable_claims(
            "收櫃資料在 FTE_570 畫面建立", "FTE_570 是建立收櫃資料的畫面"))
        # 無代碼無數字的純換句話說 → 可併
        self.assertTrue(sc._mergeable_claims(
            "走倉庫作業選單", "從倉庫作業選單進入"))
        # 一邊省略代碼（子集關係）→ 可併
        self.assertTrue(sc._mergeable_claims(
            "收櫃資料在 FTE_570 畫面建立", "收櫃資料建立有專屬畫面"))
        # 代碼互不為子集 → 不併
        self.assertFalse(sc._mergeable_claims(
            "收櫃資料在 FTE_570 畫面建立", "收櫃資料在 P4TF_560 畫面建立"))
        # 數字互不為子集 → 不併
        self.assertFalse(sc._mergeable_claims("數量欄預設 120", "數量欄預設 150"))

    def test_same_pattern_different_code_not_merged(self):
        # 反例（現有 corroboration 測試只測真換句話說）：句型幾乎相同、
        # 只差畫面代碼 — 向量相似度極高，但必須各自成卡。
        sc.ingest_skill_cards([_card(claim="收櫃資料在 FTE_570 畫面建立",
                                     video_id="vidA", tier="likely")])
        cid_a = next(iter(sc._load(sc._CARDS_PATH)))
        self.store.query.return_value = self._hit(cid_a, 0.03)  # sim 0.97 極高
        r = sc.ingest_skill_cards([_card(claim="收櫃資料在 P4TF_560 畫面建立",
                                         video_id="vidB", tier="likely")])
        self.assertEqual(r["semantic_merged"], 0)
        ledger = sc._load(sc._CARDS_PATH)
        self.assertEqual(len(ledger), 2)                  # 各自成卡，不併
        for card in ledger.values():
            self.assertEqual(card["tier"], "likely")      # 沒有假佐證升級
            self.assertEqual(card["corroborations"], 1)

    def test_same_pattern_different_number_not_merged(self):
        sc.ingest_skill_cards([_card(claim="數量欄預設 120", video_id="vidA",
                                     tier="likely")])
        cid_a = next(iter(sc._load(sc._CARDS_PATH)))
        self.store.query.return_value = self._hit(cid_a, 0.03)
        r = sc.ingest_skill_cards([_card(claim="數量欄預設 150",
                                         video_id="vidB", tier="likely")])
        self.assertEqual(r["semantic_merged"], 0)
        self.assertEqual(len(sc._load(sc._CARDS_PATH)), 2)


class ResolveRegressionTests(_Base):
    """Review 修正迴歸：衝突裁決三檔與索引寫入失敗可重試。"""

    def _make_conflict(self):
        sc.ingest_skill_cards([_card(video_id="vidA", tier="likely",
                                     detail="數量欄填 120")])
        sc.ingest_skill_cards([_card(video_id="vidB", tier="likely",
                                     detail="數量欄填 150")])
        self.store.reset_mock()
        return next(iter(sc._load(sc._PENDING_PATH)))

    def test_approve_new_adopts_new_detail_and_clears_conflicts(self):
        cid = self._make_conflict()
        msg = sc.resolve_skill_card(cid, "approve_new")
        self.assertIn("採納新影片數值", msg)
        card = sc._load(sc._CARDS_PATH)[cid]
        self.assertIn("150", card["detail"])                  # 新值蓋回
        self.assertNotIn("conflicts", card)                   # 裁決後清紀錄
        self.assertEqual(card["tier"], "likely")

    def test_approve_keeps_old_detail_and_clears_conflicts(self):
        cid = self._make_conflict()
        sc.resolve_skill_card(cid, "approve")
        card = sc._load(sc._CARDS_PATH)[cid]
        self.assertIn("120", card["detail"])                  # 舊值那邊
        self.assertNotIn("conflicts", card)

    def test_approve_new_without_conflicts_is_rejected(self):
        sc.ingest_skill_cards([_card(claim="普通待核", tier="uncertain",
                                     conf=0.4)])
        cid = next(iter(sc._load(sc._PENDING_PATH)))
        self.assertIn("無新值可採納", sc.resolve_skill_card(cid, "approve_new"))
        self.assertIn(cid, sc._load(sc._PENDING_PATH))        # 沒被吃掉

    def test_approve_store_failure_keeps_card_pending(self):
        # chroma 離線時核可：所有檔案不動、卡留待核可重試 —
        # 不得產生「ledger 有、索引永遠沒有」的查不到卡。
        cid = self._make_conflict()
        self.store.upsert_batch.side_effect = RuntimeError("chroma down")
        msg = sc.resolve_skill_card(cid, "approve")
        self.assertIn("失敗", msg)
        self.assertIn(cid, sc._load(sc._PENDING_PATH))
        self.assertEqual(sc._load(sc._CARDS_PATH), {})


if __name__ == "__main__":
    unittest.main()
