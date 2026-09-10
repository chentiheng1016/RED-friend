"""2026-07-11 深度健檢回歸測試：成本追蹤與跨行程狀態修復。

涵蓋：
  #1  cost_tracker._infer_caller_tool — 模組級 skip（不再記成 gemini_client.*）
  #2  gemini_client 記帳用回應 model_version 而非請求別名
  #3  fallback 全救回的任務不記 api_error；fallback 也失敗才補記
  #4  cost_today / cost_alert 對壞 ts 行不炸
  #5  tool_budgets 檔案後端走 locked_json（跨行程 R-M-W）
  #6  google_auth token.json atomic 寫 + 損毀讀取降級
  #7  progress_manager 進鎖重讀（不吃 import 快照）
  #8  mistake_ledger 寫入 R-M-W、讀取 mtime 重載
  #9  task_memory tasks_overdue / task_summary 套 _deadline_cmp
  #10 run_history index.jsonl 修剪走 sibling .lock
  #11 tool_tiers tg_auth import 失敗不快取、fail-closed
  #13 intent_router rotation 先過 os.stat byte 閘門
  #14 query_expansion 快取過期清掃 + 上限
  #15 _atomic_write_bytes fsync
  #16 status_center._rag_summary import 對名
  #17 tool RPC 只在連線層失敗才 fallback 直跑
  #18 worker 回應 json.dumps default=str
（#12 MYSQL_PASS_SHORT 的測試在 test_security_regressions.TestV9LogRedact。）
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _fake_usage():
    return types.SimpleNamespace(
        prompt_token_count=10,
        candidates_token_count=5,
        cached_content_token_count=0,
        thoughts_token_count=0,
        tool_use_prompt_token_count=0,
        total_token_count=15,
    )


def _fake_tool_entrypoint(record_call, um):
    """模擬「某個工具模組 → gemini_client helper 鏈 → record_call」的呼叫堆疊。

    exec 出來的兩層 helper 的 f_globals["__name__"] 是 agent_core.gemini_client，
    重現 2026-06-25 拆函式後的真實 stack 形狀。"""
    src = (
        "def _gemini_generate_once(record_call, um):\n"
        "    record_call(model='gemini-2.5-flash', usage_metadata=um)\n"
        "def _gemini_generate(record_call, um):\n"
        "    _gemini_generate_once(record_call, um)\n"
    )
    g = {"__name__": "agent_core.gemini_client"}
    exec(src, g)  # noqa: S102 - 測試用的受控原始碼
    g["_gemini_generate"](record_call, um)


# ────────────────────────────────────────────────────────────────────
# #1 — _infer_caller_tool
# ────────────────────────────────────────────────────────────────────
class InferCallerToolTests(unittest.TestCase):
    def setUp(self):
        from agent_core import cost_tracker as ct
        self.ct = ct
        self.tmp = tempfile.mkdtemp(prefix="red_cost_infer_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for p in (
            mock.patch.object(ct, "_COST_LOG",
                              os.path.join(self.tmp, "cost.jsonl")),
            mock.patch.object(ct, "_pg_cost_store", lambda: None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _last_entry(self) -> dict:
        with open(self.ct._COST_LOG, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertTrue(lines, "record_call 應該有寫入 cost.jsonl")
        return json.loads(lines[-1])

    def test_caller_skips_gemini_client_frames_by_module(self):
        """核心不變量：從假 caller 模組經 gemini_client 鏈呼叫，caller 絕不能
        是 gemini_client.*（舊 bug：函式名 skip 清單漏了 _gemini_generate_once，
        live cost.jsonl 119/200 筆記成 gemini_client._gemini_generate_once）。"""
        _fake_tool_entrypoint(self.ct.record_call, _fake_usage())
        caller = self._last_entry().get("caller", "")
        self.assertFalse(
            caller.startswith("gemini_client."),
            f"caller 被記成 gemini_client 自己的 frame：{caller!r}",
        )
        expected_mod = __name__.rsplit(".", 1)[-1]
        self.assertEqual(caller, f"{expected_mod}._fake_tool_entrypoint")

    def test_explicit_caller_still_wins(self):
        self.ct.record_call(
            model="gemini-2.5-flash", usage_metadata=_fake_usage(),
            caller="drive_sync._extract_media",
        )
        self.assertEqual(self._last_entry()["caller"],
                         "drive_sync._extract_media")


# ────────────────────────────────────────────────────────────────────
# #2 / #3 — gemini_client 記帳 model_version + fallback api_error
# ────────────────────────────────────────────────────────────────────
class GeminiAccountingTests(unittest.TestCase):
    def setUp(self):
        from agent_core import gemini_client as gc
        self.gc = gc
        p = mock.patch.object(gc, "_gemini_circuit_enabled", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _client_returning(self, resp):
        fake = mock.Mock()
        fake.models.generate_content.return_value = resp
        return fake

    def test_record_call_uses_response_model_version(self):
        resp = types.SimpleNamespace(model_version="gemini-2.5-flash",
                                     usage_metadata=_fake_usage(), text="hi")
        with mock.patch.object(self.gc, "_get_gemini_client",
                               return_value=self._client_returning(resp)), \
             mock.patch("agent_core.cost_tracker.record_call") as rec:
            out = self.gc._gemini_generate_once("gemini-flash-latest", ["x"])
        self.assertIs(out, resp)
        rec.assert_called_once()
        self.assertEqual(rec.call_args.kwargs["model"], "gemini-2.5-flash")

    def test_record_call_falls_back_to_request_alias(self):
        resp = types.SimpleNamespace(usage_metadata=_fake_usage(), text="hi")
        with mock.patch.object(self.gc, "_get_gemini_client",
                               return_value=self._client_returning(resp)), \
             mock.patch("agent_core.cost_tracker.record_call") as rec:
            self.gc._gemini_generate_once("gemini-flash-latest", ["x"])
        self.assertEqual(rec.call_args.kwargs["model"], "gemini-flash-latest")

    def _failing_then(self, ok_model: str, resp):
        """primary 一律 503、ok_model 成功的 fake client。"""
        def gen(**kwargs):
            if kwargs["model"] == ok_model:
                return resp
            raise RuntimeError("503 Service Unavailable")
        fake = mock.Mock()
        fake.models.generate_content.side_effect = gen
        return fake

    def test_fallback_success_records_no_api_error(self):
        resp = types.SimpleNamespace(model_version="fallback-flash",
                                     usage_metadata=_fake_usage(), text="ok")
        with mock.patch.dict(os.environ,
                             {"RED_GEMINI_FALLBACK_MODEL": "fallback-flash"}), \
             mock.patch.object(self.gc, "_get_gemini_client",
                               return_value=self._failing_then("fallback-flash", resp)), \
             mock.patch("agent_core.cost_tracker.record_api_error") as err, \
             mock.patch("agent_core.cost_tracker.record_call"):
            out = self.gc._gemini_generate("primary-pro", ["x"], max_attempts=1)
        self.assertIs(out, resp)
        err.assert_not_called()  # 任務被 fallback 救回 → 不是任務失敗

    def test_both_fail_records_primary_and_fallback(self):
        fake = mock.Mock()
        fake.models.generate_content.side_effect = RuntimeError(
            "503 Service Unavailable")
        with mock.patch.dict(os.environ,
                             {"RED_GEMINI_FALLBACK_MODEL": "fallback-flash"}), \
             mock.patch.object(self.gc, "_get_gemini_client",
                               return_value=fake), \
             mock.patch("agent_core.cost_tracker.record_api_error") as err:
            with self.assertRaises(RuntimeError):
                self.gc._gemini_generate("primary-pro", ["x"], max_attempts=1)
        models = {c.kwargs.get("model") for c in err.call_args_list}
        self.assertEqual(models, {"primary-pro", "fallback-flash"})

    def test_no_fallback_records_single_error(self):
        fake = mock.Mock()
        fake.models.generate_content.side_effect = RuntimeError(
            "503 Service Unavailable")
        env = dict(os.environ)
        env.pop("RED_GEMINI_FALLBACK_MODEL", None)
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(self.gc, "_get_gemini_client",
                               return_value=fake), \
             mock.patch("agent_core.cost_tracker.record_api_error") as err:
            with self.assertRaises(RuntimeError):
                self.gc._gemini_generate("primary-pro", ["x"], max_attempts=1)
        self.assertEqual(err.call_count, 1)
        self.assertEqual(err.call_args.kwargs.get("model"), "primary-pro")

    def test_fallback_not_worth_trying_still_records_primary(self):
        fake = mock.Mock()
        fake.models.generate_content.side_effect = RuntimeError("boom")
        with mock.patch.dict(os.environ,
                             {"RED_GEMINI_FALLBACK_MODEL": "fallback-flash"}), \
             mock.patch.object(self.gc, "_get_gemini_client",
                               return_value=fake), \
             mock.patch.object(self.gc, "_should_try_gemini_fallback",
                               return_value=False), \
             mock.patch("agent_core.cost_tracker.record_api_error") as err:
            with self.assertRaises(RuntimeError):
                self.gc._gemini_generate("primary-pro", ["x"], max_attempts=1)
        self.assertEqual(err.call_count, 1)
        self.assertEqual(err.call_args.kwargs.get("model"), "primary-pro")


# ────────────────────────────────────────────────────────────────────
# #4 — cost_today / cost_alert 對壞 ts 行的韌性
# ────────────────────────────────────────────────────────────────────
class CorruptTsCostQueryTests(unittest.TestCase):
    def setUp(self):
        from agent_core import cost_tracker as ct
        self.ct = ct
        self.tmp = tempfile.mkdtemp(prefix="red_cost_ts_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = os.path.join(self.tmp, "cost.jsonl")
        good = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "model": "gemini-2.5-flash", "prompt_tokens": 10,
            "output_tokens": 5, "thinking_tokens": 0, "cached_tokens": 0,
            "tool_use_tokens": 0, "total_tokens": 15, "cost_usd": 0.5,
            "duration_ms": 1.0, "caller": "x.y",
            # 這列的 cost_usd 是為了斷言好讀而寫死的，跟 10/5 顆 token 並不相符。
            # 標成當前計價紀元，_normalize_entry_costs 才會放它過 —— 否則會被依
            # raw token 重算成 ~$0.00002，本測試想驗的「壞 ts 不炸」就被掩蓋了。
            "pricing_ver": ct._PRICING_VERSION,
        }
        lines = [
            json.dumps({"ts": "GARBAGE-NOT-A-TIMESTAMP", "cost_usd": 1.0}),
            json.dumps({"cost_usd": 2.0}),  # 完全沒 ts
            json.dumps(good),
        ]
        with open(self.log, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        for p in (
            mock.patch.object(ct, "_COST_LOG", self.log),
            mock.patch.object(ct, "_pg_cost_store", lambda: None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_cost_today_survives_corrupt_ts_line(self):
        # _load_jsonl_window 刻意保留壞 ts 行 → 舊版裸 fromisoformat 直接炸
        out = self.ct.cost_today()
        self.assertIn("$0.5000", out)

    def test_cost_alert_survives_corrupt_ts_line(self):
        out = self.ct.cost_alert(daily_budget_usd=5.0)
        self.assertIn("今日已花", out)
        self.assertIn("0.5", out)


# ────────────────────────────────────────────────────────────────────
# #5 — tool_budgets 檔案後端跨行程 R-M-W
# ────────────────────────────────────────────────────────────────────
class ToolBudgetsLockedJsonTests(unittest.TestCase):
    def setUp(self):
        from agent_core import tool_budgets as tb
        self.tb = tb
        self.tmp = tempfile.mkdtemp(prefix="red_budget_lock_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_dir = tb._BUDGET_DIR
        tb._BUDGET_DIR = self.tmp
        self.addCleanup(setattr, tb, "_BUDGET_DIR", self._orig_dir)
        os.environ["RED_BUDGET_XP_TOOL_DAILY"] = "100"
        self.addCleanup(os.environ.pop, "RED_BUDGET_XP_TOOL_DAILY", None)

    def _read_state(self) -> dict:
        with open(self.tb._state_path(), "r", encoding="utf-8") as f:
            return json.load(f)

    def test_record_use_picks_up_external_writes(self):
        """R-M-W 語意：每次進鎖重讀磁碟，別的行程剛寫的計數不會被蓋掉。"""
        self.tb.record_use("xp_tool")
        state = self._read_state()
        state["xp_tool"]["daily"] = 41  # 模擬另一行程已把計數推到 41
        with open(self.tb._state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
        self.tb.record_use("xp_tool")
        self.assertEqual(self._read_state()["xp_tool"]["daily"], 42)

    def test_record_use_serializes_on_cross_process_lock(self):
        """寫入必須排在 sibling .lock 的 fcntl 排他鎖後面（flock 以 open file
        description 為單位，執行緒間行為等同跨行程）。"""
        self.tb.record_use("xp_tool")
        lock_path = self.tb._state_path() + ".lock"
        self.assertTrue(os.path.exists(lock_path),
                        "record_use 應該建立 locked_json 的 .lock sentinel")
        holder = open(lock_path, "w")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        done = threading.Event()

        def worker():
            self.tb.record_use("xp_tool")
            done.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            self.assertFalse(
                done.wait(0.3),
                "外部持鎖時 record_use 不該完成（沒有等鎖＝沒有跨行程序列化）")
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        self.assertTrue(done.wait(5), "釋放鎖後 record_use 應完成")
        t.join(timeout=5)
        self.assertEqual(self._read_state()["xp_tool"]["daily"], 2)

    def test_no_fixed_tmp_file_left_behind(self):
        """舊版固定 <path>.tmp 是兩行程互踩的元兇 — 新寫法不該再產生它。"""
        self.tb.record_use("xp_tool")
        self.assertFalse(os.path.exists(self.tb._state_path() + ".tmp"))

    def test_corrupt_state_file_does_not_crash_record_use(self):
        with open(self.tb._state_path(), "w", encoding="utf-8") as f:
            f.write("{corrupt json!!!")
        self.tb.record_use("xp_tool")  # 不炸；locked_json warning + default
        self.assertEqual(self._read_state()["xp_tool"]["daily"], 1)

    def test_reset_budget_removes_only_target_tool(self):
        os.environ["RED_BUDGET_XP_TOOL2_DAILY"] = "100"
        self.addCleanup(os.environ.pop, "RED_BUDGET_XP_TOOL2_DAILY", None)
        self.tb.record_use("xp_tool")
        self.tb.record_use("xp_tool2")
        self.assertTrue(self.tb.reset_budget("xp_tool"))
        state = self._read_state()
        self.assertNotIn("xp_tool", state)
        self.assertEqual(state["xp_tool2"]["daily"], 1)
        self.assertFalse(self.tb.reset_budget("xp_tool"))  # 已不存在


# ────────────────────────────────────────────────────────────────────
# #6 — google_auth token.json
# ────────────────────────────────────────────────────────────────────
class GoogleAuthTokenFileTests(unittest.TestCase):
    def setUp(self):
        from agent_core import google_auth as ga
        self.ga = ga
        self.tmp = tempfile.mkdtemp(prefix="red_ga_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.token_path = os.path.join(self.tmp, "token.json")
        for p in (
            mock.patch.object(ga, "TOKEN_FILE", self.token_path),
            mock.patch.object(
                ga, "get_secret",
                return_value=types.SimpleNamespace(value=None)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_corrupt_token_file_degrades_to_no_token(self):
        """torn/損毀 token.json → 當作無 token 走授權流程，而不是在讀取端
        炸 JSON 錯誤（艦隊每次啟動都炸）。CREDENTIALS_FILE 不存在時應走到
        FileNotFoundError（授權流程入口），證明損毀已被吞掉降級。"""
        with open(self.token_path, "w", encoding="utf-8") as f:
            f.write("NOT JSON {{{ torn write")
        with mock.patch.object(self.ga, "CREDENTIALS_FILE",
                               os.path.join(self.tmp, "missing_creds.json")):
            with self.assertRaises(FileNotFoundError):
                self.ga.get_google_credentials()

    def test_token_refresh_writes_atomically(self):
        fake_creds = types.SimpleNamespace(
            valid=False, expired=True, refresh_token="r",
            to_json=lambda: '{"written": true}')

        class FakeCredentials:
            @staticmethod
            def from_authorized_user_info(info, scopes):
                return fake_creds

        with mock.patch.object(
                self.ga, "get_secret",
                return_value=types.SimpleNamespace(value='{"tok": 1}')), \
             mock.patch.object(self.ga, "_get_google_oauth_classes",
                               return_value=(FakeCredentials, mock.Mock())), \
             mock.patch.object(self.ga, "_refresh_creds_with_retry",
                               return_value=True), \
             mock.patch.object(self.ga, "_atomic_write_text",
                               wraps=self.ga._atomic_write_text) as aw:
            out = self.ga.get_google_credentials()
        self.assertIs(out, fake_creds)
        aw.assert_called_once_with(self.token_path, '{"written": true}')
        with open(self.token_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"written": true}')


# ────────────────────────────────────────────────────────────────────
# #7 — progress_manager 不吃 import 快照
# ────────────────────────────────────────────────────────────────────
class ProgressManagerTests(unittest.TestCase):
    def setUp(self):
        from agent_core import progress_manager as pm
        self.pm = pm
        self.tmp = tempfile.mkdtemp(prefix="red_progress_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_file = os.path.join(self.tmp, "progress.json")

    def _mgr(self):
        mgr = self.pm.ProgressManager()
        mgr._data_file = self.data_file
        return mgr

    def test_two_instances_see_each_others_writes(self):
        """跨行程 proxy：兩個獨立 instance（= 兩個行程）不共享記憶體快照，
        彼此的更新都要落地可見。舊版 import 快照 + 整檔覆寫會互相蓋掉。"""
        a, b = self._mgr(), self._mgr()
        a.update_sample_progress("S1", "cutting", "in_progress")
        b.update_sample_progress("S2", "sewing", "completed")
        samples = self._mgr().get_all_samples_progress()
        self.assertIn("S1", samples)
        self.assertIn("S2", samples, "第二個 instance 的寫入不可蓋掉第一個")

    def test_reads_are_fresh_not_import_snapshot(self):
        mgr = self._mgr()
        mgr.update_sample_progress("S1", "cutting", "in_progress")
        with open(self.data_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["samples"]["EXTERNAL"] = {"stages": {}, "last_updated": "2026-01-01T00:00:00"}
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.assertIn("EXTERNAL", mgr.get_all_samples_progress())

    def test_corrupt_file_does_not_crash_update(self):
        with open(self.data_file, "w", encoding="utf-8") as f:
            f.write("{{{corrupt")
        mgr = self._mgr()
        mgr.update_sample_progress("S1", "cutting", "done")
        self.assertIn("S1", mgr.get_all_samples_progress())


# ────────────────────────────────────────────────────────────────────
# #8 — mistake_ledger
# ────────────────────────────────────────────────────────────────────
class MistakeLedgerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        from agent_core import mistake_ledger as ml
        self.ml = ml
        self.tmp = tempfile.mkdtemp(prefix="red_ledger_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger_path = os.path.join(self.tmp, "mistakes.json")
        p = mock.patch.object(ml, "MISTAKES_FILE", self.ledger_path)
        p.start()
        self.addCleanup(p.stop)
        # 重置模組級快照狀態（並在測後還原乾淨狀態，避免洩漏到其他測試）
        self._reset_snapshot()
        self.addCleanup(self._reset_snapshot)

    def _reset_snapshot(self):
        self.ml._mistake_ledger = {"corrections": {}, "log": []}
        self.ml._ledger_loaded = False
        self.ml._ledger_mtime = -1.0

    def _read_file(self) -> dict:
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_writer_merges_with_external_writes(self):
        """lost-update 回歸：本行程快照過期時，寫入仍以磁碟新鮮狀態為底。
        舊版「快照 mutate + 整檔覆寫」會把別的行程剛寫的紀錄整個蓋掉。"""
        self.ml._log_mistake("t1", "a", "d1")
        # 模擬另一行程 append 一筆（不經本行程的記憶體快照）
        data = self._read_file()
        data["log"].append({"time": "2026-07-11 00:00:00", "type": "t2",
                            "user_said": "b", "detail": "d2", "resolution": ""})
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.ml._log_mistake("t3", "c", "d3")
        types_on_disk = [e["type"] for e in self._read_file()["log"]]
        self.assertEqual(types_on_disk, ["t1", "t2", "t3"],
                         "外部行程的 t2 不可被本行程的寫入蓋掉")

    def test_reader_reloads_on_mtime_change(self):
        self.ml.correct_mistake("雞蛋", "GitHub")
        # 模擬另一行程改檔（加一條規則），並確保 mtime 前進
        data = self._read_file()
        data["corrections"]["工作靴"] = "工作鞋"
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        st = os.stat(self.ledger_path)
        os.utime(self.ledger_path, (st.st_atime, st.st_mtime + 5))
        out = self.ml.list_mistakes()
        self.assertIn("工作靴", out, "mtime 變了應重載，不能一直用舊快照")

    def test_delete_correction_sees_external_rule(self):
        # 規則只存在磁碟（別的行程寫的），本行程快照沒有 → 仍要刪得到
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump({"corrections": {"舊詞": "新詞"}, "log": []}, f,
                      ensure_ascii=False)
        out = self.ml.delete_correction("舊詞")
        self.assertIn("已刪除規則", out)
        self.assertNotIn("舊詞", self._read_file()["corrections"])


# ────────────────────────────────────────────────────────────────────
# #9 — task_memory date-only deadline
# ────────────────────────────────────────────────────────────────────
class TaskDeadlineCmpTests(unittest.TestCase):
    def setUp(self):
        from agent_core import task_memory as tm
        self.tm = tm
        self.tmp = tempfile.mkdtemp(prefix="red_tasks_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for p in (
            mock.patch.object(tm, "_TASK_FILE",
                              os.path.join(self.tmp, "task_memory.json")),
            mock.patch.object(tm, "_pg_task_store", lambda: None),
        ):
            p.start()
            self.addCleanup(p.stop)
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        data = {"version": 1, "tasks": [
            {"id": "t_today", "status": "pending", "deadline": today,
             "title": "due today", "priority": 3},
            {"id": "t_yesterday", "status": "pending", "deadline": yesterday,
             "title": "late", "priority": 3},
        ]}
        with open(tm._TASK_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_tasks_overdue_excludes_date_only_today(self):
        """date-only 截止日=當天結束才逾期；舊版裸字串比較讓「今天截止」
        的 task 從 00:00 起就被列為逾期。"""
        out = self.tm.tasks_overdue()
        self.assertIn("t_yesterday", out)
        self.assertNotIn("t_today", out)

    def test_task_summary_counts_today_as_due_today_not_overdue(self):
        s = self.tm.task_summary()
        self.assertEqual(s["overdue"], 1)
        self.assertEqual(s["due_today"], 1)


# ────────────────────────────────────────────────────────────────────
# #10 — run_history index 修剪走 .lock
# ────────────────────────────────────────────────────────────────────
class RunHistoryIndexLockTests(unittest.TestCase):
    def setUp(self):
        from agent_core import run_history as rh
        self.rh = rh
        self.tmp = tempfile.mkdtemp(prefix="red_runs_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.index = os.path.join(self.tmp, "index.jsonl")
        for p in (
            mock.patch.object(rh, "RUNS_DIR", self.tmp),
            mock.patch.object(rh, "RUNS_INDEX", self.index),
            mock.patch.object(rh, "SCREENSHOTS_DIR",
                              os.path.join(self.tmp, "screenshots")),
            mock.patch.object(rh, "_pg_run_store", lambda: None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _record(self, i: int) -> dict:
        return {"id": f"r{i}", "tool": "x", "started_at": "2026-07-11T00:00:00",
                "status": "success", "elapsed_sec": 0.1, "result": "ok"}

    def test_trim_keeps_only_recent_lines(self):
        with mock.patch.object(self.rh, "_MAX_INDEX_BYTES", 1), \
             mock.patch.object(self.rh, "_MAX_INDEX_LINES", 2):
            for i in range(5):
                self.rh._write_record(self._record(i))
        with open(self.index, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertLessEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[-1])["id"], "r4")
        for ln in lines:
            json.loads(ln)  # 每行都要是完整 JSON

    def test_write_record_waits_on_external_flock(self):
        """append + 修剪在 sibling .lock 排他鎖下 — 外部持鎖時必須等待，
        否則修剪的整檔重寫會吃掉並發 append 的行。"""
        self.rh._write_record(self._record(0))
        lock_path = self.index + ".lock"
        self.assertTrue(os.path.exists(lock_path))
        holder = open(lock_path, "w")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        done = threading.Event()

        def worker():
            self.rh._write_record(self._record(1))
            done.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            self.assertFalse(done.wait(0.3),
                             "外部持鎖時 _write_record 不該完成")
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        self.assertTrue(done.wait(5))
        t.join(timeout=5)
        with open(self.index, "r", encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 2)


# ────────────────────────────────────────────────────────────────────
# #11 — tool_tiers fail-closed、失敗不快取
# ────────────────────────────────────────────────────────────────────
class ToolTierFailClosedTests(unittest.TestCase):
    def setUp(self):
        from agent_core import tool_tiers as tt
        self.tt = tt
        tt._sensitive_tools_frozen.cache_clear()
        self.addCleanup(tt._sensitive_tools_frozen.cache_clear)

    def test_import_failure_is_fail_closed_and_not_cached(self):
        """tg_auth import 失敗：① 回 CONFIRM（fail-closed，不是舊版的 SAFE
        fail-open）；② 失敗不進 cache — import 恢復後同一工具名要回正常值。"""
        with mock.patch.dict(sys.modules, {"agent_core.tg_auth": None}):
            self.assertEqual(self.tt.get_tier("zz_not_a_real_tool"),
                             self.tt.TIER_CONFIRM)
        # import 窗口過了 → 不再被永久固化，回正常 SAFE default
        self.assertEqual(self.tt.get_tier("zz_not_a_real_tool"),
                         self.tt.TIER_SAFE)

    def test_sensitive_fallback_still_confirm(self):
        from agent_core.tg_auth import _SENSITIVE_TOOLS
        candidates = [n for n in _SENSITIVE_TOOLS
                      if self.tt._static_tier(n) is None]
        if not candidates:
            self.skipTest("所有 sensitive 工具都有 override/pattern tier")
        self.assertEqual(self.tt.get_tier(candidates[0]),
                         self.tt.TIER_CONFIRM)

    def test_overrides_and_patterns_unchanged(self):
        self.assertEqual(self.tt.get_tier("delete_calendar_event"),
                         self.tt.TIER_DANGEROUS)
        self.assertEqual(self.tt.get_tier(""), self.tt.TIER_SAFE)


# ────────────────────────────────────────────────────────────────────
# #13 — intent_router rotation size 閘門
# ────────────────────────────────────────────────────────────────────
class IntentLogRotationTests(unittest.TestCase):
    def setUp(self):
        from agent_core import intent_router as ir
        self.ir = ir
        self.tmp = tempfile.mkdtemp(prefix="red_intent_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = os.path.join(self.tmp, "intent_log.jsonl")
        for p in (
            mock.patch.object(ir, "_LOG_FILE", self.log),
            mock.patch.object(ir, "STATE_DIR", self.tmp),
            mock.patch.object(ir, "_pg_intent_store", lambda: None),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _result(self):
        return self.ir.IntentResult(intent="chat", confidence=0.9,
                                    method="heuristic")

    def _line_count(self) -> int:
        with open(self.log, "r", encoding="utf-8") as f:
            return len([ln for ln in f.read().splitlines() if ln.strip()])

    def test_no_trim_below_byte_threshold(self):
        """size 閘門：檔案沒超過 byte 門檻就不做 readlines/trim —— 即使行數
        已超過 _LOG_MAX_LINES（舊版每則訊息都全檔 readlines 數行數）。"""
        with mock.patch.object(self.ir, "_LOG_ROTATE_BYTES", 10 ** 9), \
             mock.patch.object(self.ir, "_LOG_MAX_LINES", 1):
            for i in range(5):
                self.ir._log_classification(f"msg {i}", self._result())
        self.assertEqual(self._line_count(), 5)

    def test_trim_once_over_byte_threshold(self):
        with mock.patch.object(self.ir, "_LOG_ROTATE_BYTES", 1), \
             mock.patch.object(self.ir, "_LOG_MAX_LINES", 3):
            for i in range(6):
                self.ir._log_classification(f"msg {i}", self._result())
        self.assertLessEqual(self._line_count(), 3)
        with open(self.log, "r", encoding="utf-8") as f:
            last = json.loads(f.read().splitlines()[-1])
        self.assertEqual(last["text_preview"], "msg 5")


# ────────────────────────────────────────────────────────────────────
# #14 — query_expansion 快取清掃 + 上限
# ────────────────────────────────────────────────────────────────────
class QueryExpansionCacheTests(unittest.TestCase):
    def setUp(self):
        from agent_core import query_expansion as qe
        self.qe = qe
        qe._expand_cache.clear()
        self.addCleanup(qe._expand_cache.clear)
        resp = types.SimpleNamespace(
            text='{"original": "q", "expanded": ["synonym-a", "synonym-b"]}')
        p = mock.patch.object(qe, "_gemini_generate", return_value=resp)
        p.start()
        self.addCleanup(p.stop)

    def test_expired_entries_swept_on_write(self):
        import time
        stale_ts = time.time() - self.qe._CACHE_TTL_SEC - 100
        for i in range(3):
            self.qe._expand_cache[f"old-{i}"] = (f"old-{i}", stale_ts)
        self.qe.expand_query("工作靴 PFAS")
        for i in range(3):
            self.assertNotIn(f"old-{i}", self.qe._expand_cache,
                             "過期項應在寫入時被順手清掉")
        self.assertIn("工作靴 PFAS", self.qe._expand_cache)

    def test_cache_capped_fifo(self):
        import time
        now = time.time()
        with mock.patch.object(self.qe, "_CACHE_MAX_ENTRIES", 5):
            for i in range(5):
                self.qe._expand_cache[f"fresh-{i}"] = (f"fresh-{i}", now)
            self.qe.expand_query("新的查詢字串")
        self.assertLessEqual(len(self.qe._expand_cache), 5)
        self.assertNotIn("fresh-0", self.qe._expand_cache,
                         "超過上限應 FIFO 淘汰最舊的")
        self.assertIn("新的查詢字串", self.qe._expand_cache)


# ────────────────────────────────────────────────────────────────────
# #15 — _atomic_write_bytes fsync
# ────────────────────────────────────────────────────────────────────
class AtomicWriteFsyncTests(unittest.TestCase):
    def test_fsync_called_before_replace(self):
        from agent_core.logging_and_paths import _atomic_write_text
        tmp = tempfile.mkdtemp(prefix="red_fsync_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "state.json")
        real_fsync = os.fsync
        calls: list[int] = []

        def spy(fd):
            calls.append(fd)
            return real_fsync(fd)

        with mock.patch("os.fsync", side_effect=spy):
            _atomic_write_text(path, '{"ok": 1}')
        self.assertTrue(calls, "os.replace 前必須 fsync（斷電保護）")
        with open(path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"ok": 1}')


# ────────────────────────────────────────────────────────────────────
# #16 — status_center._rag_summary
# ────────────────────────────────────────────────────────────────────
class RagSummaryImportTests(unittest.TestCase):
    def test_uses_get_memory_collection(self):
        from agent_core import status_center as sc
        fake_col = mock.Mock()
        fake_col.count.return_value = 1234
        with mock.patch("agent_core.memory._get_memory_collection",
                        return_value=fake_col):
            out = sc._rag_summary()
        self.assertEqual(out, {"chroma_doc_count": 1234},
                         "舊版 import 不存在的 _get_collection → 永遠 -1")


# ────────────────────────────────────────────────────────────────────
# #17 — RPC fallback 只認連線層失敗
# ────────────────────────────────────────────────────────────────────
class RpcFallbackGatingTests(unittest.TestCase):
    def _request(self):
        from agent_core.tool_rpc_protocol import make_request
        return make_request("system_alerts", {}, timeout_sec=5)

    def test_connect_refused_marks_unreachable(self):
        from agent_core import tool_rpc_client as trc
        tmp = tempfile.mkdtemp(prefix="red_rpc_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        missing = os.path.join(tmp, "no_server.sock")
        with mock.patch.object(trc, "SOCKET_PATH", missing):
            resp = trc.call_rpc(self._request())
        self.assertFalse(resp["transport_ok"])
        self.assertTrue(resp.get("rpc_unreachable"),
                        "連不上 server（request 從未送達）→ 可安全重跑")

    def test_server_closing_after_connect_is_not_unreachable(self):
        """連上後 server 沒回有效回應（worker 可能已開跑）→ 不可標 unreachable。"""
        from agent_core import tool_rpc_client as trc
        tmp = tempfile.mkdtemp(prefix="red_rpc_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sock_path = os.path.join(tmp, "half_dead.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)
        self.addCleanup(server.close)

        def accept_and_drop():
            conn, _ = server.accept()
            conn.recv(65536)  # 收下 request 後直接斷線（不回應）
            conn.close()

        t = threading.Thread(target=accept_and_drop, daemon=True)
        t.start()
        with mock.patch.object(trc, "SOCKET_PATH", sock_path):
            resp = trc.call_rpc(self._request())
        t.join(timeout=5)
        self.assertFalse(resp["transport_ok"])
        self.assertFalse(bool(resp.get("rpc_unreachable")),
                         "request 已抵達 server → 禁止 fallback 重跑")

    def _make_response(self, request, **kw):
        from agent_core.tool_rpc_protocol import make_response
        return make_response(request, **kw)

    def test_tool_runner_does_not_rerun_after_worker_timeout(self):
        """server 轉發的 worker timeout（transport_ok=False 但 request 已執行過）
        原樣返回 — fallback 直跑會讓 telegram_send_file 這類副作用重複執行。"""
        from agent_core import tool_runner
        req = self._request()
        timeout_resp = self._make_response(
            req, transport_ok=False, text="❌ tool worker timeout after 5s",
            error_code="timeout", recoverable=True)
        with mock.patch("agent_core.tool_rpc_client.call_rpc",
                        return_value=timeout_resp), \
             mock.patch("agent_core.tool_worker_exec.run_worker_subprocess") as direct:
            result = tool_runner.call_tool("system_alerts", {}, timeout_sec=5)
        direct.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "timeout")

    def test_tool_runner_falls_back_when_unreachable(self):
        from agent_core import tool_runner
        req = self._request()
        unreachable = self._make_response(
            req, transport_ok=False, text="❌ tool RPC unavailable",
            error_code="network", recoverable=True)
        unreachable["rpc_unreachable"] = True
        direct_ok = self._make_response(
            req, transport_ok=True, tool_ok=True, text="done")
        with mock.patch("agent_core.tool_rpc_client.call_rpc",
                        return_value=unreachable), \
             mock.patch("agent_core.tool_worker_exec.run_worker_subprocess",
                        return_value=direct_ok) as direct:
            result = tool_runner.call_tool("system_alerts", {}, timeout_sec=5)
        direct.assert_called_once()
        self.assertTrue(result.ok)
        self.assertEqual(str(result), "done")  # ToolResult 是 str subclass

    def test_tool_runner_respects_fallback_disabled(self):
        from agent_core import tool_runner
        req = self._request()
        unreachable = self._make_response(
            req, transport_ok=False, text="❌ tool RPC unavailable",
            error_code="network", recoverable=True)
        unreachable["rpc_unreachable"] = True
        with mock.patch("agent_core.tool_rpc_client.call_rpc",
                        return_value=unreachable), \
             mock.patch("agent_core.tool_worker_exec.run_worker_subprocess") as direct:
            result = tool_runner.call_tool(
                "system_alerts", {}, timeout_sec=5, fallback_direct=False)
        direct.assert_not_called()
        self.assertFalse(result.ok)


# ────────────────────────────────────────────────────────────────────
# #18 — worker 回應序列化 default=str
# ────────────────────────────────────────────────────────────────────
class WorkerResponseSerializationTests(unittest.TestCase):
    def test_non_json_serializable_data_survives(self):
        from agent_core.tool_worker_exec import _dump_response
        from agent_core.tool_rpc_protocol import make_request, make_response
        resp = make_response(
            make_request("x_tool", {}),
            transport_ok=True, tool_ok=True, text="done",
            data={"when": datetime(2026, 7, 11, 8, 0, 0),
                  "path": Path("/tmp/x.xlsx")},
        )
        # 舊版（無 default）直接 TypeError → 成功執行的工具被判 transport 失敗
        with self.assertRaises(TypeError):
            json.dumps(resp, ensure_ascii=False)
        out = json.loads(_dump_response(resp))
        self.assertTrue(out["tool_ok"])
        self.assertEqual(out["text"], "done")
        self.assertIn("2026-07-11", out["data"]["when"])
        self.assertEqual(out["data"]["path"], "/tmp/x.xlsx")


if __name__ == "__main__":
    unittest.main()
