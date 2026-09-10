"""2026-09-07 事故 regression：DarkWake catch-up 撞上 DNS 未恢復。

闔蓋睡眠中的 DarkWake（每次只醒 ~45 秒）撞上 dispatcher catch-up 輪，四個任務
同一秒拋 [Errno 8]；mark_dispatcher_task_failed 蓋掉 last_run_at → 每日任務整天
不再重試（UserA/UserM 早報、匯率推播漏發一天）＋失敗告警假警報。

修法＝task_dispatcher 的網路前置閘門：有任務到期先 DNS 探測，不通就整輪延後、
完全不動任務狀態（不 mark、不存檔、force flag 不清），下一輪網路回來自然補跑。
"""
import socket
import threading
import unittest
from unittest import mock

from agent_core import daemon_dispatcher as dd


def _task(name, **extra):
    base = {"name": name, "prompt": "p", "interval_minutes": 60,
            "start_hour": 0, "end_hour": 24}
    base.update(extra)
    return base


class DispatcherNetworkGateTests(unittest.TestCase):
    def _run(self, tasks, *, network_up, should_run=lambda t, now: True):
        calls = {"probe": 0, "run": [], "saves": 0, "notified": []}

        def probe():
            calls["probe"] += 1
            return network_up

        def save(data):
            calls["saves"] += 1
            return True

        def run_one(task):
            calls["run"].append(task["name"])
            return f"result-{task['name']}"

        dd.task_dispatcher(
            load_daemon_tasks=lambda: {"tasks": tasks},
            save_daemon_tasks=save,
            should_run_task_fn=should_run,
            run_one_dispatcher_task_fn=run_one,
            mark_dispatcher_task_failed_fn=dd.mark_dispatcher_task_failed,
            mark_dispatcher_task_succeeded_fn=dd.mark_dispatcher_task_succeeded,
            dispatcher_result_is_empty_fn=dd.dispatcher_result_is_empty,
            remember_dispatcher_result_fn=dd.remember_dispatcher_result,
            notify_dispatcher_result_fn=lambda t, r: calls["notified"].append(t["name"]),
            network_is_up_fn=probe,
        )
        return calls

    def test_network_down_defers_whole_round_without_touching_state(self):
        # 事故核心：斷網時什麼都不能動 —— 一 mark 就蓋 last_run_at，每日任務
        # 整天不再重試；一記 last_error 就觸發假警報。
        tasks = [_task("daily_a"), _task("forced_b", next_force_run=True)]
        calls = self._run(tasks, network_up=False)

        self.assertEqual(calls["probe"], 1)
        self.assertEqual(calls["run"], [])
        self.assertEqual(calls["saves"], 0)
        self.assertEqual(calls["notified"], [])
        for task in tasks:
            self.assertNotIn("last_run_at", task)
            self.assertNotIn("last_error", task)
        # 手動 force-run 也要撐過延後，網路回來那輪照樣觸發
        self.assertTrue(tasks[1]["next_force_run"])

    def test_network_up_runs_normally(self):
        tasks = [_task("a"), _task("b")]
        calls = self._run(tasks, network_up=True)

        self.assertEqual(calls["run"], ["a", "b"])
        self.assertEqual(calls["notified"], ["a", "b"])
        for task in tasks:
            self.assertEqual(task["run_count"], 1)
            self.assertIsNone(task["last_error"])

    def test_probe_skipped_when_nothing_due(self):
        # 沒任務到期就不解析 —— dispatcher 每 ~5 分鐘 tick 一次，別白打 DNS。
        calls = self._run([_task("a")], network_up=False,
                          should_run=lambda t, now: False)
        self.assertEqual(calls["probe"], 0)
        self.assertEqual(calls["run"], [])


class DispatcherNetworkProbeTests(unittest.TestCase):
    def test_probe_false_when_all_hosts_unresolvable(self):
        with mock.patch.object(
            dd.socket, "getaddrinfo",
            side_effect=socket.gaierror(8, "nodename nor servname provided"),
        ) as gai:
            self.assertFalse(dd.dispatcher_network_is_up())
        self.assertEqual(gai.call_count, len(dd._NET_PROBE_HOSTS))

    def test_probe_true_when_any_host_resolves(self):
        # 第一個主機掛、第二個活 → 網路算活著（單一域名失敗不該擋整輪，
        # 要讓任務照跑、走既有失敗路徑留下真錯誤）。
        results = [socket.gaierror(8, "boom"), [("stub",)]]

        def gai(host, *args, **kwargs):
            outcome = results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch.object(dd.socket, "getaddrinfo", side_effect=gai):
            self.assertTrue(dd.dispatcher_network_is_up())

    def test_probe_false_when_resolver_hangs(self):
        # 解析卡住不回（半醒的 resolver）也算網路不通。gate 在 with 區塊內
        # set，讓 worker 在 patch 還在時收尾，不把 fake 漏出測試邊界。
        gate = threading.Event()

        def hanging_gai(*args, **kwargs):
            gate.wait(5)
            raise socket.gaierror(8, "boom")

        with mock.patch.object(dd.socket, "getaddrinfo", side_effect=hanging_gai), \
                mock.patch.object(dd, "_DISPATCHER_NET_PROBE_TIMEOUT_S", 0.2):
            self.assertFalse(dd.dispatcher_network_is_up())
            gate.set()


if __name__ == "__main__":
    unittest.main()
