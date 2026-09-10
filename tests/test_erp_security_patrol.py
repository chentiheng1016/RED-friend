"""ERP RDP 安全巡檢（agent_core/erp_security_patrol）+ dashboard_alerts 紅線測試。

不連 ERP 主機：SSH 執行層 mock 掉，驗腳本組裝/傳遞方式、JSON 解析+淨化、旗標裁決、
落盤、以及「巡檢失敗永不拖垮鏡像主流程」的 run_patrol_safe 契約。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

from agent_core import dashboard_alerts
from agent_core import erp_security_patrol as patrol
from tests.awake_clock_isolation import AwakeClockIsolationMixin


def _canned_remote(**overrides) -> dict:
    """遠端 PowerShell 統計的標準回傳（PS 端欄位縮寫：u/ip/t/d/n）。"""
    obj = {
        "ok": 1, "host": "FUCHUN-ERP", "window_h": 24,
        "fail_total": 1234, "fail_truncated": False,
        "real_fail_total": 0, "success_scan_truncated": False,
        "top_src_ips": [{"ip": "203.0.113.9", "n": 800}],
        "top_accounts": [{"u": "administrator", "n": 900}],
        "real_accounts": [],
        "real_account_pairs": [],
        "rdp_logons": [],
    }
    obj.update(overrides)
    return obj


class PsScriptTests(unittest.TestCase):
    def test_script_covers_event_ids_and_params(self):
        s = patrol._build_ps_script(24, 20, 50000)
        self.assertIn("Id=4625", s)
        self.assertIn("EventID=4624", s)          # 4624 走 XPath 直濾 LogonType=10
        self.assertIn('Data[@Name="LogonType"]', s)
        self.assertIn("$w=24", s)
        self.assertIn("$m=50000", s)
        self.assertIn("ConvertTo-Json", s)
        # 真實帳號 SubStatus 清單要進腳本（密碼錯=帳號存在的關鍵訊號）
        self.assertIn("0xC000006A", s)
        self.assertNotIn("0xC0000064", s)          # 帳號不存在 — 刻意排除

    def test_event_query_failure_not_swallowed(self):
        # Codex P1：查詢失敗（權限/壞 XPath）不能被吞成「0 筆=乾淨」假陰性——
        # 兩個 Get-WinEvent 都要 -ErrorAction Stop + try/catch，只放行「查無事件」。
        s = patrol._build_ps_script(24, 20, 50000)
        self.assertEqual(s.count("-ErrorAction Stop"), 2)
        self.assertNotIn("-ErrorAction SilentlyContinue", s)
        self.assertEqual(s.count("NoMatchingEventsFound"), 2)
        self.assertIn("if($err){$ok=0}", s)

    def test_params_must_be_int(self):
        with self.assertRaises((TypeError, ValueError)):
            patrol._build_ps_script("24; rm -rf /", 20, 50000)

    def test_try_and_catch_share_one_line(self):
        # 腳本走 stdin 餵 `powershell -Command -`：catch 落到下一行會被當孤兒 catch
        # parse error（try 先自成一句執行掉）。每個 try 後面必須緊接 catch。
        s = patrol._build_ps_script(24, 20, 50000)
        for ln in s.splitlines():
            if "try{" in ln:
                self.assertIn("}catch{", ln, f"try/catch 被換行拆開：{ln}")
        self.assertEqual(s.count("try{"), 2)


class ParseReportTests(unittest.TestCase):
    def _stdout(self, obj) -> str:
        return "some banner noise\r\n" + json.dumps(obj, ensure_ascii=False) + "\n"

    def test_parse_normalizes_fields(self):
        obj = _canned_remote(
            # 單元素集合：PS 版本差可能不包 array — 要被正規化成 list
            top_src_ips={"ip": "203.0.113.9", "n": 800},
            rdp_logons=[{"t": "2026-07-19 01:00:00", "u": "erpadmin",
                         "d": "FUCHUN", "ip": "198.51.100.7"}],
        )
        rep = patrol._parse_report(self._stdout(obj))
        self.assertEqual(rep["top_src_ips"], [{"ip": "203.0.113.9", "n": 800}])
        self.assertEqual(rep["fail_total"], 1234)
        self.assertEqual(rep["rdp_logons"][0]["account"], "erpadmin")
        self.assertEqual(rep["rdp_logons"][0]["ip"], "198.51.100.7")
        self.assertTrue(rep["generated_at"])

    def test_every_string_field_passes_sanitize_for_llm(self):
        obj = _canned_remote(
            rdp_logons=[{"t": "2026-07-19 01:00:00", "u": "evil",
                         "d": "FUCHUN", "ip": "198.51.100.7"}],
            real_accounts=[{"u": "erpadmin", "n": 9}],
        )
        with mock.patch.object(patrol, "sanitize_for_llm",
                               side_effect=lambda s: "SAN:" + s):
            rep = patrol._parse_report(self._stdout(obj))
        self.assertTrue(rep["host"].startswith("SAN:"))
        self.assertTrue(rep["rdp_logons"][0]["account"].startswith("SAN:"))
        self.assertTrue(rep["real_accounts"][0]["account"].startswith("SAN:"))

    def test_attacker_controlled_strings_are_truncated(self):
        obj = _canned_remote(
            top_accounts=[{"u": "A" * 5000, "n": 3}],
        )
        rep = patrol._parse_report(self._stdout(obj))
        self.assertLessEqual(len(rep["top_accounts"][0]["account"]),
                             patrol._MAX_FIELD_LEN + 10)

    def test_no_json_raises(self):
        with self.assertRaises(patrol.ErpSecurityPatrolError):
            patrol._parse_report("'powershell' is not recognized...\n")

    def test_missing_ok_marker_raises(self):
        with self.assertRaises(patrol.ErpSecurityPatrolError):
            patrol._parse_report('{"foo": 1}\n')

    def test_remote_query_failure_raises_with_error(self):
        # 遠端回 ok=0（Get-WinEvent 權限不足等）→ raise 並帶出 PowerShell 錯誤訊息
        stdout = '{"ok": 0, "err": "Attempted to perform an unauthorized operation."}\n'
        with self.assertRaises(patrol.ErpSecurityPatrolError) as ctx:
            patrol._parse_report(stdout)
        self.assertIn("unauthorized", str(ctx.exception))

    def test_script_goes_through_stdin_not_command_line(self):
        # 腳本一旦回到命令列（尤其 -EncodedCommand base64），阿里雲雲安全中心會把巡檢
        # 判成「蠕蟲病毒命令」每晚誤報——命令列必須乾淨、腳本走 stdin。
        proc = mock.Mock(returncode=0, stdout='{"ok":1}\n', stderr="")
        with mock.patch.object(patrol, "_ssh_cfg",
                               return_value={"key": os.devnull, "user": "u", "host": "h"}), \
             mock.patch.object(patrol.subprocess, "run", return_value=proc) as run:
            patrol._run_remote_powershell("Write-Output hi", timeout_s=30)
        argv, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertEqual(kwargs["input"], "Write-Output hi")
        self.assertIn("-Command -", argv[-1])
        joined = " ".join(argv)
        self.assertNotIn("-EncodedCommand", joined)
        self.assertNotIn("Write-Output hi", joined)
        self.assertNotIn("-n", argv)  # -n 會把 stdin 導向 /dev/null，腳本就送不過去

    def test_ssh_failure_with_empty_exception_message(self):
        # gemini review：str(e) 空字串時 splitlines()[0] 會 IndexError 蓋掉原始錯誤
        with mock.patch.object(patrol, "_ssh_cfg",
                               return_value={"key": os.devnull, "user": "u", "host": "h"}), \
             mock.patch.object(patrol.subprocess, "run", side_effect=OSError("")):
            with self.assertRaises(patrol.ErpSecurityPatrolError) as ctx:
                patrol._run_remote_powershell("Write-Output hi", timeout_s=30)
        self.assertIn("OSError", str(ctx.exception))


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("RED_ERP_RDP_ALLOWED_IPS", "RED_ERP_RDP_REAL_FAIL_WARN",
                  "RED_ERP_RDP_REAL_FAIL_CRIT"):
            os.environ.pop(k, None)

    def _report(self, logons=(), real=()):
        return {"rdp_logons": list(logons), "real_accounts": list(real)}

    def test_unknown_ip_flagged(self):
        rep = self._report(logons=[{"time": "t", "account": "x", "ip": "198.51.100.7"}])
        flags = patrol._evaluate(rep)
        self.assertEqual(len(flags["success_unknown_ip"]), 1)
        self.assertEqual(flags["level_success"], "crit")

    def test_localhost_always_allowed_even_with_env_whitelist(self):
        # SSH tunnel 內的 RDP 看起來是 loopback = 被授權路徑；env 只「加白」不覆蓋
        os.environ["RED_ERP_RDP_ALLOWED_IPS"] = "203.0.113.50"
        rep = self._report(logons=[
            {"ip": "127.0.0.1"}, {"ip": "::1"}, {"ip": "203.0.113.50"},
            {"ip": "-"}, {"ip": ""},           # 無網路來源 — 不當外連跡象
        ])
        flags = patrol._evaluate(rep)
        self.assertEqual(flags["success_unknown_ip"], [])
        self.assertEqual(flags["level_success"], "")

    def test_dense_real_account_warn_then_crit(self):
        flags = patrol._evaluate(self._report(real=[{"account": "erpadmin", "n": 5}]))
        self.assertEqual(flags["level_bruteforce"], "warn")
        flags = patrol._evaluate(self._report(real=[{"account": "erpadmin", "n": 50}]))
        self.assertEqual(flags["level_bruteforce"], "crit")

    def test_sparse_real_account_silent(self):
        # 正常人打錯密碼 1-4 次不告警（預設 warn 門檻 5）
        flags = patrol._evaluate(self._report(real=[{"account": "erpadmin", "n": 4}]))
        self.assertEqual(flags["real_account_bruteforce"], [])
        self.assertEqual(flags["level_bruteforce"], "")

    def test_thresholds_env_tunable(self):
        os.environ["RED_ERP_RDP_REAL_FAIL_WARN"] = "10"
        flags = patrol._evaluate(self._report(real=[{"account": "erpadmin", "n": 9}]))
        self.assertEqual(flags["real_account_bruteforce"], [])


class RunPatrolTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.latest = os.path.join(self.dir, "rdp_audit_latest.json")
        self.history = os.path.join(self.dir, "rdp_audit_history.jsonl")
        for name, val in (("_REPORT_DIR", self.dir), ("REPORT_PATH", self.latest),
                          ("_HISTORY_PATH", self.history)):
            p = mock.patch.object(patrol, name, val)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"RED_ERP_ORACLE_ENABLED": "1"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("RED_ERP_SEC_AUDIT_ENABLED", None)
        os.environ.pop("RED_ERP_RDP_ALLOWED_IPS", None)

    def test_end_to_end_writes_report_with_flags(self):
        remote = json.dumps(_canned_remote(
            real_fail_total=9,
            real_accounts=[{"u": "erpadmin", "n": 9}],
            rdp_logons=[{"t": "2026-07-19 01:00:00", "u": "erpadmin",
                         "d": "FUCHUN", "ip": "198.51.100.7"}],
        )) + "\n"
        with mock.patch.object(patrol, "_run_remote_powershell", return_value=remote):
            rep = patrol.run_patrol(log=lambda m: None)
        self.assertEqual(rep["flags"]["level_success"], "crit")
        self.assertEqual(rep["flags"]["level_bruteforce"], "warn")
        with open(self.latest, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["flags"]["level_success"], "crit")
        with open(self.history, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 1)

    def test_disabled_by_oracle_env_skips_without_ssh(self):
        os.environ["RED_ERP_ORACLE_ENABLED"] = "0"
        with mock.patch.object(patrol, "_run_remote_powershell") as run:
            out = patrol.run_patrol(log=lambda m: None)
        self.assertIn("skipped", out)
        run.assert_not_called()

    def test_disabled_by_audit_switch(self):
        os.environ["RED_ERP_SEC_AUDIT_ENABLED"] = "0"
        with mock.patch.object(patrol, "_run_remote_powershell") as run:
            out = patrol.run_patrol(log=lambda m: None)
        self.assertIn("skipped", out)
        run.assert_not_called()

    def test_run_patrol_safe_never_raises(self):
        with mock.patch.object(patrol, "run_patrol", side_effect=RuntimeError("ssh 斷線")):
            out = patrol.run_patrol_safe(log=lambda m: None)
        self.assertFalse(out.get("ok", True))
        self.assertIn("ssh 斷線", out.get("error", ""))

    def test_run_patrol_safe_survives_broken_log_callback(self):
        def bad_log(_msg):
            raise OSError("stdout gone")
        with mock.patch.object(patrol, "run_patrol", side_effect=RuntimeError("x")):
            out = patrol.run_patrol_safe(log=bad_log)
        self.assertFalse(out.get("ok", True))


class MirrorScriptIntegrationTests(unittest.TestCase):
    """erp_mirror_refresh._refresh：巡檢炸掉（連 run_patrol_safe 契約都被打破時）
    也不影響鏡像結果。"""

    def _load_script(self):
        import importlib.util
        path = os.path.join(_REPO_ROOT, "launchd", "scripts", "erp_mirror_refresh.py")
        spec = importlib.util.spec_from_file_location("erp_mirror_refresh_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_patrol_failure_does_not_break_refresh(self):
        mod = self._load_script()
        res = {"done": 22, "count_mismatch": 0, "errors": 0, "total": 22}
        with mock.patch.object(mod, "is_enabled", return_value=True), \
             mock.patch.object(mod, "refresh_hot", return_value=res), \
             mock.patch.object(mod, "run_patrol_safe",
                               side_effect=RuntimeError("巡檢大爆炸")):
            out = mod._refresh()
        self.assertEqual(out, res)

    def test_patrol_runs_after_successful_refresh(self):
        mod = self._load_script()
        res = {"done": 22, "count_mismatch": 0, "errors": 0, "total": 22}
        called = []
        with mock.patch.object(mod, "is_enabled", return_value=True), \
             mock.patch.object(mod, "refresh_hot", return_value=res), \
             mock.patch.object(mod, "run_patrol_safe",
                               side_effect=lambda **kw: called.append(1) or {}):
            mod._refresh()
        self.assertEqual(called, [1])

    def test_skipped_refresh_skips_patrol(self):
        mod = self._load_script()
        with mock.patch.object(mod, "is_enabled", return_value=False), \
             mock.patch.object(mod, "run_patrol_safe") as pat:
            out = mod._refresh()
        self.assertEqual(out, {"skipped": True})
        pat.assert_not_called()


class ErpRdpSecurityAlertCheckTests(AwakeClockIsolationMixin, unittest.TestCase):
    """dashboard_alerts._check_erp_rdp_security：讀巡檢報告旗標轉 alert。"""

    def setUp(self):
        # 報告 age 走 _observed_age_h（>48h 靜默）。tick 檔不隔離的話，live
        # var/state 的空窗會把 50h 扣到 48h 以下 → 該靜默的反而報出來。
        self._awake_clock_iso_setup()
        self.addCleanup(self._awake_clock_iso_teardown)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _write(self, report: dict) -> str:
        path = os.path.join(self.dir, "rdp_audit_latest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False)
        return path

    def _report(self, hours_ago=1.0, **flags):
        base_flags = {"success_unknown_ip": [], "real_account_bruteforce": [],
                      "level_success": "", "level_bruteforce": ""}
        base_flags.update(flags)
        return {
            "generated_at": (datetime.now()
                             - timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
            "window_hours": 24,
            "real_fail_total": 9,
            "flags": base_flags,
        }

    def test_registered_in_all_checks(self):
        self.assertIn(dashboard_alerts._check_erp_rdp_security,
                      dashboard_alerts._ALL_CHECKS)

    def test_missing_report_silent(self):
        self.assertEqual(
            dashboard_alerts._check_erp_rdp_security("/nonexistent/report.json"), [])

    def test_clean_report_silent(self):
        p = self._write(self._report())
        self.assertEqual(dashboard_alerts._check_erp_rdp_security(p), [])

    def test_unknown_ip_logon_is_crit(self):
        p = self._write(self._report(
            success_unknown_ip=[{"time": "2026-07-19 01:00:00", "account": "erpadmin",
                                 "ip": "198.51.100.7"}],
            level_success="crit",
        ))
        alerts = dashboard_alerts._check_erp_rdp_security(p)
        self.assertEqual([a["id"] for a in alerts], ["erp_rdp_logon_unknown_ip"])
        self.assertEqual(alerts[0]["level"], "crit")
        self.assertIn("erpadmin@198.51.100.7", alerts[0]["detail"])

    def test_real_account_bruteforce_levels(self):
        p = self._write(self._report(
            real_account_bruteforce=[{"account": "erpadmin", "n": 12}],
            level_bruteforce="warn",
        ))
        alerts = dashboard_alerts._check_erp_rdp_security(p)
        self.assertEqual([a["id"] for a in alerts], ["erp_rdp_real_account_bruteforce"])
        self.assertEqual(alerts[0]["level"], "warn")
        p2 = self._write(self._report(
            real_account_bruteforce=[{"account": "erpadmin", "n": 99}],
            level_bruteforce="crit",
        ))
        self.assertEqual(dashboard_alerts._check_erp_rdp_security(p2)[0]["level"], "crit")

    def test_bogus_level_falls_back_to_warn(self):
        p = self._write(self._report(
            real_account_bruteforce=[{"account": "erpadmin", "n": 12}],
            level_bruteforce="banana",
        ))
        self.assertEqual(dashboard_alerts._check_erp_rdp_security(p)[0]["level"], "warn")

    def test_both_flags_two_alerts(self):
        p = self._write(self._report(
            success_unknown_ip=[{"time": "t", "account": "a", "ip": "198.51.100.7"}],
            level_success="crit",
            real_account_bruteforce=[{"account": "erpadmin", "n": 12}],
            level_bruteforce="warn",
        ))
        ids = [a["id"] for a in dashboard_alerts._check_erp_rdp_security(p)]
        self.assertEqual(sorted(ids),
                         ["erp_rdp_logon_unknown_ip", "erp_rdp_real_account_bruteforce"])

    def test_stale_report_silent(self):
        # 巡檢停跑 >48h：別拿舊事件永久掛警（巡檢失敗自己會出現在 daemon log）
        p = self._write(self._report(
            hours_ago=50,
            success_unknown_ip=[{"time": "t", "account": "a", "ip": "198.51.100.7"}],
            level_success="crit",
        ))
        self.assertEqual(dashboard_alerts._check_erp_rdp_security(p), [])

    def test_corrupt_report_silent(self):
        path = os.path.join(self.dir, "rdp_audit_latest.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(dashboard_alerts._check_erp_rdp_security(path), [])

    def test_missing_generated_at_silent(self):
        rep = self._report()
        rep.pop("generated_at")
        p = self._write(rep)
        self.assertEqual(dashboard_alerts._check_erp_rdp_security(p), [])


if __name__ == "__main__":
    unittest.main()
