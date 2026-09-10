"""_check_daemons_health 記憶體超標的修復策略。

2026-07-23/24 事故：chroma 在 rag_sync 連續同步下工作集合法超過 6144 MB，
health_check 每 30 分鐘 unload/load 重啟一次（26 小時內 16 次），冷 cache +
斬斷進行中 upsert 反而把 rag_sync 拖到撞 wall-clock 上限。修法：
chroma 記憶體超標改「只告警不重啟」（_HEALTH_MEM_WARN_ONLY），
其他 daemon 維持 launchctl_restart 自動修復。
"""
import unittest
from unittest import mock

from agent_core import health


def _fake_subprocess_run(labels_with_pids, rss_kb):
    """回傳假 subprocess.run：launchctl list 給定 label/PID，ps 回固定 RSS。"""

    def _run(cmd, **kwargs):
        result = mock.Mock()
        result.returncode = 0
        if cmd[-1] == "list":
            lines = [
                f"{pid}\t0\t{label}" for label, pid in labels_with_pids.items()
            ]
            result.stdout = "\n".join(lines) + "\n"
        elif cmd[0] == "ps":
            result.stdout = f"{rss_kb}\n"
        else:
            result.stdout = ""
        result.stderr = ""
        return result

    return _run


class HealthRssWarnOnlyTests(unittest.TestCase):
    def _issues_with_rss_mb(self, rss_mb):
        labels_with_pids = {
            "com.xiaohong.chroma": "111",
            "com.xiaohong.telegram": "222",
        }
        fake_run = _fake_subprocess_run(labels_with_pids, rss_kb=rss_mb * 1024)
        # 這個 fake 只列 2 個 label 卻讓所有 plist 都「存在」，所以其餘 label
        # 一律走「沒載入」路徑 —— 那條路徑會為了濾掉部署空窗誤判而重採一次
        # （見 test_health_launchd_repair）。這裡測的是記憶體策略，不是重採，
        # 把間隔歸零免得每個 case 白等兩秒。
        with mock.patch.object(health, "_is_macos_edge_host", return_value=True), \
                mock.patch.object(health, "_HEALTH_LAUNCHD_RECHECK_DELAY_SEC", 0), \
                mock.patch.object(health.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(health.os.path, "exists", return_value=True):
            return health._check_daemons_health()

    def test_chroma_memory_high_is_warn_only(self):
        issues = self._issues_with_rss_mb(20480)  # 超過所有 daemon 上限
        by_area = {i["area"]: i for i in issues}

        chroma = by_area["daemon/com.xiaohong.chroma"]
        self.assertIn("記憶體過高", chroma["msg"])
        self.assertNotIn("repair", chroma, "chroma 超標不得觸發自動重啟")

        telegram = by_area["daemon/com.xiaohong.telegram"]
        self.assertEqual(telegram.get("repair"), "launchctl_restart")
        self.assertEqual(telegram.get("label"), "com.xiaohong.telegram")

    def test_chroma_under_limit_no_issue(self):
        issues = self._issues_with_rss_mb(9000)  # < 10240 不告警
        areas = {i["area"] for i in issues}
        self.assertNotIn("daemon/com.xiaohong.chroma", areas)

    def test_chroma_limit_raised_to_10240(self):
        self.assertEqual(
            health._HEALTH_RSS_LIMIT_MB["com.xiaohong.chroma"], 10240,
            "6144 已證實太緊（合法工作集 6.4–8.6 GB），不要改回去",
        )


if __name__ == "__main__":
    unittest.main()
