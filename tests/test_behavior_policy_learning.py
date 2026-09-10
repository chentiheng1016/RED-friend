"""behavior_policy 學習機制（learn_behavior 家族）的 scope / 排序 / 取代邏輯。

Phase 1（記憶治理）新增：visibility_scope 跨部門可見度、confidence×新鮮度
排序 + 截斷上限、同 scenario 取代舊規則。這裡不碰真的 ChromaDB / Gemini
embedding —— 用假 collection 只驗證 memory.py 這幾個 helper 的邏輯本身。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

from agent_core import memory as mem_mod


class _FakeBehaviorCollection:
    """模擬 chroma collection：get(where=/include=/limit=/offset=) /
    upsert(documents=/metadatas=/ids=) / update(ids=/metadatas=)。"""

    def __init__(self):
        self.rows: dict[str, tuple[str, dict]] = {}

    def get(self, *, ids=None, include=None, limit=None, offset=None, where=None):
        if ids is not None:
            picked = [(i, *self.rows[i]) for i in ids if i in self.rows]
        else:
            items = list(self.rows.items())
            if where:
                items = [
                    (i, doc, meta) for i, (doc, meta) in items
                    if all(meta.get(k) == v for k, v in where.items())
                ]
            else:
                items = [(i, doc, meta) for i, (doc, meta) in items]
            lo = int(offset or 0)
            hi = lo + int(limit) if limit is not None else len(items)
            picked = items[lo:hi]
        return {
            "ids": [p[0] for p in picked],
            "documents": [p[1] for p in picked],
            "metadatas": [p[2] for p in picked],
        }

    def upsert(self, *, documents, metadatas, ids):
        for _id, doc, meta in zip(ids, documents, metadatas):
            self.rows[_id] = (doc, dict(meta))

    def update(self, *, ids, metadatas):
        for _id, meta in zip(ids, metadatas):
            if _id in self.rows:
                doc, _old = self.rows[_id]
                self.rows[_id] = (doc, dict(meta))


def _iso(delta_days: float = 0.0) -> str:
    return (datetime.now() - timedelta(days=delta_days)).isoformat(timespec="seconds")


class _FakeCollectionMixin(unittest.TestCase):
    """conftest autouse 在 unittest discover 下不生效 —— 隔離放 setUp。"""

    def setUp(self):
        super().setUp()
        self.col = _FakeBehaviorCollection()
        self._patcher = mock.patch.object(
            mem_mod, "_get_memory_collection", return_value=self.col
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        # 鎖 _build_chat_fn=None：若同輪較早的測試 import 過 agent.py，
        # tool_registry hook 已被註冊，learn_behavior 會真的去重建 chat →
        # CI 無 GEMINI_API_KEY 時 _get_gemini_api_key() sys.exit(1) 殺測試
        # （本機有真 key 反而測不出來）。測試不得依賴 hook 註冊狀態。
        from agent_core import tool_registry
        hook_patcher = mock.patch.object(tool_registry, "_build_chat_fn", None)
        hook_patcher.start()
        self.addCleanup(hook_patcher.stop)


class ValidateVisibilityScopeTests(unittest.TestCase):
    def test_default_and_explicit_owner_only(self):
        self.assertEqual(mem_mod._validate_visibility_scope(""), ("owner_only", ""))
        self.assertEqual(mem_mod._validate_visibility_scope("owner_only"), ("owner_only", ""))

    def test_all_scope(self):
        self.assertEqual(mem_mod._validate_visibility_scope("all"), ("all", ""))

    def test_known_department_color(self):
        scope, warning = mem_mod._validate_visibility_scope("department:orange")
        self.assertEqual(scope, "department:orange")
        self.assertEqual(warning, "")

    def test_unknown_department_color_falls_back(self):
        scope, warning = mem_mod._validate_visibility_scope("department:neon")
        self.assertEqual(scope, "owner_only")
        self.assertNotEqual(warning, "")

    def test_garbage_value_falls_back(self):
        scope, warning = mem_mod._validate_visibility_scope("!!!not-a-scope")
        self.assertEqual(scope, "owner_only")
        self.assertNotEqual(warning, "")


class BehaviorVisibleToTests(unittest.TestCase):
    def test_owner_sees_everything(self):
        for scope in ("owner_only", "all", "department:orange"):
            self.assertTrue(mem_mod._behavior_visible_to(scope, "red"))

    def test_owner_only_hidden_from_department(self):
        self.assertFalse(mem_mod._behavior_visible_to("owner_only", "orange"))

    def test_all_visible_to_department(self):
        self.assertTrue(mem_mod._behavior_visible_to("all", "orange"))

    def test_department_scope_matches_only_that_department(self):
        self.assertTrue(mem_mod._behavior_visible_to("department:orange", "orange"))
        self.assertFalse(mem_mod._behavior_visible_to("department:orange", "yellow"))

    def test_unknown_scope_defaults_conservative(self):
        self.assertFalse(mem_mod._behavior_visible_to("???", "orange"))


class LearnBehaviorSupersedeTests(_FakeCollectionMixin):
    def test_new_rule_defaults_owner_only_confidence_one(self):
        msg = mem_mod.learn_behavior("寫信給日本客戶", "一律用敬語")
        self.assertIn("✅", msg)
        self.assertIn("只有大王看得到", msg)
        (doc, meta), = [(d, m) for d, m in self.col.rows.values()]
        self.assertEqual(meta["visibility_scope"], "owner_only")
        self.assertEqual(meta["confidence"], "1.0")
        self.assertEqual(meta["correction_count"], "1")
        self.assertEqual(meta["archived"], "0")

    def test_explicit_department_scope(self):
        msg = mem_mod.learn_behavior(
            "報價單附加運費", "報價要附加運費估算", visibility_scope="department:orange"
        )
        self.assertIn("orange", msg)
        (_doc, meta), = [(d, m) for d, m in self.col.rows.values()]
        self.assertEqual(meta["visibility_scope"], "department:orange")

    def test_invalid_scope_falls_back_with_warning_in_message(self):
        msg = mem_mod.learn_behavior("測試情境", "測試規則", visibility_scope="department:neon")
        self.assertIn("退回 owner_only", msg)
        (_doc, meta), = [(d, m) for d, m in self.col.rows.values()]
        self.assertEqual(meta["visibility_scope"], "owner_only")

    def test_same_scenario_supersedes_old_rule(self):
        mem_mod.learn_behavior("回覆客戶詢價", "先確認庫存再回")
        first_id = next(iter(self.col.rows))
        msg = mem_mod.learn_behavior("回覆客戶詢價", "先確認庫存 + 交期再回")
        self.assertIn("取代了 1 條同情境舊規則", msg)
        # 舊規則被標 archived，內容不變（沒被覆寫成新規則）
        old_doc, old_meta = self.col.rows[first_id]
        self.assertEqual(old_meta["archived"], "1")
        self.assertEqual(old_meta["archived_reason"], "superseded")
        # 新規則 correction_count 累計
        new_entries = [
            m for _id, (d, m) in self.col.rows.items() if _id != first_id
        ]
        self.assertEqual(len(new_entries), 1)
        self.assertEqual(new_entries[0]["correction_count"], "2")

    def test_different_scenario_does_not_supersede(self):
        mem_mod.learn_behavior("寫信給日本客戶", "一律用敬語")
        mem_mod.learn_behavior("寫信給美國客戶", "語氣可以輕鬆一點")
        archived = [m for _doc, m in self.col.rows.values() if m.get("archived") == "1"]
        self.assertEqual(archived, [])
        self.assertEqual(len(self.col.rows), 2)


class ListBehaviorsTests(_FakeCollectionMixin):
    def test_excludes_archived_and_reports_count(self):
        mem_mod.learn_behavior("A情境", "舊規則")
        mem_mod.learn_behavior("A情境", "新規則")  # supersedes 舊規則
        out = mem_mod.list_behaviors()
        self.assertIn("1 條生效中", out)
        self.assertIn("另有 1 條已停用", out)
        self.assertIn("新規則", out)
        self.assertNotIn("舊規則", out)


class CompileBehaviorPoliciesTests(_FakeCollectionMixin):
    def _seed(self, doc_id, scenario, rule, *, scope="owner_only",
              confidence="1.0", days_old=0.0, archived="0"):
        self.col.rows[doc_id] = (
            f"【行為準則】處理「{scenario}」相關任務時必須：{rule}",
            {
                "source": "behavior_policy",
                "scenario": scenario,
                "rule": rule,
                "visibility_scope": scope,
                "confidence": confidence,
                "archived": archived,
                "ts": _iso(days_old),
            },
        )

    def test_owner_scope_sees_all_non_archived(self):
        self._seed("id1", "情境A", "規則A", scope="owner_only")
        self._seed("id2", "情境B", "規則B", scope="department:orange")
        self._seed("id3", "情境C", "規則C", scope="all")
        self._seed("id4", "情境D", "規則D", archived="1")
        text = mem_mod._compile_behavior_policies(caller_scope="red")
        self.assertIn("規則A", text)
        self.assertIn("規則B", text)
        self.assertIn("規則C", text)
        self.assertNotIn("規則D", text)

    def test_department_scope_only_sees_all_and_own_department(self):
        self._seed("id1", "情境A", "規則A", scope="owner_only")
        self._seed("id2", "情境B", "規則B", scope="department:orange")
        self._seed("id3", "情境C", "規則C", scope="department:yellow")
        self._seed("id4", "情境D", "規則D", scope="all")
        text = mem_mod._compile_behavior_policies(caller_scope="orange")
        self.assertNotIn("規則A", text, "owner_only 不該外洩給部門 bot")
        self.assertIn("規則B", text)
        self.assertNotIn("規則C", text, "別部門的規則不該可見")
        self.assertIn("規則D", text)

    def test_no_visible_rules_returns_empty_string(self):
        self._seed("id1", "情境A", "規則A", scope="owner_only")
        text = mem_mod._compile_behavior_policies(caller_scope="orange")
        self.assertEqual(text, "")

    def test_ranking_prefers_high_confidence_and_recent(self):
        self._seed("old_low", "舊且低信心", "應排後面", confidence="0.3", days_old=200)
        self._seed("new_high", "新且高信心", "應排前面", confidence="1.0", days_old=0)
        text = mem_mod._compile_behavior_policies(caller_scope="red")
        self.assertLess(text.index("應排前面"), text.index("應排後面"))

    def test_max_injected_cap_reports_dropped_count(self):
        for i in range(5):
            self._seed(f"id{i}", f"情境{i}", f"規則{i}", days_old=float(i))
        with mock.patch.dict(
            "os.environ", {"RED_BEHAVIOR_POLICY_MAX_INJECTED": "2"}
        ):
            text = mem_mod._compile_behavior_policies(caller_scope="red")
        shown = sum(1 for i in range(5) if f"規則{i}" in text)
        self.assertEqual(shown, 2)
        self.assertIn("還有 3 條較舊/較低信心的規則未列出", text)


class _FakeStateStore:
    """模擬 daemon_helpers.load_state/update_state：純記憶體、無鎖（測試不需要）。"""

    def __init__(self):
        self._state: dict = {}

    def load_state(self) -> dict:
        return dict(self._state)

    def update_state(self, mutate) -> None:
        mutate(self._state)


class RunBehaviorPolicyDecayTests(_FakeCollectionMixin):
    def setUp(self):
        super().setUp()
        self.store = _FakeStateStore()

    def _seed(self, doc_id, *, confidence="1.0", days_old=0.0, archived="0"):
        self.col.rows[doc_id] = (
            f"【行為準則】{doc_id}",
            {
                "source": "behavior_policy",
                "scenario": doc_id,
                "rule": "規則內容",
                "visibility_scope": "owner_only",
                "confidence": confidence,
                "archived": archived,
                "ts": _iso(days_old),
            },
        )

    def test_archives_low_effective_confidence_rules(self):
        self._seed("fresh_high", confidence="1.0", days_old=0)
        self._seed("stale_low", confidence="0.2", days_old=400)  # 遠超半衰期，衰減後接近 0
        note = mem_mod.run_behavior_policy_decay(self.store.load_state, self.store.update_state)
        self.assertIn("停用 1 條", note)
        self.assertEqual(self.col.rows["fresh_high"][1]["archived"], "0")
        self.assertEqual(self.col.rows["stale_low"][1]["archived"], "1")
        self.assertEqual(self.col.rows["stale_low"][1]["archived_reason"], "decay")

    def test_second_run_same_day_is_noop(self):
        self._seed("stale_low", confidence="0.2", days_old=400)
        first = mem_mod.run_behavior_policy_decay(self.store.load_state, self.store.update_state)
        self.assertNotEqual(first, "")
        # 手動把它復活，驗證「今天已跑過」門檻真的擋住第二次
        self.col.rows["stale_low"][1]["archived"] = "0"
        second = mem_mod.run_behavior_policy_decay(self.store.load_state, self.store.update_state)
        self.assertEqual(second, "")
        self.assertEqual(self.col.rows["stale_low"][1]["archived"], "0")

    def test_already_archived_rules_are_left_alone(self):
        self._seed("already_archived", confidence="0.9", days_old=0, archived="1")
        note = mem_mod.run_behavior_policy_decay(self.store.load_state, self.store.update_state)
        self.assertEqual(note, "")
        self.assertNotIn("archived_reason", self.col.rows["already_archived"][1])

    def test_no_rules_below_floor_returns_empty_but_marks_ran(self):
        self._seed("healthy", confidence="1.0", days_old=0)
        note = mem_mod.run_behavior_policy_decay(self.store.load_state, self.store.update_state)
        self.assertEqual(note, "")
        self.assertEqual(
            self.store.load_state()[mem_mod._BEHAVIOR_DECAY_STATE_KEY],
            datetime.now().date().isoformat(),
        )


class MemoryGovernanceReportTests(_FakeCollectionMixin):
    def _seed(self, doc_id, scenario, *, confidence="1.0", days_old=0.0,
              archived="0", archived_reason="", archived_at=""):
        meta = {
            "source": "behavior_policy",
            "scenario": scenario,
            "rule": "規則內容",
            "visibility_scope": "owner_only",
            "confidence": confidence,
            "archived": archived,
            "ts": _iso(days_old),
        }
        if archived_reason:
            meta["archived_reason"] = archived_reason
        if archived_at:
            meta["archived_at"] = archived_at
        self.col.rows[doc_id] = (f"【行為準則】{doc_id}", meta)

    def test_no_rules_still_renders_full_report(self):
        # 零規則不提前 return——「還沒學任何規則、卻反覆被糾正」正是
        # 糾正訊號段最該被看到的情境。
        report = mem_mod.memory_governance_report()
        self.assertIn("共 0 條生效中、0 條已停用", report)
        self.assertIn("resolve_conflict", report)

    def test_flags_rules_near_review_floor(self):
        self._seed("healthy", "健康規則", confidence="1.0", days_old=0)
        self._seed("weak", "快衰減規則", confidence="0.3", days_old=100)
        report = mem_mod.memory_governance_report()
        self.assertIn("即將衰減", report)
        self.assertIn("快衰減規則", report)
        self.assertNotIn("健康規則", report)

    def test_lists_recently_archived_with_reason(self):
        self._seed("gone", "被取代的規則", archived="1",
                   archived_reason="superseded", archived_at=_iso(1))
        report = mem_mod.memory_governance_report()
        self.assertIn("最近停用", report)
        self.assertIn("被取代的規則", report)
        self.assertIn("superseded", report)


class ResolveConflictTests(_FakeCollectionMixin):
    def _seed(self, doc_id, scenario, *, archived="0", source="behavior_policy"):
        self.col.rows[doc_id] = (
            f"【行為準則】{doc_id}",
            {
                "source": source,
                "scenario": scenario,
                "rule": "規則內容",
                "visibility_scope": "owner_only",
                "confidence": "1.0",
                "archived": archived,
                "ts": _iso(0),
            },
        )

    def test_archive_active_rule(self):
        self._seed("id1", "情境A")
        msg = mem_mod.resolve_conflict("id1", "archive")
        self.assertIn("✅", msg)
        self.assertIn("停用", msg)
        self.assertEqual(self.col.rows["id1"][1]["archived"], "1")
        self.assertEqual(self.col.rows["id1"][1]["archived_reason"], "manual")

    def test_revive_archived_rule(self):
        self._seed("id1", "情境A", archived="1")
        msg = mem_mod.resolve_conflict("id1", "revive")
        self.assertIn("✅", msg)
        self.assertIn("恢復", msg)
        self.assertEqual(self.col.rows["id1"][1]["archived"], "0")
        self.assertIn("revived_at", self.col.rows["id1"][1])

    def test_archive_already_archived_is_noop_message(self):
        self._seed("id1", "情境A", archived="1")
        msg = mem_mod.resolve_conflict("id1", "archive")
        self.assertIn("本來就已停用", msg)

    def test_revive_already_active_is_noop_message(self):
        self._seed("id1", "情境A")
        msg = mem_mod.resolve_conflict("id1", "revive")
        self.assertIn("本來就生效中", msg)

    def test_unknown_id_returns_error(self):
        msg = mem_mod.resolve_conflict("does-not-exist", "archive")
        self.assertIn("找不到", msg)

    def test_invalid_action_rejected(self):
        self._seed("id1", "情境A")
        msg = mem_mod.resolve_conflict("id1", "delete")
        self.assertIn("錯誤", msg)
        self.assertEqual(self.col.rows["id1"][1]["archived"], "0")

    def test_refuses_non_behavior_policy_ids(self):
        self._seed("note-id", "無關筆記", source="note")
        msg = mem_mod.resolve_conflict("note-id", "archive")
        self.assertIn("不是 behavior_policy", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
