import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("AGENT_DAEMON_MODE", "1")


class AgentLazyPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("agent", None)
        cls.agent = importlib.import_module("agent")

    def test_search_drive_files_builds_valid_drive_query(self):
        # search_drive_files / upload_to_drive now live in agent_core.google_suite;
        # patch there so the module-local bindings (get_service, _clean_path,
        # _get_media_file_upload) actually take effect.
        from agent_core import google_suite as gs

        executed = {}

        class FakeFiles:
            def list(self, **kwargs):
                executed.update(kwargs)
                return types.SimpleNamespace(execute=lambda: {"files": [{"name": "spec", "id": "123"}]})

        fake_service = types.SimpleNamespace(files=lambda: FakeFiles())

        with mock.patch.object(gs, "get_service", return_value=fake_service):
            result = self.agent.search_drive_files("bob's file")

        self.assertIn("spec", result)
        self.assertEqual(executed["q"], "name contains 'bob\\'s file' and trashed = false")

    def test_upload_to_drive_uses_media_upload_and_parent_folder(self):
        from agent_core import google_suite as gs

        executed = {}

        class FakeFiles:
            def create(self, **kwargs):
                executed.update(kwargs)
                return types.SimpleNamespace(execute=lambda: {"id": "file-1"})

        fake_service = types.SimpleNamespace(files=lambda: FakeFiles())
        media_factory = mock.Mock(return_value="MEDIA")

        with mock.patch.object(gs, "_clean_path", return_value="/tmp/report.pdf"), \
             mock.patch.object(gs.os.path, "exists", return_value=True), \
             mock.patch.object(gs, "get_service", return_value=fake_service), \
             mock.patch.object(gs, "_get_media_file_upload", return_value=media_factory):
            result = self.agent.upload_to_drive("~/report.pdf", folder_id="folder-123")

        self.assertEqual(result, "上傳成功！")
        self.assertEqual(executed["body"]["name"], "report.pdf")
        self.assertEqual(executed["body"]["parents"], ["folder-123"])
        self.assertEqual(executed["media_body"], "MEDIA")
        media_factory.assert_called_once_with("/tmp/report.pdf", resumable=True)

    def test_upload_to_drive_rejects_missing_file(self):
        from agent_core import google_suite as gs
        with mock.patch.object(gs, "_clean_path", return_value="/tmp/missing.pdf"), \
             mock.patch.object(gs.os.path, "exists", return_value=False):
            result = self.agent.upload_to_drive("~/missing.pdf")

        self.assertEqual(result, "錯誤：找不到 /tmp/missing.pdf")

    def test_get_youtube_transcript_supports_fetch_only_api(self):
        fake_module = types.ModuleType("youtube_transcript_api")

        class Snippet:
            def __init__(self, text):
                self.text = text

        class FakeApi:
            def fetch(self, video_id, languages):
                return types.SimpleNamespace(snippets=[Snippet("hello"), Snippet("world")])

        fake_module.YouTubeTranscriptApi = FakeApi

        with mock.patch.dict(sys.modules, {"youtube_transcript_api": fake_module}):
            result = self.agent.get_youtube_transcript("https://youtu.be/abc123")

        self.assertIn("hello world", result)

    def test_get_youtube_transcript_falls_back_to_audio_and_cleans_up_upload(self):
        # get_youtube_transcript now lives in agent_core.youtube; patch there.
        from agent_core import youtube as yt_mod

        fake_transcript_module = types.ModuleType("youtube_transcript_api")

        class FakeTranscriptApi:
            def fetch(self, video_id, languages):
                raise RuntimeError("no subtitles")

        fake_transcript_module.YouTubeTranscriptApi = FakeTranscriptApi

        fake_ytdlp_module = types.ModuleType("yt_dlp")

        class FakeYoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def download(self, _urls):
                with open(self.opts["outtmpl"], "wb") as fh:
                    fh.write(b"audio-bytes")

        fake_ytdlp_module.YoutubeDL = FakeYoutubeDL
        uploaded_file = types.SimpleNamespace(name="gemini-file-1")
        fake_files = types.SimpleNamespace(
            upload=mock.Mock(return_value=uploaded_file),
            delete=mock.Mock(),
        )
        fake_client = types.SimpleNamespace(files=fake_files)
        fake_resp = types.SimpleNamespace(text="音軌摘要")

        with tempfile.TemporaryDirectory() as tmpdir, \
             mock.patch.dict(sys.modules, {
                 "youtube_transcript_api": fake_transcript_module,
                 "yt_dlp": fake_ytdlp_module,
             }), \
             mock.patch.object(yt_mod.tempfile, "gettempdir", return_value=tmpdir), \
             mock.patch.object(yt_mod, "_get_gemini_client", return_value=fake_client), \
             mock.patch.object(yt_mod, "_wait_for_file_ready", return_value=uploaded_file), \
             mock.patch.object(yt_mod, "_gemini_generate", return_value=fake_resp):
            result = self.agent.get_youtube_transcript("https://youtu.be/abc123")

        self.assertIn("音軌摘要", result)
        fake_files.upload.assert_called_once()
        fake_files.delete.assert_called_once_with(name="gemini-file-1")

    def test_get_youtube_transcript_reports_missing_dependencies_for_audio_fallback(self):
        with mock.patch.dict(sys.modules, {"youtube_transcript_api": None, "yt_dlp": None}):
            result = self.agent.get_youtube_transcript("https://example.com/no-id-format")

        self.assertIn("缺少套件", result)

    def test_get_google_credentials_refuses_interactive_oauth_in_daemon_mode(self):
        # get_google_credentials now lives in agent_core.google_auth; patch there
        # so the module-local TOKEN_FILE / CREDENTIALS_FILE / os.path bindings
        # actually take effect.
        from agent_core import google_auth as ga

        fake_credentials_cls = types.SimpleNamespace(
            from_authorized_user_file=lambda *_args, **_kwargs: None
        )
        fake_flow_cls = types.SimpleNamespace(
            from_client_secrets_file=lambda *_args, **_kwargs: mock.Mock()
        )

        with mock.patch.object(ga, "_IS_DAEMON_MODE", True), \
             mock.patch.object(ga, "TOKEN_FILE", "/tmp/missing-token.json"), \
             mock.patch.object(ga, "CREDENTIALS_FILE", "/tmp/fake-creds.json"), \
             mock.patch.object(ga, "_get_google_oauth_classes", return_value=(fake_credentials_cls, fake_flow_cls)), \
             mock.patch.object(ga.os.path, "exists", side_effect=lambda p: p == "/tmp/fake-creds.json"), \
             mock.patch.object(ga, "_notify_daemon_oauth_blocked") as notify_mock:
            with self.assertRaisesRegex(RuntimeError, "daemon 模式下無法啟動互動式 OAuth 授權"):
                self.agent.get_google_credentials()
        notify_mock.assert_called_once_with()

    def test_daemon_oauth_alert_falls_back_to_email_and_latches(self):
        from agent_core import google_auth as ga

        old_latch = ga._daemon_oauth_alert_sent
        ga._daemon_oauth_alert_sent = False
        try:
            with mock.patch.object(ga, "TOKEN_FILE", "/tmp/token.json"), \
                 mock.patch.dict(ga.os.environ, {"XPC_SERVICE_NAME": "com.red.test"}, clear=False), \
                 mock.patch("agent_core.telegram.telegram_push", return_value="錯誤：missing chat") as telegram_push, \
                 mock.patch("agent_core.daemon_helpers.notify") as notify:
                ga._notify_daemon_oauth_blocked()
                ga._notify_daemon_oauth_blocked()
        finally:
            ga._daemon_oauth_alert_sent = old_latch

        telegram_push.assert_called_once()
        notify.assert_called_once()
        self.assertIn("Google OAuth 失效", notify.call_args.kwargs["subject"])
        self.assertIn("com.red.test", notify.call_args.kwargs["body"])

    def test_search_gmail_formats_results(self):
        class FakeMessages:
            def list(self, **kwargs):
                self.list_kwargs = kwargs
                return types.SimpleNamespace(execute=lambda: {"messages": [{"id": "m1"}]})

            def get(self, **kwargs):
                return types.SimpleNamespace(execute=lambda: {
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Quote"},
                            {"name": "From", "value": "alice@example.com"},
                            {"name": "Date", "value": "Mon"},
                        ]
                    },
                    "snippet": "Need pricing soon",
                })

        fake_messages = FakeMessages()
        fake_service = types.SimpleNamespace(
            users=lambda: types.SimpleNamespace(messages=lambda: fake_messages)
        )

        # Gmail wrappers moved to agent_core.gmail; patch on that module.
        from agent_core import gmail as gmail_mod
        with mock.patch.object(gmail_mod, "get_service", return_value=fake_service):
            result = self.agent.search_gmail("from:alice")

        self.assertIn("alice@example.com", result)
        self.assertIn("Quote", result)
        self.assertEqual(fake_messages.list_kwargs["q"], "from:alice")

    def test_read_gmail_includes_body_and_attachments(self):
        fake_message = {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Spec"},
                    {"name": "From", "value": "bob@example.com"},
                    {"name": "To", "value": "me@example.com"},
                    {"name": "Cc", "value": "team@example.com"},
                    {"name": "Date", "value": "Tue"},
                ],
                "parts": [
                    {"filename": "quote.pdf", "body": {"attachmentId": "att-1"}},
                ],
            }
        }
        fake_service = types.SimpleNamespace(
            users=lambda: types.SimpleNamespace(
                messages=lambda: types.SimpleNamespace(
                    get=lambda **kwargs: types.SimpleNamespace(execute=lambda: fake_message)
                )
            )
        )

        from agent_core import gmail as gmail_mod
        with mock.patch.object(gmail_mod, "get_service", return_value=fake_service), \
             mock.patch.object(gmail_mod, "_extract_body", return_value="full body text"):
            result = self.agent.read_gmail("m1")

        self.assertIn("bob@example.com", result)
        self.assertIn("quote.pdf", result)
        self.assertIn("full body text", result)

    def test_send_gmail_builds_and_sends_message(self):
        sent = {}
        def _send(**kwargs):
            sent["body"] = kwargs
            return types.SimpleNamespace(execute=lambda: True)

        fake_messages = types.SimpleNamespace(send=_send)
        fake_service = types.SimpleNamespace(
            users=lambda: types.SimpleNamespace(messages=lambda: fake_messages)
        )

        # gmail wrappers moved to agent_core.gmail; _index_memory now lives in
        # agent_core.memory and gmail imports it at module load — patch on the
        # gmail module where the binding is.
        from agent_core import gmail as gmail_mod
        with mock.patch.object(gmail_mod, "get_service", return_value=fake_service), \
             mock.patch.object(gmail_mod, "_append_signature", side_effect=lambda body: body + "\n--sig"), \
             mock.patch.object(gmail_mod, "_build_mime", return_value=self.agent.MIMEText("hello")), \
             mock.patch.object(gmail_mod, "_index_memory") as index_memory:
            result = self.agent.send_gmail("to@example.com", "Subject", "Body", cc="cc@example.com")

        self.assertIn("已寄出給 to@example.com", result)
        self.assertEqual(sent["body"]["userId"], "me")
        index_memory.assert_called_once()

    def test_reply_gmail_preserves_thread_and_reply_headers(self):
        sent = {}
        orig = {
            "threadId": "thread-1",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Need Quote"},
                    {"name": "From", "value": "alice@example.com"},
                    {"name": "Cc", "value": "team@example.com"},
                    {"name": "Message-ID", "value": "<mid-1>"},
                    {"name": "References", "value": "<old-ref>"},
                ]
            },
        }
        def _send(**kwargs):
            sent["body"] = kwargs
            return types.SimpleNamespace(execute=lambda: True)

        fake_messages = types.SimpleNamespace(
            get=lambda **kwargs: types.SimpleNamespace(execute=lambda: orig),
            send=_send,
        )
        fake_service = types.SimpleNamespace(
            users=lambda: types.SimpleNamespace(messages=lambda: fake_messages)
        )
        fake_msg = self.agent.MIMEText("reply body")

        from agent_core import gmail as gmail_mod
        with mock.patch.object(gmail_mod, "get_service", return_value=fake_service), \
             mock.patch.object(gmail_mod, "_append_signature", side_effect=lambda body: body), \
             mock.patch.object(gmail_mod, "_build_mime", return_value=fake_msg), \
             mock.patch.object(gmail_mod, "_index_memory") as index_memory:
            result = self.agent.reply_gmail("m1", "reply", reply_all=True, cc="boss@example.com")

        self.assertIn("已回覆 alice@example.com", result)
        self.assertEqual(sent["body"]["body"]["threadId"], "thread-1")
        self.assertEqual(fake_msg["In-Reply-To"], "<mid-1>")
        self.assertIn("boss@example.com", fake_msg["cc"])
        index_memory.assert_called_once()

    def test_download_gmail_attachment_saves_matching_files(self):
        payload = {
            "parts": [
                {"filename": "quote.pdf", "body": {"attachmentId": "att-1"}},
                {"filename": "note.txt", "body": {"attachmentId": "att-2"}},
            ]
        }
        fake_messages = types.SimpleNamespace(
            get=lambda **kwargs: types.SimpleNamespace(execute=lambda: {"payload": payload}),
            attachments=lambda: types.SimpleNamespace(
                get=lambda **kwargs: types.SimpleNamespace(
                    execute=lambda: {"data": self.agent.base64.urlsafe_b64encode(b"PDFDATA").decode()}
                )
            ),
        )
        fake_service = types.SimpleNamespace(
            users=lambda: types.SimpleNamespace(messages=lambda: fake_messages)
        )

        from agent_core import gmail as gmail_mod
        with mock.patch.object(gmail_mod, "get_service", return_value=fake_service), \
             mock.patch.object(self.agent.os, "makedirs"), \
             mock.patch.object(self.agent.os.path, "expanduser", side_effect=lambda p: "/tmp/test-downloads"), \
             mock.patch("builtins.open", mock.mock_open()) as m:
            result = self.agent.download_gmail_attachment("m1", filename="quote")

        self.assertIn("quote.pdf", result)
        m.assert_called()

    def test_summarize_inbox_uses_gemini_summary(self):
        fake_messages = types.SimpleNamespace(
            list=lambda **kwargs: types.SimpleNamespace(execute=lambda: {"messages": [{"id": "m1"}]}),
            get=lambda **kwargs: types.SimpleNamespace(
                execute=lambda: {
                    "payload": {"headers": [{"name": "Subject", "value": "Quote"}, {"name": "From", "value": "alice@example.com"}]}
                }
            ),
        )
        fake_service = types.SimpleNamespace(
            users=lambda: types.SimpleNamespace(messages=lambda: fake_messages)
        )
        fake_resp = types.SimpleNamespace(text="🔴 今天就要回")

        from agent_core import gmail as gmail_mod
        with mock.patch.object(gmail_mod, "get_service", return_value=fake_service), \
             mock.patch.object(gmail_mod, "_extract_body", return_value="Need pricing"), \
             mock.patch.object(gmail_mod, "_gemini_generate", return_value=fake_resp):
            result = self.agent.summarize_inbox(hours=12)

        self.assertIn("【信箱摘要 過去 12 小時 / 1 封未讀】", result)
        self.assertIn("🔴 今天就要回", result)

    def test_prioritized_inbox_sorts_by_urgency_and_counts_new_items(self):
        fake_messages = types.SimpleNamespace(
            list=lambda **kwargs: types.SimpleNamespace(
                execute=lambda: {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]}
            )
        )
        fake_service = types.SimpleNamespace(
            users=lambda: types.SimpleNamespace(messages=lambda: fake_messages)
        )
        classifications = {
            "m1": {
                "urgency": "Y",
                "category": "follow-up",
                "subject": "Need updated quote",
                "from": "yellow@example.com",
                "reason": "本週內需要跟進",
                "cached": True,
            },
            "m2": {
                "urgency": "R",
                "category": "pricing",
                "subject": "Confirm today",
                "from": "red@example.com",
                "reason": "今天必須回覆",
                "cached": False,
            },
            "m3": {
                "urgency": "G",
                "category": "fyi",
                "subject": "Just sharing",
                "from": "green@example.com",
                "reason": "可稍後處理",
                "cached": False,
            },
        }

        from agent_core import email_classify as ec_mod
        with mock.patch.object(ec_mod, "get_service", return_value=fake_service), \
             mock.patch.object(ec_mod, "_classify_email_raw", side_effect=lambda mid: classifications[mid]):
            result = self.agent.prioritized_inbox(hours=24, max_mails=10)

        self.assertIn("共 3 封，新分類 2 封", result)
        self.assertLess(result.index("【🔴 今天必回】"), result.index("【🟡 本週內】"))
        self.assertLess(result.index("【🟡 本週內】"), result.index("【🟢 可延/參考】"))
        self.assertIn("id=m2", result)
        self.assertIn("id=m3", result)

    def test_extract_quote_from_thread_sorts_messages_and_uses_latest_metadata(self):
        thread_payload = {
            "messages": [
                {
                    "internalDate": "200",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "late@example.com"},
                            {"name": "Subject", "value": "Final quote"},
                            {"name": "Date", "value": "Tue, 02 Jan 2024 10:00:00 +0000"},
                        ]
                    },
                },
                {
                    "internalDate": "100",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "early@example.com"},
                            {"name": "Subject", "value": "Initial quote"},
                            {"name": "Date", "value": "Mon, 01 Jan 2024 09:00:00 +0000"},
                        ]
                    },
                },
            ]
        }
        fake_service = types.SimpleNamespace(
            users=lambda: types.SimpleNamespace(
                threads=lambda: types.SimpleNamespace(
                    get=lambda **kwargs: types.SimpleNamespace(execute=lambda: thread_payload)
                )
            )
        )
        bodies = iter(["first body", "second body"])
        captured = {}
        fake_resp = types.SimpleNamespace(text=json.dumps({"items": [], "direction": "out"}))

        def capture_prompt(sender, subject, conversation, email_date):
            captured["sender"] = sender
            captured["subject"] = subject
            captured["conversation"] = conversation
            captured["email_date"] = email_date
            return "PROMPT"

        # quote tools moved to agent_core.quote; patch there. _extract_body is
        # still lazy-imported from agent inside _extract_quote_from_thread, so
        # self.agent remains the right patch target for it.
        # Phase 3c：quote 實作搬到 orange_sales 子套件；patch 必須對準真實模組，
        # 因為 shim 只是 re-export 引用，patch shim 不會影響真實模組內部呼叫。
        from agent_core.agents.orange_sales import quote as quote_mod
        with mock.patch.object(quote_mod, "get_service", return_value=fake_service), \
             mock.patch.object(self.agent, "_extract_body", side_effect=lambda _payload: next(bodies)), \
             mock.patch.object(quote_mod, "_quote_extract_prompt", side_effect=capture_prompt), \
             mock.patch.object(quote_mod, "_gemini_generate", return_value=fake_resp):
            result = self.agent._extract_quote_from_thread("thread-1")

        self.assertEqual(captured["sender"], "late@example.com")
        self.assertEqual(captured["subject"], "Final quote")
        self.assertEqual(captured["email_date"], "2024-01-02")
        self.assertLess(captured["conversation"].index("Initial quote"), captured["conversation"].index("Final quote"))
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["msg_count"], 2)

    def test_extract_quote_from_email_appends_rows_and_marks_processed(self):
        extracted = {
            "parsed": {"direction": "out", "customer": "ACME"},
            "message_id": "m1",
            "sender": "sales@acme.com",
            "subject": "Quote",
            "email_date": "2026-04-19",
        }
        rows = [{"sku": "SHOE-1"}]

        # Phase 3c：quote 實作搬到 orange_sales 子套件；patch 必須對準真實模組，
        # 因為 shim 只是 re-export 引用，patch shim 不會影響真實模組內部呼叫。
        # 去重帳本改以 thread 為單位：先 _resolve_thread_id 查重、再抽整串。
        from agent_core.agents.orange_sales import quote as quote_mod
        with mock.patch.object(quote_mod, "_load_extracted_ids", return_value=set()), \
             mock.patch.object(quote_mod, "_resolve_thread_id", return_value="thr1"), \
             mock.patch.object(quote_mod, "_extract_quote_from_thread", return_value=extracted), \
             mock.patch.object(quote_mod, "_flatten_quote_to_rows", return_value=rows), \
             mock.patch.object(quote_mod, "_append_quote_rows") as append_rows, \
             mock.patch.object(quote_mod, "_save_extracted_ids") as save_ids:
            result = self.agent.extract_quote_from_email("m1")

        self.assertIn("抽出 1 筆報價資料", result)
        append_rows.assert_called_once_with(rows)
        save_ids.assert_called_once()
        self.assertIn("m1", save_ids.call_args.args[0])
        self.assertIn("thr1", save_ids.call_args.args[0])  # thread id 也入帳（防同串重抽灌水）

    def test_extract_quote_from_email_rejects_empty_message_id(self):
        result = self.agent.extract_quote_from_email("   ")
        self.assertEqual(result, "錯誤：message_id 不能空。")

    def test_build_quote_history_processes_only_unseen_threads_and_flushes_rows(self):
        thread_pages = [
            {"threads": [{"id": "t1"}, {"id": "t2"}]},
            {"threads": [{"id": "t2"}, {"id": "t3"}]},
        ]
        page_iter = iter(thread_pages)

        class FakeThreads:
            def list(self, **kwargs):
                return object()

            def list_next(self, req, resp):
                try:
                    return object() if resp is thread_pages[0] else None
                except Exception:
                    return None

        fake_threads = FakeThreads()
        fake_service = types.SimpleNamespace(
            users=lambda: types.SimpleNamespace(threads=lambda: fake_threads)
        )
        processed_saved = []

        def fake_execute():
            return next(page_iter)

        fake_req = types.SimpleNamespace(execute=fake_execute)
        fake_threads.list = mock.Mock(return_value=fake_req)
        fake_threads.list_next = mock.Mock(side_effect=lambda req, resp: types.SimpleNamespace(execute=fake_execute) if resp is thread_pages[0] else None)

        extract_results = {
            "t2": {
                "parsed": {"items": [{"sku": "A"}], "direction": "out", "customer": "ACME"},
                "message_id": "t2",
                "sender": "sales@acme.com",
                "subject": "Quote A",
                "email_date": "2026-04-19",
            },
            "t3": {
                "parsed": {"items": [], "direction": "unknown", "customer": "ACME"},
                "message_id": "t3",
                "sender": "sales@acme.com",
                "subject": "Quote B",
                "email_date": "2026-04-19",
            },
        }

        def fake_flatten(extracted):
            items = extracted["parsed"]["items"]
            return [{"sku": "A"}] if items else []

        # Phase 3c：quote 實作搬到 orange_sales 子套件；patch 必須對準真實模組，
        # 因為 shim 只是 re-export 引用，patch shim 不會影響真實模組內部呼叫。
        from agent_core.agents.orange_sales import quote as quote_mod
        # build_quote_history does `import time as _t` inside the function;
        # patching the shared time module (via self.agent.time or directly)
        # still works because _t is the same object.
        with mock.patch.object(quote_mod, "get_service", return_value=fake_service), \
             mock.patch.object(quote_mod, "_load_extracted_ids", return_value={"t1"}), \
             mock.patch.object(quote_mod, "_extract_quote_from_thread", side_effect=lambda tid: extract_results[tid]), \
             mock.patch.object(quote_mod, "_flatten_quote_to_rows", side_effect=fake_flatten), \
             mock.patch.object(quote_mod, "_append_quote_rows") as append_rows, \
             mock.patch.object(quote_mod, "_save_extracted_ids", side_effect=lambda ids: processed_saved.append(set(ids))), \
             mock.patch.object(self.agent.time, "sleep") as sleep_mock:
            result = self.agent.build_quote_history(days=30, max_threads=10, pause_ms=1)

        self.assertIn("處理 2 個 thread，抽出 1 筆報價", result)
        append_rows.assert_called_once_with([{"sku": "A"}])
        self.assertIn({"t1", "t2", "t3"}, processed_saved)
        sleep_mock.assert_called_once()

    def test_build_quote_history_returns_when_all_threads_already_processed(self):
        fake_threads = types.SimpleNamespace(
            list=lambda **kwargs: types.SimpleNamespace(execute=lambda: {"threads": [{"id": "t1"}]}),
            list_next=lambda req, resp: None,
        )
        fake_service = types.SimpleNamespace(
            users=lambda: types.SimpleNamespace(threads=lambda: fake_threads)
        )

        # Phase 3c：quote 實作搬到 orange_sales 子套件；patch 必須對準真實模組，
        # 因為 shim 只是 re-export 引用，patch shim 不會影響真實模組內部呼叫。
        from agent_core.agents.orange_sales import quote as quote_mod
        with mock.patch.object(quote_mod, "get_service", return_value=fake_service), \
             mock.patch.object(quote_mod, "_load_extracted_ids", return_value={"t1"}):
            result = self.agent.build_quote_history(days=30, max_threads=10, pause_ms=0)

        self.assertEqual(result, "全部 1 個 thread 都已處理過。")

    def test_meeting_briefing_assembles_sections_without_telegram_push(self):
        event = {
            "summary": "ACME Pricing Review",
            "start": {"dateTime": "2026-04-19T10:00:00+00:00"},
            "location": "Zoom",
            "attendees": [{"email": "buyer@acme.com"}],
            "description": "Discuss quote",
        }

        class FakeEventGet:
            def execute(self):
                return event

        fake_calendar = types.SimpleNamespace(
            events=lambda: types.SimpleNamespace(get=lambda **kwargs: FakeEventGet())
        )

        class FakeMessages:
            def list(self, **kwargs):
                return types.SimpleNamespace(execute=lambda: {"messages": [{"id": "m1"}]})

            def get(self, **kwargs):
                return types.SimpleNamespace(
                    execute=lambda: {
                        "payload": {"headers": [
                            {"name": "Subject", "value": "Quote follow-up"},
                            {"name": "From", "value": "buyer@acme.com"},
                            {"name": "Date", "value": "Fri, 18 Apr 2026 08:00:00 +0000"},
                        ]},
                        "labelIds": ["UNREAD"],
                    }
                )

        fake_gmail = types.SimpleNamespace(
            users=lambda: types.SimpleNamespace(messages=lambda: FakeMessages())
        )

        def fake_get_service(name, version):
            if name == "calendar":
                return fake_calendar
            if name == "gmail":
                return fake_gmail
            raise AssertionError(name)

        # meeting_briefing lives in agent_core.briefing; patch its module-local
        # bindings. query_quote_history now lives in agent_core.quote and is
        # imported at module load time, so we patch it on the briefing module
        # where it was bound. `recall` still lives in agent.py and is lazy-
        # imported inside the function, so self.agent is still the right
        # patch target for it.
        from agent_core import briefing as brief_mod
        with mock.patch.object(brief_mod, "get_service", side_effect=fake_get_service), \
             mock.patch.object(brief_mod, "query_quote_history", return_value="🔍 查到 2 筆報價"), \
             mock.patch.object(brief_mod, "recall", return_value="memory summary"), \
             mock.patch.object(brief_mod, "telegram_push") as telegram_push:
            result = self.agent.meeting_briefing(event_id="evt-1", lookback_days=14, push_telegram=False)

        self.assertIn("# 📋 會議 Briefing：ACME Pricing Review", result)
        self.assertIn("## 📧 近 14 天 Email 往來", result)
        self.assertIn("## 💰 相關報價歷史", result)
        self.assertIn("## 🧠 相關記憶（RAG 語意搜尋）", result)
        self.assertIn("buyer@acme.com", result)
        telegram_push.assert_not_called()

    def test_meeting_briefing_returns_when_no_upcoming_meeting(self):
        from agent_core import briefing as brief_mod
        with mock.patch.object(brief_mod, "get_service", return_value=mock.Mock()), \
             mock.patch.object(brief_mod, "_find_next_meeting", return_value=(None, None)):
            result = self.agent.meeting_briefing(event_id="", lookback_days=14, push_telegram=False)

        self.assertEqual(result, "接下來 72 小時內沒有會議。")

    def test_recall_falls_back_to_bm25_when_vector_query_fails(self):
        fake_collection = types.SimpleNamespace(query=mock.Mock(side_effect=RuntimeError("vector down")))
        fake_bm25 = types.SimpleNamespace(get_scores=lambda _tokens: [3.0, 1.0])
        # 新版緊湊 cache：沒有 docs/metas 全量欄位，命中後用 _fetch_docs_by_ids 補抓
        fake_idx = {
            "bm25": fake_bm25,
            "ids": ["id-1", "id-2"],
            "sources": ["email", "note"],
            "dates": ["", ""],
        }
        fake_store = {
            "id-1": ("shoe order abc", {"source": "email", "ts": "2026-04-19T10:00:00"}),
            "id-2": ("other memory", {"source": "note", "ts": "2026-04-18T09:00:00"}),
        }

        from agent_core import memory as mem_mod
        with mock.patch.object(mem_mod, "_get_memory_collection", return_value=fake_collection), \
             mock.patch.object(mem_mod, "_build_bm25_index", return_value=fake_idx), \
             mock.patch.object(mem_mod, "_fetch_docs_by_ids",
                               side_effect=lambda col, ids: {i: fake_store[i] for i in ids if i in fake_store}):
            result = self.agent.recall("shoe abc", mode="hybrid")

        self.assertIn("找到 2 筆", result)
        self.assertIn("id=id-1", result)
        self.assertIn("BM25", result)

    def test_remember_returns_doc_id_when_index_succeeds(self):
        from agent_core import memory as mem_mod
        with mock.patch.object(mem_mod, "_index_memory", return_value="doc-1"):
            result = self.agent.remember("note text", tags="tag1")

        self.assertIn("id=doc-1", result)

    def test_forget_memory_deletes_from_collection(self):
        fake_collection = types.SimpleNamespace(delete=mock.Mock())

        from agent_core import memory as mem_mod
        with mock.patch.object(mem_mod, "_get_memory_collection", return_value=fake_collection):
            result = self.agent.forget_memory("doc-1")

        self.assertEqual(result, "已刪除 doc-1")
        fake_collection.delete.assert_called_once_with(ids=["doc-1"])

    def test_memory_stats_handles_unavailable_collection(self):
        from agent_core import memory as mem_mod
        with mock.patch.object(mem_mod, "_get_memory_collection", return_value=None):
            result = self.agent.memory_stats()

        self.assertIn("向量資料庫不可用", result)

    def test_collection_get_all_paginates_until_exhausted(self):
        from agent_core import memory as mem_mod

        fake_collection = types.SimpleNamespace(
            get=mock.Mock(side_effect=[
                {
                    "ids": ["id-1", "id-2"],
                    "documents": ["doc-1", "doc-2"],
                    "metadatas": [{"source": "email"}, {"source": "note"}],
                },
                {
                    "ids": ["id-3"],
                    "documents": ["doc-3"],
                    "metadatas": [{"source": "meeting"}],
                },
            ])
        )

        result = mem_mod._collection_get_all(
            fake_collection,
            include=["documents", "metadatas"],
            batch_size=2,
        )

        self.assertEqual(result["ids"], ["id-1", "id-2", "id-3"])
        self.assertEqual(result["documents"], ["doc-1", "doc-2", "doc-3"])
        self.assertEqual(
            result["metadatas"],
            [{"source": "email"}, {"source": "note"}, {"source": "meeting"}],
        )
        self.assertEqual(fake_collection.get.call_count, 2)
        first_call = fake_collection.get.call_args_list[0].kwargs
        second_call = fake_collection.get.call_args_list[1].kwargs
        self.assertEqual(first_call["offset"], 0)
        self.assertEqual(second_call["offset"], 2)

    def test_memory_stats_uses_all_paginated_metadatas(self):
        from agent_core import memory as mem_mod

        fake_collection = types.SimpleNamespace(count=mock.Mock(return_value=3))
        fake_fetch = mock.Mock(return_value=[
            {"source": "email"},
            {"source": "note"},
            {"source": "email"},
        ])

        with mock.patch.object(mem_mod, "_get_memory_collection", return_value=fake_collection), \
             mock.patch.object(mem_mod, "_fetch_all_memory_metadatas", fake_fetch):
            result = self.agent.memory_stats()

        self.assertIn("共 3 筆", result)
        self.assertIn("email=2", result)
        self.assertIn("note=1", result)

    def test_browser_open_wraps_session_errors(self):
        with mock.patch.object(self.agent._BrowserSession, "_ensure", side_effect=RuntimeError("boom")):
            result = self.agent.browser_open("https://example.com")

        self.assertIn("瀏覽器操作失敗", result)
        self.assertIn("RuntimeError", result)

    def test_browser_read_truncates_long_content(self):
        fake_page = types.SimpleNamespace(evaluate=lambda _js: "x" * 600)

        with mock.patch.object(self.agent._BrowserSession, "_ensure", return_value=fake_page):
            result = self.agent.browser_read(max_chars=500)

        self.assertIn("已截斷", result)
        self.assertIn("<browser-content>", result)

    def test_browser_click_returns_success_message(self):
        locator = types.SimpleNamespace(first=types.SimpleNamespace(click=lambda **kwargs: None))
        fake_page = types.SimpleNamespace(
            locator=lambda _selector: locator,
            url="https://example.com/after",
        )

        with mock.patch.object(self.agent._BrowserSession, "_ensure", return_value=fake_page):
            result = self.agent.browser_click("text=Submit")

        self.assertIn("✅ 已點擊：text=Submit", result)

    def test_browser_fill_returns_success_message(self):
        locator = types.SimpleNamespace(first=types.SimpleNamespace(fill=lambda *_args, **_kwargs: None))
        fake_page = types.SimpleNamespace(locator=lambda _selector: locator)

        with mock.patch.object(self.agent._BrowserSession, "_ensure", return_value=fake_page):
            result = self.agent.browser_fill("#email", "a@example.com")

        self.assertIn("✅ 已填入 #email", result)

    def test_browser_type_returns_success_message(self):
        first = types.SimpleNamespace(
            click=lambda: None,
            press_sequentially=lambda *_args, **_kwargs: None,
        )
        locator = types.SimpleNamespace(first=first)
        fake_page = types.SimpleNamespace(locator=lambda _selector: locator)

        with mock.patch.object(self.agent._BrowserSession, "_ensure", return_value=fake_page):
            result = self.agent.browser_type("#search", "nike")

        self.assertIn("✅ 已逐字輸入 #search", result)

    def test_browser_press_returns_success_message(self):
        fake_page = types.SimpleNamespace(keyboard=types.SimpleNamespace(press=lambda _key: None))

        with mock.patch.object(self.agent._BrowserSession, "_ensure", return_value=fake_page):
            result = self.agent.browser_press("Enter")

        self.assertEqual(result, "✅ 已按 Enter")


class TelegramLazyPathTests(unittest.TestCase):
    """agent_daemon no longer imports `agent`; it pulls gemini_client helpers
    directly from agent_core. This test patches those helpers on the loaded
    agent_daemon module so _tg_handle_message returns a canned reply."""

    def setUp(self):
        sys.modules.pop("agent_daemon", None)
        self.mod = importlib.import_module("agent_daemon")

        fake_chat = types.SimpleNamespace(
            send_message=lambda _msg: types.SimpleNamespace(text="telegram ok")
        )
        self._patches = [
            mock.patch.object(self.mod, "_get_gemini_client", return_value=types.SimpleNamespace(
                chats=types.SimpleNamespace(create=lambda **kwargs: fake_chat)
            )),
            mock.patch.object(self.mod, "_get_genai_types", return_value=types.SimpleNamespace(
                GenerateContentConfig=lambda **kwargs: kwargs,
                AutomaticFunctionCallingConfig=lambda **kwargs: kwargs,
            )),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        sys.modules.pop("agent_daemon", None)

    def test_tg_handle_message_supports_lazy_agent_interfaces(self):
        text = self.mod._tg_handle_message("hello")
        self.assertEqual(text, "telegram ok")


if __name__ == "__main__":
    unittest.main()
