"""Smoke tests for agent_daemon.py task functions.

After the phase-78+ refactor, agent_daemon imports from agent_core directly
(no `import agent as A`). Tests patch function attributes on the imported
agent_daemon module rather than sys.modules-stubbing a fake `agent`.
"""
import importlib
import os
import sys
import types
import unittest
from unittest import mock


os.environ.setdefault("AGENT_DAEMON_MODE", "1")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def load_agent_daemon():
    """Fresh import of agent_daemon. No stubs needed — the daemon is
    self-contained via agent_core.* imports."""
    sys.modules.pop("agent_daemon", None)
    return importlib.import_module("agent_daemon")


class DispatcherSmokeTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_agent_daemon()

    def tearDown(self):
        sys.modules.pop("agent_daemon", None)

    def test_dispatcher_failure_persists_state(self):
        data = {
            "version": 1,
            "tasks": [{"name": "broken", "prompt": "x", "interval_minutes": 60, "start_hour": 0, "end_hour": 24}],
        }
        saved = {}

        # Dispatcher now saves via save_dispatcher_run (merge into on-disk
        # state under lock) instead of _save_daemon_tasks (raw overwrite),
        # so patch that symbol on agent_daemon. The mock captures the same
        # `data` blob the dispatcher would have merged — assertions unchanged.
        with mock.patch.object(self.mod, "_load_daemon_tasks", return_value=data), \
             mock.patch.object(self.mod, "_run_one_dispatcher_task", side_effect=RuntimeError("boom")), \
             mock.patch.object(self.mod, "_dispatcher_network_is_up", return_value=True), \
             mock.patch.object(self.mod, "save_dispatcher_run", side_effect=lambda payload: saved.setdefault("data", payload) or True):
            self.mod.task_dispatcher()

        task = saved["data"]["tasks"][0]
        self.assertEqual(task["last_error"], "boom")
        self.assertIn("last_run_at", task)

    def test_dispatcher_duplicate_result_skips_notification(self):
        dup_hash = self.mod.hashlib.sha1("same result".encode("utf-8")).hexdigest()[:16]
        data = {
            "version": 1,
            "tasks": [{
                "name": "dup",
                "prompt": "x",
                "interval_minutes": 60,
                "start_hour": 0,
                "end_hour": 24,
                "dedup_hashes": [dup_hash],
            }],
        }

        with mock.patch.object(self.mod, "_load_daemon_tasks", return_value=data), \
             mock.patch.object(self.mod, "_run_one_dispatcher_task", return_value="same result"), \
             mock.patch.object(self.mod, "_dispatcher_network_is_up", return_value=True), \
             mock.patch.object(self.mod, "save_dispatcher_run", return_value=True), \
             mock.patch.object(self.mod, "_notify_dispatcher_result") as notify:
            self.mod.task_dispatcher()

        task = data["tasks"][0]
        notify.assert_not_called()
        self.assertEqual(task["run_count"], 1)
        self.assertIsNone(task["last_error"])

    def test_run_one_dispatcher_task_uses_gemini_client(self):
        """_run_one_dispatcher_task composes a Gemini chat session and returns
        its text response. Patch the two gemini_client helpers to feed a canned
        reply without hitting the real API."""
        fake_chat = types.SimpleNamespace(
            send_message=lambda _msg: types.SimpleNamespace(text="ok")
        )
        fake_client = types.SimpleNamespace(
            chats=types.SimpleNamespace(create=lambda **kwargs: fake_chat)
        )
        fake_types = types.SimpleNamespace(
            GenerateContentConfig=lambda **kwargs: kwargs,
            AutomaticFunctionCallingConfig=lambda **kwargs: kwargs,
        )
        with mock.patch.object(self.mod, "_get_gemini_client", return_value=fake_client), \
             mock.patch.object(self.mod, "_get_genai_types", return_value=fake_types):
            result = self.mod._run_one_dispatcher_task({
                "name": "lazy", "prompt": "x",
                "interval_minutes": 60, "start_hour": 0, "end_hour": 24,
            })
        # 結果尾巴會多一段資料來源足跡（report_trail）——這裡要驗的是「有沒有
        # 組出 Gemini chat 並回傳它的文字」，不是足跡內容，所以拆掉再比。
        from agent_core.report_trail import strip_source_footer
        self.assertEqual(strip_source_footer(result), "ok")

    def test_should_run_task_honors_force_run_even_outside_schedule(self):
        now = self.mod.datetime(2026, 4, 19, 23, 0, 0)
        task = {
            "enabled": True,
            "next_force_run": True,
            "start_hour": 9,
            "end_hour": 18,
            "interval_minutes": 60,
            "last_run_at": "2026-04-19T22:55:00",
        }

        self.assertTrue(self.mod._should_run_task(task, now))


class TelegramSendSmokeTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_agent_daemon()

    def tearDown(self):
        sys.modules.pop("agent_daemon", None)

    def test_tg_send_rejects_ok_false_payload_even_with_http_200(self):
        """HTTP 200 但 body 是 ok:false → 要判定沒送達。

        mock 必須是「只有 .post、沒有 .Session」的 stub：`tg_send` 實際打的是
        `_get_tg_session(requests_module)` 回傳的**共用 Session 物件**，只有這種
        沒有 Session factory 的 stub 才會被原樣回傳（見 _get_tg_session docstring）。
        原本 patch 模組層的 `_requests.post` 打不到那條路 —— 真 requests 有 Session，
        於是這個測試每跑一次就真的對 api.telegram.org 連 24 次，再靠「真的失敗」
        假通過（2026-08-14 socket 層普查抓到）。
        """
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"ok": False, "description": "Bad Request: chat not found"}
        response.text = '{"ok":false}'

        stub = mock.Mock(spec=["post"])
        stub.post.return_value = response

        with mock.patch.object(self.mod, "_requests", stub):
            ok = self.mod._tg_send("token", "chat", "hello")

        self.assertFalse(ok)
        # 斷言 mock 真的被呼叫到 —— 少了這行，patch 錯 seam 也照樣「綠」。
        stub.post.assert_called_once()
        self.assertEqual(stub.post.call_args.kwargs["json"]["text"], "hello")


class MailcheckSmokeTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_agent_daemon()

    def tearDown(self):
        sys.modules.pop("agent_daemon", None)

    def test_mailcheck_only_drafts_for_urgent_and_notifies_business(self):
        messages = [{"id": "u1"}, {"id": "b1"}]
        fake_service = mock.Mock()
        fake_service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": messages
        }

        def classify(mid):
            if mid == "u1":
                return {"category": "其他", "urgency": "R", "from": "A", "subject": "Urgent", "reason": "need now"}
            if mid == "b1":
                return {"category": "詢價", "urgency": "Y", "from": "B", "subject": "Biz", "reason": "quote"}
            return None

        with mock.patch.object(self.mod, "load_state", return_value={}), \
             mock.patch.object(self.mod, "get_service", return_value=fake_service), \
             mock.patch.object(self.mod, "_classify_email_raw", side_effect=classify), \
             mock.patch.object(self.mod, "_draft_reply_for", return_value="draft for urgent") as draft, \
             mock.patch.object(self.mod, "_remember_mailcheck_ids") as remember, \
             mock.patch.object(self.mod, "notify") as notify_mock:
            self.mod.task_mailcheck()

        draft.assert_called_once_with("u1")
        remember.assert_called_once()
        notify_mock.assert_called_once()
        body = notify_mock.call_args.kwargs["body"]
        self.assertIn("draft for urgent", body)
        self.assertIn("Biz", body)


class PonderSmokeTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_agent_daemon()

    def tearDown(self):
        sys.modules.pop("agent_daemon", None)

    def test_ponder_deduplicates_existing_insights(self):
        seen = self.mod._hash_insight("🔔 先前建議")
        fake_calendar = mock.Mock()
        fake_calendar.events.return_value.list.return_value.execute.return_value = {"items": []}
        gemini_resp = types.SimpleNamespace(text="🔔 先前建議\n🔔 新的追蹤客戶")

        with mock.patch.object(self.mod, "_in_working_hours", return_value=True), \
             mock.patch.object(self.mod, "load_state", return_value={"ponder_seen_hashes": [seen]}), \
             mock.patch.object(self.mod, "summarize_inbox", return_value="mail"), \
             mock.patch.object(self.mod, "get_service", return_value=fake_calendar), \
             mock.patch.object(self.mod, "search_gmail", return_value="sent thread"), \
             mock.patch.object(self.mod, "_gemini_generate", return_value=gemini_resp), \
             mock.patch.object(self.mod, "_remember_ponder_insights") as remember, \
             mock.patch.object(self.mod, "notify") as notify_mock:
            self.mod.task_ponder()

        remember.assert_called_once()
        fresh = remember.call_args.args[1]
        self.assertEqual(fresh, ["🔔 新的追蹤客戶"])
        notify_mock.assert_called_once()
        self.assertIn("新的追蹤客戶", notify_mock.call_args.kwargs["body"])

    def test_ponder_skips_cleanly_on_gemini_error(self):
        # 暫時性 Gemini 503 不該讓 task_ponder 拋例外（→ daemon exit 1 → smoke 紅燈），
        # 應 log 後乾淨跳過，且不通知、不記 seen。
        fake_calendar = mock.Mock()
        fake_calendar.events.return_value.list.return_value.execute.return_value = {"items": []}

        with mock.patch.object(self.mod, "_in_working_hours", return_value=True), \
             mock.patch.object(self.mod, "load_state", return_value={"ponder_seen_hashes": []}), \
             mock.patch.object(self.mod, "summarize_inbox", return_value="mail"), \
             mock.patch.object(self.mod, "get_service", return_value=fake_calendar), \
             mock.patch.object(self.mod, "search_gmail", return_value="sent thread"), \
             mock.patch.object(self.mod, "_gemini_generate", side_effect=RuntimeError("503 UNAVAILABLE")), \
             mock.patch.object(self.mod, "_remember_ponder_insights") as remember, \
             mock.patch.object(self.mod, "notify") as notify_mock:
            self.mod.task_ponder()  # 不該拋

        notify_mock.assert_not_called()
        remember.assert_not_called()

    def test_ponder_skips_mark_seen_when_notify_fails(self):
        # 推送失敗（Telegram/Gmail 暫時性 5xx）時不可記 seen，否則 insight 被永久
        # dedup 掉、再也不補送；應乾淨跳過、下一輪重試遞送。
        fake_calendar = mock.Mock()
        fake_calendar.events.return_value.list.return_value.execute.return_value = {"items": []}
        gemini_resp = types.SimpleNamespace(text="🔔 新的追蹤客戶")

        with mock.patch.object(self.mod, "_in_working_hours", return_value=True), \
             mock.patch.object(self.mod, "load_state", return_value={"ponder_seen_hashes": []}), \
             mock.patch.object(self.mod, "summarize_inbox", return_value="mail"), \
             mock.patch.object(self.mod, "get_service", return_value=fake_calendar), \
             mock.patch.object(self.mod, "search_gmail", return_value="sent thread"), \
             mock.patch.object(self.mod, "_gemini_generate", return_value=gemini_resp), \
             mock.patch.object(self.mod, "_remember_ponder_insights") as remember, \
             mock.patch.object(self.mod, "notify", side_effect=RuntimeError("telegram 502")):
            self.mod.task_ponder()  # 不該拋

        remember.assert_not_called()


if __name__ == "__main__":
    unittest.main()
