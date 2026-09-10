from __future__ import annotations

import os
import unittest
from unittest import mock


_DB_ENV = {
    "RED_OPERATIONAL_DB_URL": "postgresql://red",
    "RED_TASK_QUEUE_BACKEND": "postgres",
}


class OperationalTaskQueueRoutingTests(unittest.TestCase):
    def test_submit_task_uses_postgres_backend_when_enabled(self):
        from agent_core import task_queue

        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_task_queue.enqueue_task",
                    return_value={"ok": True, "task_id": "db-t1"},
                ) as enqueue:
            result = task_queue.submit_task(
                "send_gmail",
                {"to": "a@b", "subject": "s", "body": "b"},
            )

        self.assertTrue(getattr(result, "ok", False), result)
        enqueue.assert_called_once()
        args, kwargs = enqueue.call_args
        self.assertEqual(args[0]["tool"], "send_gmail")
        self.assertEqual(kwargs["queue_max_size"], task_queue._QUEUE_MAX_SIZE)
        self.assertEqual(result.data["backend"], "postgres")

    def test_cancel_task_sets_local_flag_for_running_postgres_task(self):
        from agent_core import task_queue
        import threading

        flag = threading.Event()
        task_queue._running_cancel_flags["t-running"] = flag
        try:
            with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                    mock.patch(
                        "agent_core.operational_task_queue.cancel_task",
                        return_value={"ok": True, "previous_state": task_queue.STATE_RUNNING},
                    ):
                result = task_queue.cancel_task("t-running")
        finally:
            task_queue._running_cancel_flags.pop("t-running", None)

        self.assertTrue(getattr(result, "ok", False), result)
        self.assertTrue(flag.is_set())

    def test_run_worker_once_claims_and_completes_postgres_task(self):
        from agent_core import task_queue

        task = {
            "id": "t1",
            "tool": "send_gmail",
            "kwargs": {"to": "a@b", "subject": "s", "body": "b"},
            "timeout_sec": 30,
        }
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch("agent_core.operational_task_queue.reap_stale_running") as reap, \
                mock.patch("agent_core.operational_task_queue.claim_task", return_value=task) as claim, \
                mock.patch("agent_core.operational_task_queue.complete_task") as complete, \
                mock.patch.object(
                    task_queue,
                    "_execute_task_snapshot",
                    return_value=(True, "sent", ""),
                ):
            ran = task_queue.run_worker_once()

        self.assertEqual(ran, 1)
        reap.assert_called_once()
        claim.assert_called_once()
        complete.assert_called_once()
        self.assertEqual(complete.call_args.args[0], "t1")
        self.assertTrue(complete.call_args.kwargs["success"])

    def test_queue_summary_uses_postgres_backend_when_enabled(self):
        from agent_core import task_queue

        summary = {"pending": 1, "running": 0, "dlq": 0, "backend": "postgres"}
        with mock.patch.dict(os.environ, _DB_ENV, clear=True), \
                mock.patch(
                    "agent_core.operational_task_queue.queue_summary",
                    return_value=summary,
                ):
            self.assertEqual(task_queue.queue_summary(), summary)


if __name__ == "__main__":
    unittest.main()
