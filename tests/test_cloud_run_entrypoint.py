import os
import sys
import types
import unittest
from unittest import mock


class CloudRunEntrypointTests(unittest.TestCase):
    def test_task_role_dispatches_agent_daemon_task(self):
        from agent_core import cloud_run_entrypoint

        fake_daemon = types.SimpleNamespace(main=mock.Mock())
        with mock.patch.dict(os.environ, {"RED_DAEMON_TASK": "health_check"}, clear=True), \
             mock.patch.dict(sys.modules, {"agent_daemon": fake_daemon}):
            cloud_run_entrypoint.main(["task"])

        fake_daemon.main.assert_called_once_with()
        self.assertEqual(sys.argv, ["agent_daemon.py", "--task", "health_check"])

    def test_unknown_role_exits(self):
        from agent_core import cloud_run_entrypoint

        with mock.patch.dict(os.environ, {"RED_CLOUD_ROLE": "nope"}, clear=True):
            with self.assertRaises(SystemExit):
                cloud_run_entrypoint.main([])


if __name__ == "__main__":
    unittest.main()
