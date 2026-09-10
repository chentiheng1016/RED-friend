import os
import socket
import sys
import unittest
from unittest import mock

os.environ.setdefault("AGENT_DAEMON_MODE", "1")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import health


class HealthNetworkTests(unittest.TestCase):
    def test_check_daemons_health_reports_launchctl_nonzero(self):
        with mock.patch.object(health.platform, "system", return_value="Darwin"), \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(
                 health.subprocess,
                 "run",
                 return_value=mock.Mock(
                     returncode=1,
                     stdout="",
                     stderr="Operation not permitted",
                 ),
             ):
            issues = health._check_daemons_health()

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["area"], "launchctl")
        self.assertIn("exit 1", issues[0]["msg"])
        self.assertIn("Operation not permitted", issues[0]["msg"])

    def test_check_daemons_health_skips_non_macos(self):
        with mock.patch.object(health.platform, "system", return_value="Linux"), \
             mock.patch.object(health.subprocess, "run") as run:
            issues = health._check_daemons_health()

        self.assertEqual(issues, [])
        run.assert_not_called()

    def test_check_daemons_health_skips_cloud_runtime(self):
        with mock.patch.object(health.platform, "system", return_value="Linux"), \
             mock.patch.dict(os.environ, {"RED_CLOUD_MODE": "1"}, clear=True), \
             mock.patch.object(health.subprocess, "run") as run:
            issues = health._check_daemons_health()

        self.assertEqual(issues, [])
        run.assert_not_called()

    def test_check_disk_skips_cloud_runtime(self):
        with mock.patch.dict(os.environ, {"RED_CLOUD_MODE": "1"}, clear=True), \
             mock.patch.object(health.shutil, "disk_usage") as disk_usage:
            issues = health._check_disk()

        self.assertEqual(issues, [])
        disk_usage.assert_not_called()

    def test_check_network_groups_dns_failures_into_one_repair(self):
        def fake_resolve(host):
            if host == "www.googleapis.com":
                return "142.250.198.74"
            raise socket.gaierror(f"{host} down")

        with mock.patch.object(health.socket, "gethostbyname", side_effect=fake_resolve):
            issues = health._check_network()

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["area"], "network/dns")
        self.assertEqual(issues[0]["repair"], "flush_dns_cache")
        self.assertEqual(issues[0]["hosts"], list(health._HEALTH_NETWORK_HOSTS))
        self.assertIn("api.telegram.org", issues[0]["msg"])
        self.assertIn("generativelanguage.googleapis.com", issues[0]["msg"])
        self.assertNotIn("www.googleapis.com down", issues[0]["msg"])

    def test_flush_dns_cache_retries_and_reports_success(self):
        run_calls = []

        def fake_run(cmd, **_kwargs):
            run_calls.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(health.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(health.time, "sleep") as sleep, \
             mock.patch.object(health.socket, "gethostbyname", return_value="1.2.3.4") as resolve:
            result = health._repair_flush_dns_cache(["api.telegram.org"])

        self.assertTrue(result.startswith("✅"))
        self.assertEqual(run_calls[0], ["dscacheutil", "-flushcache"])
        self.assertEqual(run_calls[1], ["killall", "-HUP", "mDNSResponder"])
        sleep.assert_called_once_with(health._HEALTH_DNS_RETRY_DELAY_SEC)
        resolve.assert_called_once_with("api.telegram.org")

    def test_flush_dns_cache_reports_remaining_failure(self):
        def fake_run(_cmd, **_kwargs):
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(health.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(health.time, "sleep"), \
             mock.patch.object(health.socket, "gethostbyname", side_effect=socket.gaierror("still down")):
            result = health._repair_flush_dns_cache(["api.telegram.org"])

        self.assertTrue(result.startswith("❌"))
        self.assertIn("api.telegram.org", result)
        self.assertIn("still down", result)


if __name__ == "__main__":
    unittest.main()
