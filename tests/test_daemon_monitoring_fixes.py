"""2026-07 深度健檢：daemon / 監控修復的回歸測試。

涵蓋：
  1. mailcheck / ponder — notify 成功才記 dedup（送失敗下輪重試）
  2. dashboard_alerts — warn/crit 收斂成單一穩定 id；daemon_fail per-daemon id
  3. alert_pusher — 舊 id 一次性遷移（不誤推「✅ 已恢復」）；email fallback 讀 notify bool
  4. dispatcher — 每任務立即存檔、存檔先於通知
  5. health_check — behavior policy 衰減搬進共用實作（兩個入口都拿到）
  6. health — expected launchd 補 alert_check/chroma/tool_rpc/web_server（optional 機制）
  7. health — parquet 只驗 schema 不全量載入
  8. dashboard_trends / dashboard — cost.jsonl 反向讀 + 視窗早停
  9. status_center / dashboard / dashboard_api — alert 電池一輪只跑一次
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ────────────────────────────────────────────────────────────────────
# 1a. mailcheck：notify 成功才記 dedup
# ────────────────────────────────────────────────────────────────────
class MailcheckNotifyGateTests(unittest.TestCase):
    def _run_task(self, notify_result):
        from agent_core.daemon_mailcheck import task_mailcheck

        service = mock.MagicMock()
        service.users.return_value.messages.return_value.list.return_value \
            .execute.return_value = {"messages": [{"id": "m1"}]}
        service.users.return_value.messages.return_value.get.return_value \
            .execute.return_value = {"payload": {"headers": []}}

        events: list[tuple] = []

        def fake_notify(**kwargs):
            events.append(("notify", kwargs.get("task_name")))
            return notify_result

        def fake_update_state(mutate_fn):
            st: dict = {}
            mutate_fn(st)
            events.append(("update_state", st))

        task_mailcheck(
            get_service=lambda *_a, **_k: service,
            classify_email_raw=lambda _mid: {
                "urgency": "R", "category": "客戶訂單",
                "from": "a@b.c", "subject": "急", "reason": "急件",
            },
            extract_body=lambda _p: "body",
            gemini_generate=lambda **_kw: SimpleNamespace(text="draft"),
            gemini_model="fake-model",
            notify=fake_notify,
            load_state=lambda: {},
            update_state=fake_update_state,
        )
        return events

    def test_notify_failure_skips_dedup_so_next_tick_retries(self):
        """健檢 Medium：以前「先記 dedup 再寄」+ notify 吞失敗 → 急件通知
        一次失敗即永久漏掉。現在寄失敗（notify 回 False）整輪不記。"""
        events = self._run_task(notify_result=False)
        kinds = [e[0] for e in events]
        self.assertIn("notify", kinds)
        self.assertNotIn("update_state", kinds,
                         "notify 失敗時不得記 mailcheck_notified_ids")

    def test_notify_success_records_after_send(self):
        events = self._run_task(notify_result=True)
        kinds = [e[0] for e in events]
        self.assertEqual(kinds, ["notify", "update_state"],
                         "必須先寄成功、才記 dedup（順序不可顛倒）")
        _, st = events[1]
        self.assertEqual(st["mailcheck_notified_ids"], ["m1"])

    def test_legacy_notify_returning_none_still_records(self):
        """向後相容：舊 notify（回 None）視為成功 — 只有明確 False 才擋。"""
        events = self._run_task(notify_result=None)
        self.assertIn("update_state", [e[0] for e in events])


# ────────────────────────────────────────────────────────────────────
# 1b. ponder：notify 回 False 就不記 seen
# ────────────────────────────────────────────────────────────────────
class PonderNotifyGateTests(unittest.TestCase):
    def _run_task(self, notify_result):
        from agent_core.daemon_ponder import task_ponder

        remembered: list = []

        task_ponder(
            in_working_hours=lambda: True,
            work_hour_start=8,
            work_hour_end=19,
            load_state=lambda: {"ponder_seen_hashes": []},
            summarize_inbox=lambda **_kw: "1 封未讀",
            get_service=mock.MagicMock(side_effect=RuntimeError("no calendar")),
            search_gmail=lambda _q: "",
            gemini_generate=lambda **_kw: SimpleNamespace(text="🔔 提醒大王追 PO#123"),
            gemini_model="fake-model",
            extract_fresh_insights_fn=lambda text, _seen: [
                line for line in text.split("\n") if line.startswith("🔔")
            ],
            remember_ponder_insights_fn=lambda seen, fresh: remembered.append(fresh),
            notify=lambda **_kw: notify_result,
        )
        return remembered

    def test_notify_false_does_not_mark_seen(self):
        """健檢 Medium：ponder「送成功才記 seen」的保護以前因 notify 永不
        raise 形同虛設；現在 notify 回 False 也要擋。"""
        self.assertEqual(self._run_task(notify_result=False), [])

    def test_notify_true_marks_seen(self):
        remembered = self._run_task(notify_result=True)
        self.assertEqual(remembered, [["🔔 提醒大王追 PO#123"]])


# ────────────────────────────────────────────────────────────────────
# 2. dashboard_alerts：warn/crit 單一穩定 id；daemon_fail per-daemon id
# ────────────────────────────────────────────────────────────────────
class AlertStableIdTests(unittest.TestCase):
    def test_runs_err_pct_same_id_for_warn_and_crit(self):
        from agent_core import dashboard_alerts

        for err_pct, expect_level in ((45.0, "warn"), (75.0, "crit")):
            with mock.patch(
                "agent_core.dashboard_trends.runs_trend",
                return_value={"today": {"total": 20, "err": round(20 * err_pct / 100)},
                              "today_err_pct": err_pct},
            ):
                alerts = dashboard_alerts._check_runs_errors()
            self.assertEqual(alerts[0]["id"], "runs_err_pct")
            self.assertEqual(alerts[0]["level"], expect_level)

    def test_errors_log_same_id_for_warn_and_crit(self):
        from agent_core import dashboard_alerts

        for today, expect_level in ((150, "warn"), (600, "crit")):
            with mock.patch(
                "agent_core.dashboard_trends.errors_trend",
                return_value={"today": today, "7d_avg": today},  # ratio=1 → 不觸發 ratio alert
            ):
                alerts = dashboard_alerts._check_error_log()
            log_alerts = [a for a in alerts if a["id"] == "errors_log"]
            self.assertEqual(len(log_alerts), 1)
            self.assertEqual(log_alerts[0]["level"], expect_level)

    def _daemon_health(self, failed_labels):
        from agent_core import dashboard_alerts

        out = "".join(f"-\t1\tcom.xiaohong.{name}\n" for name in failed_labels)
        with mock.patch.object(
            dashboard_alerts.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout=out),
        ), mock.patch.object(
            dashboard_alerts, "is_known_daemon_recovering", return_value=False,
        ), mock.patch.object(
            dashboard_alerts, "is_benign_stopped_daemon", return_value=False,
        ):
            return dashboard_alerts._check_daemon_health()

    def test_three_failures_keep_individual_ids_and_escalate(self):
        """健檢 Medium：第 3 台失敗以前整批換成單一 daemon_multi_fail id →
        個別 id 消失 → pusher 對還在失敗的 daemon 推「已恢復」誤報。
        現在 id 永遠 per-daemon，規模用 level 表達（≥3 → crit）。"""
        alerts = self._daemon_health(["aaa", "bbb", "ccc"])
        self.assertEqual(
            sorted(a["id"] for a in alerts),
            ["daemon_fail_aaa", "daemon_fail_bbb", "daemon_fail_ccc"],
        )
        self.assertTrue(all(a["level"] == "crit" for a in alerts))
        self.assertTrue(all(a["metric"]["failed_count"] == 3 for a in alerts))
        self.assertNotIn("daemon_multi_fail", [a["id"] for a in alerts])

    def test_single_failure_stays_warn_with_same_id_shape(self):
        alerts = self._daemon_health(["aaa"])
        self.assertEqual([a["id"] for a in alerts], ["daemon_fail_aaa"])
        self.assertEqual(alerts[0]["level"], "warn")

    def test_recovery_of_one_daemon_keeps_others_ids(self):
        """3 台 → 2 台：剩下兩台的 id 不變（不會像舊 multi_fail 那樣整批
        消失再重生），只有恢復那台的 id 從清單移除。"""
        before = {a["id"] for a in self._daemon_health(["aaa", "bbb", "ccc"])}
        after = {a["id"] for a in self._daemon_health(["aaa", "bbb"])}
        self.assertEqual(before - after, {"daemon_fail_ccc"})
        self.assertTrue(after.issubset(before))


# ────────────────────────────────────────────────────────────────────
# 3. alert_pusher：舊 id 遷移 + email fallback 讀 notify bool
# ────────────────────────────────────────────────────────────────────
class AlertPusherLegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        import importlib
        import agent_core.alert_pusher as ap
        importlib.reload(ap)
        self.ap = ap
        self.tmpdir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmpdir, "alert_push_state.json")
        self.ap._PUSH_STATE_FILE = self.state_file
        # 隔離：不碰 Postgres store（live 環境可能開啟）
        self._pg_patch = mock.patch.object(self.ap, "_pg_alert_store", return_value=None)
        self._pg_patch.start()

    def tearDown(self):
        self._pg_patch.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_migrate_renames_and_drops(self):
        state = {
            "cost_today_high": {"level": "crit", "last_pushed_at": "2026-07-10T00:00:00"},
            "daemon_multi_fail": {"level": "crit"},
            "chroma_server_down": {"level": "crit"},  # 現行 id 不受影響
        }
        self.ap._migrate_legacy_alert_ids(state)
        self.assertIn("cost_today", state)
        self.assertNotIn("cost_today_high", state)
        self.assertNotIn("daemon_multi_fail", state)
        self.assertIn("chroma_server_down", state)

    def test_migrate_collision_keeps_new_id_entry(self):
        state = {
            "cost_today": {"level": "crit", "marker": "new"},
            "cost_today_warn": {"level": "warn", "marker": "old"},
        }
        self.ap._migrate_legacy_alert_ids(state)
        self.assertEqual(state["cost_today"]["marker"], "new")
        self.assertNotIn("cost_today_warn", state)

    def _write_state(self, state):
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)

    def test_no_false_recovery_after_id_rename(self):
        """state 檔殘留舊 id（cost_today_high）、新一輪 alert 用新 id
        （cost_today）→ 不得推「✅ 已恢復」也不得重複 push（節流仍生效）。"""
        now_iso = datetime.now().isoformat(timespec="seconds")
        self._write_state({
            "cost_today_high": {
                "first_seen_at": now_iso, "last_pushed_at": now_iso,
                "title": "今日成本超過上限", "level": "crit",
            },
        })
        active = [{"id": "cost_today", "level": "crit",
                   "title": "今日成本超過上限", "detail": "d"}]
        pushes: list[str] = []
        with mock.patch("agent_core.dashboard_alerts.check_alerts",
                        return_value=active), \
             mock.patch.object(self.ap, "_try_telegram_push",
                               side_effect=lambda m: (pushes.append(m) or (True, ""))):
            result = self.ap.push_pending_alerts()
        self.assertEqual(result["recovered"], 0,
                         "舊 id 改名不得被誤判成 recovered")
        self.assertEqual(result["pushed"], 0,
                         "剛推過（節流內）不得因 id 改名重複 push")
        self.assertEqual(pushes, [])

    def test_warn_to_crit_escalation_bypasses_throttle_on_stable_id(self):
        """id 收斂後 warn→crit 升級走 _escalated 繞過 6h 節流 — 這正是舊拆
        id 設計永遠踩不到的路徑。"""
        recent = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        self._write_state({
            "cost_today": {
                "first_seen_at": recent, "last_pushed_at": recent,
                "title": "今日成本偏高", "level": "warn",
            },
        })
        active = [{"id": "cost_today", "level": "crit",
                   "title": "今日成本超過上限", "detail": "d"}]
        with mock.patch("agent_core.dashboard_alerts.check_alerts",
                        return_value=active), \
             mock.patch.object(self.ap, "_try_telegram_push",
                               return_value=(True, "")):
            result = self.ap.push_pending_alerts()
        self.assertEqual(result["pushed"], 1, "warn→crit 必須立即升級推送")


class AlertPusherEmailFallbackBoolTests(unittest.TestCase):
    def test_fallback_fails_when_notify_reports_false(self):
        from agent_core import alert_pusher, daemon_helpers

        with mock.patch.object(daemon_helpers, "notify", return_value=False):
            ok, err = alert_pusher._try_email_fallback("s", "b")
        self.assertFalse(ok)
        self.assertIn("寄信失敗", err)

    def test_fallback_succeeds_when_notify_returns_true(self):
        from agent_core import alert_pusher, daemon_helpers

        with mock.patch.object(daemon_helpers, "notify", return_value=True):
            ok, err = alert_pusher._try_email_fallback("s", "b")
        self.assertTrue(ok)
        self.assertEqual(err, "")


# ────────────────────────────────────────────────────────────────────
# 4. dispatcher：每任務立即存檔、存檔先於通知
# ────────────────────────────────────────────────────────────────────
class DispatcherPerTaskPersistenceTests(unittest.TestCase):
    def _run(self, tasks, run_side_effect):
        from agent_core.daemon_dispatcher import (
            dispatcher_result_is_empty,
            mark_dispatcher_task_failed,
            mark_dispatcher_task_succeeded,
            remember_dispatcher_result,
            task_dispatcher,
        )

        events: list[tuple] = []
        data = {"version": 1, "tasks": tasks}

        def fake_save(d):
            events.append(("save", copy.deepcopy(d)))
            return True

        task_dispatcher(
            load_daemon_tasks=lambda: data,
            save_daemon_tasks=fake_save,
            should_run_task_fn=lambda _t, _n: True,
            run_one_dispatcher_task_fn=run_side_effect,
            mark_dispatcher_task_failed_fn=mark_dispatcher_task_failed,
            mark_dispatcher_task_succeeded_fn=mark_dispatcher_task_succeeded,
            dispatcher_result_is_empty_fn=dispatcher_result_is_empty,
            remember_dispatcher_result_fn=remember_dispatcher_result,
            notify_dispatcher_result_fn=lambda t, r: events.append(
                ("notify", t.get("name"))),
            network_is_up_fn=lambda: True,
        )
        return events

    def test_state_saved_per_task_and_before_notify(self):
        """健檢 Medium：以前整批跑完才存 state、通知先於存檔 → redeploy 打斷
        後已通知任務重跑重寄。現在每任務 mark 後立即存、且存檔在 notify 前。"""
        tasks = [{"name": "t1", "prompt": "p"}, {"name": "t2", "prompt": "p"}]
        events = self._run(tasks, lambda task: f"result-{task['name']}")
        kinds = [e[0] for e in events]
        self.assertEqual(kinds, ["save", "notify", "save", "notify"],
                         "順序必須是逐任務「先存檔、再通知」")
        # 第一次存檔（t1 通知前）就必須含 t1 的 last_run_at + dedup hash，
        # 但 t2 還沒跑（沒有 last_run_at）
        first_saved = events[0][1]["tasks"]
        self.assertTrue(first_saved[0].get("last_run_at"))
        self.assertTrue(first_saved[0].get("dedup_hashes"))
        self.assertFalse(first_saved[1].get("last_run_at"))

    def test_failed_task_state_saved_immediately(self):
        tasks = [{"name": "bad", "prompt": "p"}, {"name": "good", "prompt": "p"}]

        def run(task):
            if task["name"] == "bad":
                raise RuntimeError("boom")
            return "(無新發現)"  # 空結果 → 不通知

        events = self._run(tasks, run)
        kinds = [e[0] for e in events]
        self.assertEqual(kinds, ["save", "save"], "失敗與空結果各自立即存檔")
        first_saved = events[0][1]["tasks"]
        self.assertIn("boom", first_saved[0].get("last_error") or "")

    def test_duplicate_result_still_persists_dedup_before_skip(self):
        tasks = [{"name": "t1", "prompt": "p",
                  "dedup_hashes": []}]
        # 先跑一次記 hash，再驗第二輪重複 → 存檔照做、不通知
        events1 = self._run(copy.deepcopy(tasks), lambda _t: "same result")
        self.assertEqual([e[0] for e in events1], ["save", "notify"])
        seeded = events1[0][1]["tasks"]
        events2 = self._run(copy.deepcopy(seeded), lambda _t: "same result")
        self.assertEqual([e[0] for e in events2], ["save"],
                         "重複結果：存檔（last_run_at 更新）但不通知")


# ────────────────────────────────────────────────────────────────────
# 5. health_check：behavior policy 衰減在共用實作
# ────────────────────────────────────────────────────────────────────
class HealthCheckDecayTests(unittest.TestCase):
    def test_shared_task_health_check_runs_decay(self):
        """健檢 Medium：衰減以前只掛在 agent_daemon 備用路徑，生產 launchd
        跑的 launchd/scripts/health_check.py 拿不到。現在搬進共用實作，兩個
        入口都經過。"""
        from agent_core import daemon_health_check

        calls = {}

        def fake_decay(load_state, update_state):
            calls["args"] = (load_state, update_state)
            return "decay note"

        load_state = lambda: {}  # noqa: E731
        update_state = lambda fn: fn({})  # noqa: E731
        with mock.patch("agent_core.memory.run_behavior_policy_decay",
                        side_effect=fake_decay):
            daemon_health_check.task_health_check(
                health_check_fn=lambda auto_repair: "✅ 一切正常",
                load_state=load_state,
                update_state=update_state,
                notify=lambda **_kw: True,
            )
        self.assertEqual(calls.get("args"), (load_state, update_state))

    def test_decay_failure_does_not_block_health_check(self):
        from agent_core import daemon_health_check

        ran = {}
        with mock.patch("agent_core.memory.run_behavior_policy_decay",
                        side_effect=RuntimeError("chroma down")):
            daemon_health_check.task_health_check(
                health_check_fn=lambda auto_repair: ran.setdefault("ok", True) and "✅",
                load_state=lambda: {},
                update_state=lambda fn: fn({}),
                notify=lambda **_kw: True,
            )
        self.assertTrue(ran.get("ok"), "衰減失敗不得擋掉健康檢查本體")

    def test_agent_daemon_no_longer_duplicates_decay_call(self):
        """防漂移：agent_daemon.task_health_check 的重複衰減呼叫已移除
        （共用實作是唯一出處）。"""
        path = os.path.join(_REPO_ROOT, "agent_daemon.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("run_behavior_policy_decay", src)


# ────────────────────────────────────────────────────────────────────
# 6. health：expected launchd 涵蓋告警管線 + 基礎設施
# ────────────────────────────────────────────────────────────────────
class HealthExpectedLaunchdTests(unittest.TestCase):
    NEW_LABELS = (
        "com.xiaohong.alert_check",
        "com.xiaohong.chroma",
        "com.xiaohong.tool_rpc",
        "com.xiaohong.web_server",
    )

    def test_new_labels_expected_when_plist_deployed(self):
        """健檢 Medium：alert_check/chroma/tool_rpc/web_server 以前漏列 —
        告警管線自己死掉沒人看。plist 有部署就必須納入監看。"""
        from agent_core import health

        for label in self.NEW_LABELS:
            self.assertIn(label, health._HEALTH_EXPECTED_LAUNCHD)
        with mock.patch.object(health.os.path, "exists", return_value=True):
            labels = health._expected_launchd_labels()
        for label in self.NEW_LABELS:
            self.assertIn(label, labels)

    def test_new_labels_optional_when_plist_missing(self):
        """未部署環境（dev clone / CI）不得誤報「沒載入」。"""
        from agent_core import health

        for label in self.NEW_LABELS:
            self.assertIn(label, health._HEALTH_OPTIONAL_LAUNCHD)
        with mock.patch.object(health.os.path, "exists", return_value=False):
            labels = health._expected_launchd_labels()
        for label in self.NEW_LABELS:
            self.assertNotIn(label, labels)

    def test_keepalive_services_marked_long_running(self):
        from agent_core import health

        for label in ("com.xiaohong.chroma", "com.xiaohong.tool_rpc",
                      "com.xiaohong.web_server"):
            self.assertIn(label, health._HEALTH_LONG_RUNNING)
        # alert_check 是 StartInterval tick daemon — 平時沒 PID 是正常的
        self.assertNotIn("com.xiaohong.alert_check", health._HEALTH_LONG_RUNNING)


# ────────────────────────────────────────────────────────────────────
# 7. health：parquet 只驗 schema
# ────────────────────────────────────────────────────────────────────
class HealthParquetSchemaOnlyTests(unittest.TestCase):
    def _run_check(self, parquet_path):
        from agent_core import health

        fake_col = mock.MagicMock()
        fake_col.count.return_value = 1
        with mock.patch.object(health, "_get_memory_collection",
                               return_value=fake_col), \
             mock.patch.object(health, "_EMAIL_LAKE_PARQUET", parquet_path), \
             mock.patch("agent_core.operational_health.health_issues",
                        return_value=[]):
            return health._check_data_stores()

    def test_valid_parquet_passes_via_schema_read(self):
        import pyarrow as pa
        import pyarrow.parquet as pq

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "emails.parquet")
            pq.write_table(pa.table({"a": [1, 2]}), path)
            issues = self._run_check(path)
        self.assertEqual([i for i in issues if i["area"] == "email_lake"], [])

    def test_corrupt_parquet_reports_issue(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "emails.parquet")
            with open(path, "wb") as f:
                f.write(b"not a parquet file")
            issues = self._run_check(path)
        lake = [i for i in issues if i["area"] == "email_lake"]
        self.assertEqual(len(lake), 1)
        self.assertIn("Parquet", lake[0]["msg"])

    def test_health_no_longer_full_loads_lake(self):
        """防漂移：health.py 不得再 import/呼叫 _lake_load_df（全量載入）。
        只掃程式碼行（註解裡的歷史說明不算）。"""
        path = os.path.join(_REPO_ROOT, "agent_core", "health.py")
        with open(path, encoding="utf-8") as f:
            code_lines = [ln for ln in f.read().splitlines()
                          if not ln.strip().startswith("#")]
        self.assertNotIn("_lake_load_df", "\n".join(code_lines))


# ────────────────────────────────────────────────────────────────────
# 8. cost.jsonl 反向讀 + 視窗早停
# ────────────────────────────────────────────────────────────────────
class CostTrendWindowTests(unittest.TestCase):
    def _make_cost_log(self, tmpdir):
        path = os.path.join(tmpdir, "cost.jsonl")
        now = datetime.now()
        rows = [
            {"ts": (now - timedelta(days=40)).isoformat(timespec="seconds"),
             "cost_usd": 9.0, "model": "old-model"},
            {"ts": (now - timedelta(days=1)).isoformat(timespec="seconds"),
             "cost_usd": 0.5, "model": "m-y"},
            {"ts": now.isoformat(timespec="seconds"),
             "cost_usd": 0.25, "model": "m-t"},
            {"ts": now.isoformat(timespec="seconds"),
             "cost_usd": 0.25, "model": "m-t"},
        ]
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return path

    def test_cost_trend_uses_bounded_reverse_window(self):
        """健檢 Medium：cost_trend 以前前向掃全檔（25MB/89K 行、alert daemon
        每 5 分鐘跑）。現在必須走 cost_tracker._load_jsonl_window（反向讀 +
        30 天早停），數字不變。"""
        from agent_core import cost_tracker, dashboard_trends

        with tempfile.TemporaryDirectory() as d:
            path = self._make_cost_log(d)
            real_window = cost_tracker._load_jsonl_window
            calls = {}

            def spy(p, hours, **kw):
                calls["hours"] = hours
                return real_window(p, hours, **kw)

            with mock.patch.object(cost_tracker, "_COST_LOG", path), \
                 mock.patch.object(cost_tracker, "_load_jsonl_window",
                                   side_effect=spy):
                trend = dashboard_trends.cost_trend()

        self.assertEqual(calls.get("hours"), 31 * 24,
                         "必須用 31 天視窗反向讀（涵蓋 30 天過濾）")
        self.assertAlmostEqual(trend["today_usd"], 0.5)
        self.assertEqual(trend["today_calls"], 2)
        self.assertAlmostEqual(trend["yesterday_usd"], 0.5)
        # 40 天前的 9.0 不得混入任何 avg（視窗 + 日期過濾雙保險）
        self.assertLess(trend["30d_avg_usd"], 1.0)

    def test_cost_trend_splits_today_embedding_spend(self):
        """假警報普查 D：cost_ratio 觸發時要能一眼分辨「異常暴衝 vs 背填」，
        所以 trend 要把今日 embedding 金額單獨算出來（判定走 model 名）。"""
        from agent_core import cost_tracker, dashboard_trends

        now = datetime.now()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cost.jsonl")
            rows = [
                {"ts": now.isoformat(timespec="seconds"),
                 "cost_usd": 0.20, "model": "gemini-3-flash-preview"},
                {"ts": now.isoformat(timespec="seconds"),
                 "cost_usd": 0.60, "model": "gemini-embedding-001"},
                {"ts": (now - timedelta(days=1)).isoformat(timespec="seconds"),
                 "cost_usd": 5.0, "model": "gemini-embedding-001"},
            ]
            with open(path, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            with mock.patch.object(cost_tracker, "_COST_LOG", path):
                trend = dashboard_trends.cost_trend()

        self.assertAlmostEqual(trend["today_usd"], 0.80)
        self.assertAlmostEqual(trend["today_embedding_usd"], 0.60)  # 昨天那筆不算

    def test_section_cost_model_breakdown_uses_window(self):
        from agent_core import cost_tracker, dashboard

        with tempfile.TemporaryDirectory() as d:
            path = self._make_cost_log(d)
            real_window = cost_tracker._load_jsonl_window
            hours_seen: list = []

            def spy(p, hours, **kw):
                hours_seen.append(hours)
                return real_window(p, hours, **kw)

            with mock.patch.object(cost_tracker, "_COST_LOG", path), \
                 mock.patch.object(cost_tracker, "_load_jsonl_window",
                                   side_effect=spy):
                text = dashboard._section_cost()

        self.assertIn("今日模型分佈", text)
        self.assertIn("m-t=2", text)
        self.assertNotIn("old-model", text)
        self.assertIn(25, hours_seen, "模型分佈掃描必須用今日視窗（25h）反向讀")


# ────────────────────────────────────────────────────────────────────
# 9. alert 電池一輪只跑一次
# ────────────────────────────────────────────────────────────────────
class HealthScoreOverviewReuseTests(unittest.TestCase):
    _OVERVIEW = {
        "alerts": {"crit_count": 0, "warn_count": 0},
        "daemons": {"last_exit_nonzero": []},
        "metrics": {"total_calls": 0, "success_pct": 100},
        "queue": {"dlq": 0},
        "task_memory": {"overdue": 0},
    }

    # system_status 路徑上的 _ensure_chroma_endpoint 有 process 級副作用（寫
    # RED_CHROMA_HTTP_URL / RED_EMBED_DIM 進 os.environ）。除了在測試裡 mock 掉，
    # 這裡再加一層 env 快照還原，防未來新增測試漏 mock 又把污染放出去。
    _ENV_KEYS = ("RED_CHROMA_HTTP_URL", "RED_EMBED_DIM")

    def setUp(self):
        self._env_saved = {k: os.environ.get(k) for k in self._ENV_KEYS}

    def tearDown(self):
        for k, v in self._env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_health_score_accepts_precomputed_overview(self):
        """健檢 Low：health_score(overview) 不得再自己跑 system_overview
        （= 整套 alert 電池重跑第二遍）。"""
        from agent_core import status_center

        with mock.patch.object(
            status_center, "system_overview",
            side_effect=AssertionError("不得重算 system_overview"),
        ):
            result = status_center.health_score(dict(self._OVERVIEW))
        self.assertEqual(result["score"], 100)

    def test_dashboard_api_get_health_reuses_overview(self):
        from agent_core import dashboard_api, status_center

        overview = dict(self._OVERVIEW, at="2026-07-11T00:00:00")
        calls = {"overview": 0, "score_args": []}

        def fake_overview():
            calls["overview"] += 1
            return overview

        def fake_score(o=None):
            calls["score_args"].append(o)
            return {"score": 100, "status": "🟢 healthy", "reasons": []}

        with mock.patch.object(status_center, "system_overview",
                               side_effect=fake_overview), \
             mock.patch.object(status_center, "health_score",
                               side_effect=fake_score):
            dashboard_api.get_health()

        self.assertEqual(calls["overview"], 1,
                         "get_health 一輪只能跑一次 system_overview")
        self.assertEqual(calls["score_args"], [overview],
                         "health_score 必須收到已算好的 overview")

    def test_system_status_health_and_alerts_share_one_battery(self):
        from agent_core import dashboard, status_center

        counter = {"check_alerts": 0, "overview_alerts_arg": []}

        def fake_check_alerts():
            counter["check_alerts"] += 1
            return [{"id": "x", "level": "warn", "title": "t", "detail": "d"}]

        def fake_overview(alerts=None):
            counter["overview_alerts_arg"].append(alerts)
            return dict(self._OVERVIEW)

        # _ensure_chroma_endpoint 必須擋掉：真跑會在共用 chroma server 活著時把
        # RED_CHROMA_HTTP_URL 寫進 os.environ（process 級），洩漏給同輪後續測試
        # → test_erp_oracle 的 search_drive_docs 真連向量庫、embed 時 SystemExit。
        with mock.patch.object(dashboard, "_ensure_chroma_endpoint"), \
             mock.patch("agent_core.dashboard_alerts.check_alerts",
                        side_effect=fake_check_alerts), \
             mock.patch.object(status_center, "system_overview",
                               side_effect=fake_overview):
            text = dashboard.system_status(sections="health,alerts")

        self.assertEqual(counter["check_alerts"], 1,
                         "health + alerts 兩段必須共用同一次 check_alerts")
        self.assertEqual(len(counter["overview_alerts_arg"]), 1)
        self.assertIsNotNone(counter["overview_alerts_arg"][0],
                             "共用的 alerts 必須傳進 system_overview")
        self.assertIn("🚨", text)


if __name__ == "__main__":
    unittest.main()
