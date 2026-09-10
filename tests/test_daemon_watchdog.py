"""Tests for the daemon liveness / stall watchdog (agent_core.daemon_watchdog).

跑的是 unittest discover（不是 pytest）→ 隔離一律寫 setUp/tearDown。不 hardcode
/Users/user/RED/，repo root 從 __file__ 推導；所有檔案落在 per-test 的
tmpdir，所有 launchctl / rag-active / restart 都用注入 seam mock，不碰真實系統。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import daemon_watchdog as wd  # noqa: E402

_NOW = 1_000_000.0  # fixed clock for deterministic age math


class _WatchdogTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wd_test_")
        self.state_dir = os.path.join(self.tmp, "state")
        self.log_dir = os.path.join(self.tmp, "logs")
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        # 看門狗 env 預設值不可被外部環境污染 — 每個 test 自帶乾淨的一組。
        self._env_patch = mock.patch.dict(os.environ, {
            "RED_WATCHDOG_ENABLE": "1",
            "RED_WATCHDOG_TG_STALL_S": "1200",
            "RED_WATCHDOG_RAG_STALL_S": "1800",
            "RED_WATCHDOG_TG_AUTORESTART": "1",
            "RED_WATCHDOG_RESTART_COOLDOWN_S": "900",
            "RED_WATCHDOG_TG_GRACE_S": "0",
        }, clear=False)
        self._env_patch.start()
        # 清掉可能殘留、會影響 telegram_heartbeat_key() 的 suffix。
        os.environ.pop("RED_TELEGRAM_STATE_SUFFIX", None)

    def tearDown(self):
        self._env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _read_json(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _write_hb(self, key, *, state, ts, task="t", pid=None):
        """寫測試用 heartbeat。pid：模擬寫入者 pid（production 由
        write_telegram_heartbeat 自動寫 os.getpid()；測試要模擬「hb 是
        launchctl 現任 process 寫的」就把 pid 設成 launchctl 表裡的值，
        pid="omit" 模擬沒有 pid 欄位的舊格式檔）。"""
        wd.write_telegram_heartbeat(
            key, state=state, task=task, state_dir=self.state_dir, now=ts,
        )
        if pid is not None:
            path = wd.telegram_heartbeat_path(key, state_dir=self.state_dir)
            data = self._read_json(path)
            if pid == "omit":
                data.pop("pid", None)
            else:
                data["pid"] = pid
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)

    def _write_rag_log(self, *, mtime, basename=None):
        path = os.path.join(self.log_dir, basename or wd._RAG_LOG_BASENAME)
        with open(path, "w", encoding="utf-8") as f:
            f.write("[rag_sync] ...\n")
        os.utime(path, (mtime, mtime))
        return path

    def _write_rag_heartbeat(self, *, mtime):
        path = wd.rag_heartbeat_path(state_dir=self.state_dir)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        os.utime(path, (mtime, mtime))
        return path


# ────────────────────────────────────────────────────────────────────
# key ↔ label mapping + heartbeat writer
# ────────────────────────────────────────────────────────────────────
class KeyLabelTests(_WatchdogTestBase):
    def test_label_to_key(self):
        self.assertEqual(wd._label_to_key("com.xiaohong.telegram"), "red")
        self.assertEqual(wd._label_to_key("com.xiaohong.telegram_green"), "green")

    def test_key_to_label(self):
        self.assertEqual(wd._key_to_label("red"), "com.xiaohong.telegram")
        self.assertEqual(wd._key_to_label("green"), "com.xiaohong.telegram_green")

    def test_heartbeat_key_from_env(self):
        self.assertEqual(wd.telegram_heartbeat_key(), "red")  # no suffix
        with mock.patch.dict(os.environ, {"RED_TELEGRAM_STATE_SUFFIX": "Blue"}):
            self.assertEqual(wd.telegram_heartbeat_key(), "blue")

    def test_is_telegram_label(self):
        self.assertTrue(wd._is_telegram_label("com.xiaohong.telegram"))
        self.assertTrue(wd._is_telegram_label("com.xiaohong.telegram_yellow"))
        self.assertFalse(wd._is_telegram_label("com.xiaohong.rag_sync_daily"))
        self.assertFalse(wd._is_telegram_label("com.xiaohong.alert_check"))


class HeartbeatWriterTests(_WatchdogTestBase):
    def test_begin_writes_active(self):
        hb = wd.TelegramHeartbeat(key="red", state_dir=self.state_dir)
        hb.begin("msg#1")
        data = self._read_json(wd.telegram_heartbeat_path("red", state_dir=self.state_dir))
        self.assertEqual(data["state"], "active")
        self.assertEqual(data["task"], "msg#1")
        self.assertEqual(data["label"], "com.xiaohong.telegram")

    def test_idle_writes_idle(self):
        hb = wd.TelegramHeartbeat(key="red", state_dir=self.state_dir)
        hb.begin("x")
        hb.idle()
        data = self._read_json(wd.telegram_heartbeat_path("red", state_dir=self.state_dir))
        self.assertEqual(data["state"], "idle")

    def test_pulse_noop_while_idle(self):
        hb = wd.TelegramHeartbeat(key="red", state_dir=self.state_dir, min_pulse_interval_s=0.0)
        hb.idle()
        path = wd.telegram_heartbeat_path("red", state_dir=self.state_dir)
        ts_before = self._read_json(path)["ts"]
        time.sleep(0.02)
        hb.pulse()  # idle → must not rewrite
        self.assertEqual(self._read_json(path)["ts"], ts_before)

    def test_pulse_refreshes_ts_while_active(self):
        hb = wd.TelegramHeartbeat(key="red", state_dir=self.state_dir, min_pulse_interval_s=0.0)
        hb.begin("x")
        path = wd.telegram_heartbeat_path("red", state_dir=self.state_dir)
        ts_before = self._read_json(path)["ts"]
        time.sleep(0.02)
        hb.pulse()
        self.assertGreater(self._read_json(path)["ts"], ts_before)

    def test_pulse_throttled(self):
        hb = wd.TelegramHeartbeat(key="red", state_dir=self.state_dir, min_pulse_interval_s=1000.0)
        hb.begin("x")
        path = wd.telegram_heartbeat_path("red", state_dir=self.state_dir)
        ts_before = self._read_json(path)["ts"]
        time.sleep(0.02)
        hb.pulse()  # within throttle window → skipped
        self.assertEqual(self._read_json(path)["ts"], ts_before)


# ────────────────────────────────────────────────────────────────────
# detect_stalls — telegram
# ────────────────────────────────────────────────────────────────────
class DetectTelegramTests(_WatchdogTestBase):
    def _detect(self, launchctl):
        return wd.detect_stalls(
            now=_NOW, state_dir=self.state_dir, log_dir=self.log_dir,
            launchctl_list_fn=lambda: launchctl,
            rag_active_fn=lambda: False,
        )

    def test_active_and_stale_flags_with_pid(self):
        # 需求測試 1：heartbeat 過期（active）+ process 還活著 → 告警 + 帶 pid
        # （hb 的 pid == launchctl 現任 pid，代表真的是這個 process 卡死）
        self._write_hb("red", state="active", ts=_NOW - 9999, pid=12345)
        findings = self._detect({"com.xiaohong.telegram": "12345"})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "telegram")
        self.assertEqual(findings[0]["label"], "com.xiaohong.telegram")
        self.assertEqual(findings[0]["pid"], 12345)
        self.assertGreater(findings[0]["age_s"], 1200)

    def test_idle_never_flagged(self):
        # 需求測試 2：idle 無 active task → 不誤觸發（即使 ts 很舊）
        self._write_hb("red", state="idle", ts=_NOW - 9999)
        self.assertEqual(self._detect({"com.xiaohong.telegram": "12345"}), [])

    def test_active_fresh_not_flagged(self):
        # pid 相符（現任 process 自己的 hb）+ ts 新鮮 → 走年齡判定、不告警
        self._write_hb("red", state="active", ts=_NOW - 5, pid=12345)
        self.assertEqual(self._detect({"com.xiaohong.telegram": "12345"}), [])

    def test_dead_pid_not_flagged(self):
        # pid='-'（剛崩潰/重啟中）是 _check_daemon_health 的事，不是「活著卻卡死」
        self._write_hb("red", state="active", ts=_NOW - 9999)
        self.assertEqual(self._detect({"com.xiaohong.telegram": None}), [])

    def test_no_heartbeat_file_not_flagged(self):
        self.assertEqual(self._detect({"com.xiaohong.telegram": "12345"}), [])

    def test_color_bot_keyed_separately(self):
        self._write_hb("green", state="active", ts=_NOW - 9999, pid=222)
        # red 沒 heartbeat、green 有 → 只 green 命中
        findings = self._detect({
            "com.xiaohong.telegram": "111",
            "com.xiaohong.telegram_green": "222",
        })
        self.assertEqual([f["label"] for f in findings], ["com.xiaohong.telegram_green"])
        self.assertEqual(findings[0]["pid"], 222)

    def test_stale_heartbeat_from_previous_pid_not_flagged(self):
        # 殘檔防誤殺（健檢 High）：上一個 process 在 active 中途被殺，殘留的
        # active heartbeat 的 pid ≠ launchctl 現任 pid → 不能當「現任卡死」
        # 的證據，否則看門狗每 15 分鐘誤殺剛重啟的健康 bot。
        self._write_hb("red", state="active", ts=_NOW - 9999, pid=11111)
        self.assertEqual(self._detect({"com.xiaohong.telegram": "22222"}), [])

    def test_legacy_heartbeat_without_pid_still_flagged(self):
        # 沒 pid 欄位的舊格式檔無從比對 → 維持原判定（照樣告警）
        self._write_hb("red", state="active", ts=_NOW - 9999, pid="omit")
        findings = self._detect({"com.xiaohong.telegram": "12345"})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["pid"], 12345)

    def test_garbage_pid_field_still_flagged(self):
        # pid 欄位壞掉（非數字）→ 無從比對，維持原判定
        self._write_hb("red", state="active", ts=_NOW - 9999, pid="not-a-pid")
        findings = self._detect({"com.xiaohong.telegram": "12345"})
        self.assertEqual(len(findings), 1)


# ────────────────────────────────────────────────────────────────────
# detect_stalls — rag_sync
# ────────────────────────────────────────────────────────────────────
class DetectRagTests(_WatchdogTestBase):
    def _detect(self, *, rag_active):
        return wd.detect_stalls(
            now=_NOW, state_dir=self.state_dir, log_dir=self.log_dir,
            launchctl_list_fn=lambda: {},
            rag_active_fn=lambda: rag_active,
        )

    def test_active_and_log_stale_flags(self):
        self._write_rag_log(mtime=_NOW - 9999)
        findings = self._detect(rag_active=True)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "rag")
        self.assertEqual(findings[0]["label"], wd.RAG_LABEL)

    def test_fresh_log_not_flagged(self):
        # 需求測試 3：rag log mtime 新鮮 → 不誤觸發
        self._write_rag_log(mtime=_NOW - 60)
        self.assertEqual(self._detect(rag_active=True), [])

    def test_inactive_not_flagged(self):
        # log 很舊但根本沒在跑 → 不是 stall
        self._write_rag_log(mtime=_NOW - 9999)
        self.assertEqual(self._detect(rag_active=False), [])

    def test_active_but_no_log_not_flagged(self):
        self.assertEqual(self._detect(rag_active=True), [])

    def test_manual_run_fresh_log_not_flagged(self):
        # 誤報情境（2026-07-04 事故）：daemon log 停滯數小時（該輪早已結束），
        # 手動 RAG_SYNC_FORCE=1 補跑活躍、寫自己的 manual log → 不告警
        self._write_rag_log(mtime=_NOW - 9999)
        self._write_rag_log(mtime=_NOW - 60, basename="rag_sync_manual_20260704.log")
        self.assertEqual(self._detect(rag_active=True), [])

    def test_all_logs_stale_flags_freshest(self):
        # 真卡死情境：daemon + manual log 全停滯 + process 在跑 → 告警，
        # age/log_path 以最新那份（manual）為準
        self._write_rag_log(mtime=_NOW - 99999)
        manual = self._write_rag_log(
            mtime=_NOW - 9999, basename="rag_sync_manual_20260704.log")
        findings = self._detect(rag_active=True)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "rag")
        self.assertEqual(findings[0]["log_path"], manual)
        self.assertAlmostEqual(findings[0]["age_s"], 9999.0, delta=1.0)

    def test_manual_log_only_stale_flags(self):
        # daemon log 不存在（rotate 後）、只有停滯的 manual log → 照樣告警
        self._write_rag_log(mtime=_NOW - 9999, basename="rag_sync_manual.log")
        findings = self._detect(rag_active=True)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "rag")

    def test_stale_manual_leftover_does_not_mask_daemon_stall(self):
        # 幾天前補跑殘留的舊 manual log 不影響判定（取 max mtime）：
        # daemon 停滯照報、且 log_path 指 daemon log
        self._write_rag_log(mtime=_NOW - 3000)
        self._write_rag_log(mtime=_NOW - 999999, basename="rag_sync_manual_20260630.log")
        findings = self._detect(rag_active=True)
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0]["log_path"].endswith(wd._RAG_LOG_BASENAME))

    def test_fresh_daemon_log_with_stale_manual_leftover_not_flagged(self):
        # 反向：daemon 正常寫 log，舊 manual 殘留不會湊出假告警
        self._write_rag_log(mtime=_NOW - 60)
        self._write_rag_log(mtime=_NOW - 999999, basename="rag_sync_manual_20260630.log")
        self.assertEqual(self._detect(rag_active=True), [])

    # ── heartbeat：drive 逐檔階段 progress log 稀疏，靠每檔心跳補 ──
    # （2026-07-15 誤報：一個慢 batch 讓 log 靜默 >30 分被判 wedge，但實際健康）
    def test_fresh_heartbeat_saves_stale_log(self):
        # 核心修復：progress log 停滯過門檻，但 rag 每檔跳的心跳新鮮 → 不報
        self._write_rag_log(mtime=_NOW - 9999)
        self._write_rag_heartbeat(mtime=_NOW - 60)
        self.assertEqual(self._detect(rag_active=True), [])

    def test_heartbeat_only_fresh_no_log_not_flagged(self):
        # 只有心跳（log 尚未產生 / 已 rotate）且新鮮 → 不報
        self._write_rag_heartbeat(mtime=_NOW - 60)
        self.assertEqual(self._detect(rag_active=True), [])

    def test_stale_log_and_stale_heartbeat_flags(self):
        # 真 wedge：log 與心跳同時停滯過門檻 → 照樣告警
        self._write_rag_log(mtime=_NOW - 9999)
        self._write_rag_heartbeat(mtime=_NOW - 9999)
        findings = self._detect(rag_active=True)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "rag")

    def test_heartbeat_only_stale_flags_points_at_heartbeat(self):
        # 完全沒有 log、只有停滯的心跳 → 告警，log_path 退指心跳檔
        hb = self._write_rag_heartbeat(mtime=_NOW - 9999)
        findings = self._detect(rag_active=True)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["log_path"], hb)

    def test_stale_heartbeat_does_not_mask_fresh_log(self):
        # gmail / chat 階段：drive 心跳停在該階段結束時刻（舊），但那些階段每步
        # 都印 log → log 新鮮 → 不報（log 涵蓋非 drive 逐檔階段）
        self._write_rag_log(mtime=_NOW - 60)
        self._write_rag_heartbeat(mtime=_NOW - 9999)
        self.assertEqual(self._detect(rag_active=True), [])

    def test_stale_heartbeat_leftover_does_not_mask_daemon_stall(self):
        # 對稱於 stale-manual-leftover：舊心跳殘留不遮蔽 log 判定，log 停滯照報
        self._write_rag_log(mtime=_NOW - 3000)
        self._write_rag_heartbeat(mtime=_NOW - 999999)
        findings = self._detect(rag_active=True)
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0]["log_path"].endswith(wd._RAG_LOG_BASENAME))

    def test_write_rag_heartbeat_roundtrip(self):
        # writer 寫出檔案（供 detect 讀 mtime），內容含 ts / pid 供人工 debug
        wd.write_rag_heartbeat(state_dir=self.state_dir, now=_NOW)
        path = wd.rag_heartbeat_path(state_dir=self.state_dir)
        self.assertTrue(os.path.isfile(path))
        data = self._read_json(path)
        self.assertEqual(data["ts"], _NOW)
        self.assertIn("pid", data)


# ────────────────────────────────────────────────────────────────────
# restart_telegram_bot
# ────────────────────────────────────────────────────────────────────
class RestartTests(_WatchdogTestBase):
    def test_sigterm_then_kickstart_k(self):
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(wd.os, "kill") as mkill, \
                mock.patch.object(wd.subprocess, "run", return_value=fake) as mrun:
            ok, detail = wd.restart_telegram_bot("com.xiaohong.telegram", 4321, grace_s=0)
        self.assertTrue(ok)
        mkill.assert_called_once()
        self.assertEqual(mkill.call_args.args[0], 4321)
        # kickstart -k gui/<uid>/<label>
        argv = mrun.call_args.args[0]
        self.assertIn("kickstart", argv)
        self.assertIn("-k", argv)
        self.assertEqual(argv[-1], f"gui/{os.getuid()}/com.xiaohong.telegram")

    def test_dead_pid_does_not_blow_up_the_tick(self):
        """已死的 pid 丟 ProcessLookupError（OSError 子類）—— 舊版只接
        (ValueError, TypeError)，這行會炸穿 run_watchdog（那裡沒有 try），
        整輪結束，同一輪其他真的卡死的 bot 一個都不會被救。"""
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(wd.os, "kill",
                               side_effect=ProcessLookupError(3, "No such process")), \
                mock.patch.object(wd.subprocess, "run", return_value=fake):
            ok, detail = wd.restart_telegram_bot("com.xiaohong.telegram", 4321, grace_s=0)
        self.assertTrue(ok)          # kickstart 照跑，bot 照樣被拉回來
        self.assertIn("kickstart", detail)

    def test_foreign_pid_permission_error_does_not_blow_up(self):
        """pid 被回收給別的使用者的 process → PermissionError，同樣不可炸穿。"""
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(wd.os, "kill",
                               side_effect=PermissionError(1, "Operation not permitted")), \
                mock.patch.object(wd.subprocess, "run", return_value=fake):
            ok, _ = wd.restart_telegram_bot("com.xiaohong.telegram", 4321, grace_s=0)
        self.assertTrue(ok)

    def test_kickstart_failure_reported(self):
        fake = mock.Mock(returncode=1, stdout="", stderr="No such process")
        with mock.patch.object(wd.os, "kill"), \
                mock.patch.object(wd.subprocess, "run", return_value=fake):
            ok, detail = wd.restart_telegram_bot("com.xiaohong.telegram", 1, grace_s=0)
        self.assertFalse(ok)
        self.assertIn("No such process", detail)


class CurrentPidsTests(_WatchdogTestBase):
    """讀取失敗 vs 一個都沒載入，動作端必須分得開。"""

    def test_failure_returns_none_not_empty(self):
        with mock.patch.object(wd.subprocess, "run",
                               side_effect=OSError("launchctl missing")):
            self.assertIsNone(wd._current_pids())
        with mock.patch.object(wd.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="")):
            self.assertIsNone(wd._current_pids())

    def test_empty_listing_is_an_empty_map_not_none(self):
        with mock.patch.object(wd.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="")):
            self.assertEqual(wd._current_pids(), {})

    def test_detection_side_still_degrades_to_empty_map(self):
        """偵測端拿 {} 是安全的（沒東西可判 → 不動作），維持原行為。"""
        with mock.patch.object(wd, "_current_pids", return_value=None):
            self.assertEqual(wd._launchctl_list(), {})


# ────────────────────────────────────────────────────────────────────
# run_watchdog orchestration
# ────────────────────────────────────────────────────────────────────
class RunWatchdogTests(_WatchdogTestBase):
    def _tg_finding(self, label="com.xiaohong.telegram", pid=123):
        return {"kind": "telegram", "label": label, "pid": pid, "key": "red",
                "age_s": 1500.0, "threshold_s": 1200, "task": "msg#1"}

    def _pids_unchanged(self, label="com.xiaohong.telegram", pid="123"):
        """動手前重採：pid 跟 detect 當下一樣 → 該動就動（正常路徑）。"""
        return lambda: {label: pid}

    def test_restarts_active_stall_and_notifies(self):
        restart = mock.Mock(return_value=(True, "ok"))
        notify = mock.Mock(return_value=True)
        res = wd.run_watchdog(
            now=_NOW, state_dir=self.state_dir,
            detect_fn=lambda: [self._tg_finding()],
            restart_fn=restart, notify_fn=notify,
            current_pids_fn=self._pids_unchanged(),
        )
        self.assertEqual(res["restarted"], ["com.xiaohong.telegram"])
        restart.assert_called_once_with("com.xiaohong.telegram", 123)
        notify.assert_called_once()
        # cooldown 持久化
        st = self._read_json(wd._watchdog_state_path(self.state_dir))
        self.assertIn("com.xiaohong.telegram", st)
        self.assertEqual(st["com.xiaohong.telegram"]["count"], 1)

    def test_cooldown_skips_second_restart(self):
        restart = mock.Mock(return_value=(True, "ok"))
        notify = mock.Mock(return_value=True)
        common = dict(
            state_dir=self.state_dir,
            detect_fn=lambda: [self._tg_finding()],
            restart_fn=restart, notify_fn=notify,
            current_pids_fn=self._pids_unchanged(),
        )
        wd.run_watchdog(now=_NOW, **common)
        res2 = wd.run_watchdog(now=_NOW + 10, **common)  # 10s < 900s cooldown
        self.assertEqual(res2["skipped"], ["com.xiaohong.telegram"])
        self.assertEqual(restart.call_count, 1)

    def test_cooldown_elapsed_allows_restart(self):
        restart = mock.Mock(return_value=(True, "ok"))
        common = dict(
            state_dir=self.state_dir,
            detect_fn=lambda: [self._tg_finding()],
            restart_fn=restart, notify_fn=mock.Mock(return_value=True),
            current_pids_fn=self._pids_unchanged(),
        )
        wd.run_watchdog(now=_NOW, **common)
        wd.run_watchdog(now=_NOW + 1000, **common)  # 1000s > 900s cooldown
        self.assertEqual(restart.call_count, 2)

    def test_failed_restart_notifies_and_records(self):
        restart = mock.Mock(return_value=(False, "boom"))
        notify = mock.Mock(return_value=True)
        res = wd.run_watchdog(
            now=_NOW, state_dir=self.state_dir,
            detect_fn=lambda: [self._tg_finding()],
            restart_fn=restart, notify_fn=notify,
            current_pids_fn=self._pids_unchanged(),
        )
        self.assertEqual(res["failed"], ["com.xiaohong.telegram"])
        notify.assert_called_once()

    def test_rag_finding_alerted_only_no_restart(self):
        restart = mock.Mock()
        notify = mock.Mock()
        res = wd.run_watchdog(
            now=_NOW, state_dir=self.state_dir,
            detect_fn=lambda: [{"kind": "rag", "label": wd.RAG_LABEL,
                                "age_s": 2000.0, "threshold_s": 1800}],
            restart_fn=restart, notify_fn=notify,
        )
        self.assertEqual(res["alerted_only"], [wd.RAG_LABEL])
        restart.assert_not_called()
        notify.assert_not_called()

    def test_autorestart_off_alerts_only(self):
        restart = mock.Mock()
        with mock.patch.dict(os.environ, {"RED_WATCHDOG_TG_AUTORESTART": "0"}):
            res = wd.run_watchdog(
                now=_NOW, state_dir=self.state_dir,
                detect_fn=lambda: [self._tg_finding()],
                restart_fn=restart, notify_fn=mock.Mock(),
            )
        self.assertEqual(res["alerted_only"], ["com.xiaohong.telegram"])
        restart.assert_not_called()

    def test_pid_changed_since_detection_is_not_touched(self):
        """🚨 2026-08-18 形狀：偵測到卡死之後、動手之前，bot 已經自己換了 process。

        再對舊 pid 送 SIGTERM ＋ kickstart -k，等於把剛起來的健康 bot 砍掉重來。
        """
        restart = mock.Mock(return_value=(True, "ok"))
        notify = mock.Mock(return_value=True)
        res = wd.run_watchdog(
            now=_NOW, state_dir=self.state_dir,
            detect_fn=lambda: [self._tg_finding(pid=123)],
            restart_fn=restart, notify_fn=notify,
            current_pids_fn=lambda: {"com.xiaohong.telegram": "456"},
        )
        self.assertEqual(res["stale"], ["com.xiaohong.telegram"])
        self.assertEqual(res["restarted"], [])
        restart.assert_not_called()
        notify.assert_not_called()
        # 沒動手就不該寫 cooldown 狀態（否則下一輪真的要救時會被 cooldown 擋掉）
        self.assertFalse(os.path.exists(wd._watchdog_state_path(self.state_dir)))

    def test_launchctl_unreadable_means_hands_off(self):
        """讀不到現況 ≠ 這個 label 不見了。讀不出來就別動手。"""
        restart = mock.Mock(return_value=(True, "ok"))
        res = wd.run_watchdog(
            now=_NOW, state_dir=self.state_dir,
            detect_fn=lambda: [self._tg_finding()],
            restart_fn=restart, notify_fn=mock.Mock(),
            current_pids_fn=lambda: None,
        )
        self.assertEqual(res["stale"], ["com.xiaohong.telegram"])
        restart.assert_not_called()

    def test_label_booted_out_is_not_resurrected(self):
        """label 整個不在 domain 裡＝有人明確 bootout，不是看門狗該蓋過去的狀況。"""
        restart = mock.Mock(return_value=(True, "ok"))
        res = wd.run_watchdog(
            now=_NOW, state_dir=self.state_dir,
            detect_fn=lambda: [self._tg_finding()],
            restart_fn=restart, notify_fn=mock.Mock(),
            current_pids_fn=lambda: {"com.xiaohong.other": "9"},
        )
        self.assertEqual(res["stale"], ["com.xiaohong.telegram"])
        restart.assert_not_called()

    def test_label_loaded_but_not_running_kickstarts_without_sigterm(self):
        """pid=- （載入但沒在跑）：舊 pid 已無意義，不送 SIGTERM，只 kickstart 拉回來。"""
        restart = mock.Mock(return_value=(True, "ok"))
        res = wd.run_watchdog(
            now=_NOW, state_dir=self.state_dir,
            detect_fn=lambda: [self._tg_finding(pid=123)],
            restart_fn=restart, notify_fn=mock.Mock(return_value=True),
            current_pids_fn=lambda: {"com.xiaohong.telegram": None},
        )
        self.assertEqual(res["restarted"], ["com.xiaohong.telegram"])
        restart.assert_called_once_with("com.xiaohong.telegram", None)

    def test_disabled_short_circuits(self):
        detect = mock.Mock()
        with mock.patch.dict(os.environ, {"RED_WATCHDOG_ENABLE": "0"}):
            res = wd.run_watchdog(now=_NOW, state_dir=self.state_dir, detect_fn=detect)
        self.assertFalse(res["enabled"])
        detect.assert_not_called()


# ────────────────────────────────────────────────────────────────────
# dashboard_alerts mapping
# ────────────────────────────────────────────────────────────────────
class DashboardMappingTests(_WatchdogTestBase):
    def test_telegram_maps_to_crit(self):
        from agent_core import dashboard_alerts
        finding = {"kind": "telegram", "label": "com.xiaohong.telegram_green",
                   "pid": 9, "age_s": 1500.0, "threshold_s": 1200, "task": "msg#3"}
        with mock.patch("agent_core.daemon_watchdog.detect_stalls", return_value=[finding]):
            alerts = dashboard_alerts._check_daemon_stalls()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "crit")
        self.assertEqual(alerts[0]["id"], "daemon_stall_com.xiaohong.telegram_green")

    def test_rag_maps_to_warn(self):
        from agent_core import dashboard_alerts
        finding = {"kind": "rag", "label": wd.RAG_LABEL,
                   "age_s": 2000.0, "threshold_s": 1800}
        with mock.patch("agent_core.daemon_watchdog.detect_stalls", return_value=[finding]):
            alerts = dashboard_alerts._check_daemon_stalls()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "warn")
        self.assertEqual(alerts[0]["id"], "daemon_stall_rag_sync")
        # 沒帶 log_path（舊版 finding）→ fallback 到 daemon log 名
        self.assertIn("daemon-rag_sync.log", alerts[0]["detail"])

    def test_rag_detail_names_freshest_log(self):
        # 手動補跑卡死時，告警文字/advice 要指向實際停滯的 manual log
        from agent_core import dashboard_alerts
        finding = {"kind": "rag", "label": wd.RAG_LABEL,
                   "age_s": 2000.0, "threshold_s": 1800,
                   "log_path": "/x/logs/rag_sync_manual_20260704.log"}
        with mock.patch("agent_core.daemon_watchdog.detect_stalls", return_value=[finding]):
            alerts = dashboard_alerts._check_daemon_stalls()
        self.assertEqual(len(alerts), 1)
        self.assertIn("rag_sync_manual_20260704.log", alerts[0]["detail"])
        self.assertIn("tail var/logs/rag_sync_manual_20260704.log", alerts[0]["advice"])
        self.assertEqual(alerts[0]["metric"]["log"], "rag_sync_manual_20260704.log")

    def test_detect_exception_swallowed(self):
        from agent_core import dashboard_alerts
        with mock.patch("agent_core.daemon_watchdog.detect_stalls",
                        side_effect=RuntimeError("boom")):
            self.assertEqual(dashboard_alerts._check_daemon_stalls(), [])


if __name__ == "__main__":
    unittest.main()
