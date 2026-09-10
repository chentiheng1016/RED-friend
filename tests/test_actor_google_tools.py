"""Actor-scoped Gmail/行事曆工具測試（全 mock，不碰任何真實郵件/行事曆）。

驗證：
  - 可用性判定（email + SA 存在 + env 未關）
  - 工具以**委派 service**（subject=員工 email）操作，非大王 OAuth
  - 附件白名單擋掉非上傳區路徑
  - scope 未開時回友善提示（含 gmail_ops 把錯誤吞成字串的情況）
  - 「全公司」展開 + create_calendar_event 帶 attendees + sendUpdates=all
  - actor-scoped 工具永不被 tool RPC proxy 派發（by-name 會破功）
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

SP = {
    "chat_id": "9990000003", "color": "red",
    "email": "gm@company.example", "name": "gm", "source": "employee_registry",
}
_FAKE_SA = "/tmp/fake_service_account.json"
_COMPANY = [
    {"email": "gm@company.example"}, {"email": "sampledev@company.example"},
    {"email": "cashier@company.example"},
]


def _with_sa(**extra):
    env = {"RED_GOOGLE_SERVICE_ACCOUNT_FILE": _FAKE_SA}
    env.update(extra)
    return env


class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeEvents:
    def __init__(self, sink):
        self._sink = sink

    def insert(self, **kwargs):
        self._sink["insert"] = kwargs
        return _FakeExecutable({"id": "evt_123"})

    def list(self, **kwargs):
        return _FakeExecutable({"items": []})


class _FakeCalendarService:
    def __init__(self, sink):
        self._sink = sink

    def events(self):
        return _FakeEvents(self._sink)


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        # SA 檔存在性是判定條件 — 用 patch 讓 _sa_file() 認為存在
        patcher = mock.patch("os.path.exists", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_available_for_employee_with_email(self):
        from agent_core.actor_google_tools import actor_google_tools_available
        with mock.patch.dict(os.environ, _with_sa(), clear=False):
            self.assertTrue(actor_google_tools_available(SP))

    def test_unavailable_without_email(self):
        from agent_core.actor_google_tools import actor_google_tools_available
        with mock.patch.dict(os.environ, _with_sa(), clear=False):
            self.assertFalse(actor_google_tools_available({"chat_id": "1", "color": "red"}))

    def test_env_kill_switch(self):
        from agent_core.actor_google_tools import actor_google_tools_available
        with mock.patch.dict(os.environ, _with_sa(RED_TG_ACTOR_GOOGLE_TOOLS="0"), clear=False):
            self.assertFalse(actor_google_tools_available(SP))

    def test_unavailable_when_no_sa(self):
        from agent_core.actor_google_tools import actor_google_tools_available
        with mock.patch("os.path.exists", return_value=False), \
                mock.patch.dict(os.environ, {"RED_GOOGLE_SERVICE_ACCOUNT_FILE": ""}, clear=False):
            self.assertFalse(actor_google_tools_available(SP))


class BuildAndMarkTests(unittest.TestCase):
    def setUp(self):
        # live checkout 有真實 var/state/google/service_account.json；
        # build_actor_google_tools 的 sa_file 空字串 fallback `(sa_file or
        # _sa_file())` 會解析到它，於是連 sa_file="" 都建得出工具、
        # test_no_tools_without_email_or_sa 失敗（CI 乾淨環境沒這檔才會過）。
        # 把 SA 解析來源中性化，讓「沒 SA → 沒工具」的斷言在 live 與 CI 都成立。
        # test_tools_built_and_marked 顯式帶 _FAKE_SA、短路掉 fallback，不受影響。
        patcher = mock.patch("agent_core.actor_google_tools._sa_file", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tools_built_and_marked(self):
        from agent_core.actor_google_tools import build_actor_google_tools
        tools = build_actor_google_tools("gm@company.example", sa_file=_FAKE_SA)
        names = [t.__name__ for t in tools]
        self.assertEqual(names, [
            "search_gmail", "read_gmail", "send_gmail", "reply_gmail",
            "list_calendar_events", "create_calendar_event", "set_my_signature",
        ])
        for t in tools:
            self.assertTrue(getattr(t, "_actor_scoped", False), t.__name__)
            # 大王選免確認 → 標記讓 telegram 確認門略過
            self.assertTrue(getattr(t, "_tg_auth_wrapped", False), t.__name__)
            self.assertEqual(getattr(t, "_actor_email", ""), "gm@company.example")

    def test_no_tools_without_email_or_sa(self):
        from agent_core.actor_google_tools import build_actor_google_tools
        self.assertEqual(build_actor_google_tools("", sa_file=_FAKE_SA), [])
        self.assertEqual(build_actor_google_tools("x@y.com", sa_file=""), [])

    def test_annotations_resolved_not_strings(self):
        """回歸（#113 漏網的動態路徑）：actor 工具在 `from __future__ import
        annotations` 模組裡逐 actor 動態建、不經 tool_registry 靜態組裝點。若
        __annotations__ 留成字串，google-genai 派發 send_gmail 參數時會炸
        `isinstance() arg 2 must be a type...`（UserS 2026-06-15 實際踩到）。
        """
        import inspect

        from agent_core.actor_google_tools import build_actor_google_tools
        tools = build_actor_google_tools("gm@company.example", sa_file=_FAKE_SA)
        offenders = []
        for t in tools:
            ann = getattr(t, "__annotations__", {}) or {}
            bad = {k: v for k, v in ann.items() if isinstance(v, str)}
            if bad:
                offenders.append((t.__name__, bad))
        self.assertEqual(offenders, [], f"actor 工具仍帶字串 annotation：{offenders}")
        # send_gmail 的 to 參數要能過 isinstance（SDK 派發的那道檢查）
        send = {t.__name__: t for t in tools}["send_gmail"]
        to_ann = inspect.signature(send).parameters["to"].annotation
        self.assertIs(to_ann, str)
        self.assertTrue(isinstance("a@b.com", to_ann))

    def test_genai_dispatch_accepts_send_gmail(self):
        """直接走 google-genai 的參數轉換（UserS 出事的那條路）：修復前字串
        annotation → isinstance 炸 TypeError；修復後值原樣通過、不打任何 Gmail API。
        """
        try:
            from google.genai._extra_utils import convert_argument_from_function
        except Exception:  # SDK 私有 API — 版本變動時跳過而非紅
            self.skipTest("google.genai._extra_utils 不可用")
        from agent_core.actor_google_tools import build_actor_google_tools
        send = {
            t.__name__: t
            for t in build_actor_google_tools("gm@company.example", sa_file=_FAKE_SA)
        }["send_gmail"]
        converted = convert_argument_from_function(
            {"to": "a@b.com", "subject": "hi", "body": "yo"}, send
        )
        self.assertEqual(converted["to"], "a@b.com")
        self.assertEqual(converted["subject"], "hi")
        self.assertEqual(converted["body"], "yo")


class AttendeeExpansionTests(unittest.TestCase):
    def test_expand(self):
        from agent_core import actor_google_tools as agt
        with mock.patch.object(agt, "_company_emails", return_value=[e["email"] for e in _COMPANY]):
            self.assertEqual(len(agt._expand_attendees("全公司")), 3)
            self.assertEqual(len(agt._expand_attendees("all")), 3)
        self.assertEqual(agt._expand_attendees("a@x.com, b@y.com"), ["a@x.com", "b@y.com"])
        self.assertEqual(agt._expand_attendees(""), [])

    def test_expand_chinese_separators(self):
        # gemini review：中文頓號/全形逗號分號也要當分隔符
        from agent_core import actor_google_tools as agt
        self.assertEqual(
            agt._expand_attendees("a@x.com、b@y.com，c@z.com；d@w.com"),
            ["a@x.com", "b@y.com", "c@z.com", "d@w.com"],
        )


class AttachmentVettingTests(unittest.TestCase):
    def test_vetting(self):
        from agent_core.actor_google_tools import _vet_attachments
        ok, _ = _vet_attachments("")
        self.assertTrue(ok)
        ok, msg = _vet_attachments("/etc/passwd")
        self.assertFalse(ok)
        self.assertIn("只能用你上傳", msg)
        good = os.path.expanduser("~/Downloads/小紅-uploads/2026-06-13/x.pdf")
        ok, _ = _vet_attachments(good)
        self.assertTrue(ok)

    def test_send_rejects_bad_attachment_without_calling_api(self):
        from agent_core.actor_google_tools import build_actor_google_tools
        tools = {t.__name__: t for t in build_actor_google_tools("gm@company.example", sa_file=_FAKE_SA)}
        with mock.patch("agent_core.gmail_ops.send_gmail") as send:
            out = tools["send_gmail"]("x@y.com", "s", "b", attachments="/etc/passwd")
        send.assert_not_called()
        self.assertIn("只能用你上傳", out)

    def test_semicolon_bypass_blocked(self):
        # Codex P1：split_paths 切 [,;]，vetting 也要切分號，否則
        # 「上傳區/a.pdf;/etc/passwd」整串通過、再被拆出 /etc/passwd 夾帶。
        from agent_core.actor_google_tools import _vet_attachments
        good = os.path.expanduser("~/Downloads/小紅-uploads/2026-06-13/a.pdf")
        ok, msg = _vet_attachments(f"{good};/etc/passwd")
        self.assertFalse(ok)
        self.assertIn("/etc/passwd", msg)

    def test_vetting_uses_configured_upload_root(self):
        # Codex P2：env 覆寫上傳目錄時，該目錄下的檔要過、舊 hardcode 目錄要擋
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            from agent_core.actor_google_tools import _vet_attachments
            inside = os.path.join(d, "x.pdf")
            with mock.patch.dict(os.environ, {"RED_TELEGRAM_UPLOAD_DIR": d}, clear=False):
                ok, _ = _vet_attachments(inside)
                self.assertTrue(ok)
                # 舊 hardcode 路徑在覆寫後不該再被視為合法
                ok2, _ = _vet_attachments(os.path.expanduser("~/Downloads/小紅-uploads/y.pdf"))
                self.assertFalse(ok2)


class DelegationAndScopeTests(unittest.TestCase):
    def test_send_uses_delegated_service_as_actor(self):
        """send_gmail 必須拿到「委派到 gm」的 service，不是大王 OAuth。"""
        from agent_core.actor_google_tools import build_actor_google_tools
        tools = {t.__name__: t for t in build_actor_google_tools("gm@company.example", sa_file=_FAKE_SA)}

        captured = {}

        def fake_send(*a, **k):
            captured["get_service"] = k.get("get_service")
            return "✅ 已寄出"

        with mock.patch("agent_core.gmail_ops.send_gmail", side_effect=fake_send), \
                mock.patch("agent_core.google_auth.get_service_for_account") as gsfa:
            out = tools["send_gmail"]("x@y.com", "s", "b")
            self.assertEqual(out, "✅ 已寄出")
            # 觸發 get_service → 確認帶 subject=gm + send scope
            captured["get_service"]("gmail", "v1")
            _, kwargs = gsfa.call_args
            self.assertEqual(kwargs["subject"], "gm@company.example")
            self.assertIn("https://www.googleapis.com/auth/gmail.send", kwargs["scopes"])

    def test_scope_error_string_becomes_friendly_hint(self):
        from agent_core import actor_google_tools as agt
        tools = {t.__name__: t for t in agt.build_actor_google_tools("gm@company.example", sa_file=_FAKE_SA)}
        err = "('unauthorized_client: Client is unauthorized to retrieve access tokens', {})"
        with mock.patch("agent_core.gmail_ops.send_gmail", return_value=f"發信失敗：{err}"):
            out = tools["send_gmail"]("x@y.com", "s", "b")
        self.assertIn("尚未開通", out)
        self.assertIn("106147013660041858132", out)


class CalendarInviteTests(unittest.TestCase):
    def test_create_event_invites_whole_company_with_sendupdates_all(self):
        from agent_core import actor_google_tools as agt
        tools = {t.__name__: t for t in agt.build_actor_google_tools("gm@company.example", sa_file=_FAKE_SA)}
        sink: dict = {}
        fake_service = _FakeCalendarService(sink)
        with mock.patch("agent_core.google_auth.get_service_for_account", return_value=fake_service), \
                mock.patch.object(agt, "_company_emails", return_value=[e["email"] for e in _COMPANY]):
            out = tools["create_calendar_event"](
                "全公司會議", "2026-06-20T14:00:00+08:00", "2026-06-20T15:00:00+08:00",
                attendees="全公司",
            )
        self.assertIn("會議已建立", out)
        insert = sink["insert"]
        self.assertEqual(insert["calendarId"], "primary")
        self.assertEqual(insert["sendUpdates"], "all")
        emails = {a["email"] for a in insert["body"]["attendees"]}
        self.assertEqual(emails, {"gm@company.example", "sampledev@company.example", "cashier@company.example"})

    def test_create_event_no_attendees_no_notification(self):
        from agent_core import actor_google_tools as agt
        tools = {t.__name__: t for t in agt.build_actor_google_tools("gm@company.example", sa_file=_FAKE_SA)}
        sink: dict = {}
        with mock.patch("agent_core.google_auth.get_service_for_account",
                        return_value=_FakeCalendarService(sink)):
            tools["create_calendar_event"](
                "個人提醒", "2026-06-20T14:00:00+08:00", "2026-06-20T15:00:00+08:00",
            )
        self.assertEqual(sink["insert"]["sendUpdates"], "none")
        self.assertNotIn("attendees", sink["insert"]["body"])


class ActorSignatureTests(unittest.TestCase):
    """actor 寄信用自己的簽名檔，絕不套大王的 EMAIL_SIGNATURE（Owner/(Owner)）。"""

    def _build(self):
        from agent_core.actor_google_tools import build_actor_google_tools
        return {t.__name__: t for t in build_actor_google_tools("gm@company.example", sa_file=_FAKE_SA)}

    def _capture_append_fn(self, tools):
        captured = {}

        def fake_send(*a, **k):
            captured["append"] = k.get("append_signature_fn")
            return "✅ 已寄出"

        with mock.patch("agent_core.gmail_ops.send_gmail", side_effect=fake_send):
            tools["send_gmail"]("x@y.com", "主旨", "內文")
        return captured["append"]

    def test_send_uses_actor_signature_not_owner(self):
        tools = self._build()
        with mock.patch("agent_core.web_server.employee_registry.get_employee",
                        return_value={"name": "gm",
                                      "signature": "Best regards,\n\nUserS Chen  陳小明\nSales Manager"}):
            append = self._capture_append_fn(tools)
            out = append("內文")
        self.assertIn("UserS Chen  陳小明", out)
        self.assertIn("Sales Manager", out)
        self.assertNotIn("Owner", out)
        self.assertNotIn("(Owner)", out)
        self.assertNotIn("General Manager", out)

    def test_fallback_signature_is_minimal_not_owner(self):
        tools = self._build()
        with mock.patch("agent_core.web_server.employee_registry.get_employee",
                        return_value={"name": "gm"}):  # 沒設 signature
            append = self._capture_append_fn(tools)
            out = append("內文")
        self.assertIn("Gm", out)          # 名字 title-case fallback
        self.assertNotIn("Owner", out)
        self.assertNotIn("(Owner)", out)
        self.assertNotIn("JAIFUNG CORPORATION", out)  # 不亂編公司區塊

    def test_set_my_signature_persists_via_setter(self):
        tools = self._build()
        self.assertIn("set_my_signature", tools)
        with mock.patch("agent_core.web_server.employee_registry.set_employee_signature") as setter:
            out = tools["set_my_signature"]("Best regards,\n\nUserS")
        setter.assert_called_once_with("gm@company.example", "Best regards,\n\nUserS")
        self.assertIn("已更新", out)


class SendGmailAsTests(unittest.TestCase):
    """send_gmail_as —— 給背景 dispatcher 這類非互動路徑直接呼叫的獨立函式，
    不需要先建整包 build_actor_google_tools 工具清單。跟 build_actor_google_tools
    內的 send_gmail 走同一套委派/簽名邏輯。"""

    def test_calls_gmail_ops_send_gmail_with_valid_signature(self):
        """回歸（2026-07-02 實際踩到）：send_gmail_as 忘了轉傳 attachments，
        gmail_ops.send_gmail 是 keyword-only 必填參數，daily_production_alert/
        email_pending_tracker 手動觸發時全部收件人 TypeError 寄信失敗。
        這裡用 autospec=True——一般的 mock.patch 接受任何呼叫簽名，不會抓到
        漏傳必填參數這類錯誤，必須 autospec 讓 mock 照真實函式簽名驗證。"""
        from agent_core.actor_google_tools import send_gmail_as
        with mock.patch(
            "agent_core.gmail_ops.send_gmail", autospec=True, return_value="✅ 已寄出",
        ), mock.patch("agent_core.google_auth.get_service_for_account"):
            out = send_gmail_as(
                "owner@company.example", "owner@company.example", "subj", "body",
                sa_file=_FAKE_SA,
            )
        self.assertEqual(out, "✅ 已寄出")

    def test_uses_delegated_service_as_actor(self):
        from agent_core.actor_google_tools import send_gmail_as
        captured = {}

        def fake_send(*a, **k):
            captured["get_service"] = k.get("get_service")
            return "✅ 已寄出"

        with mock.patch("agent_core.gmail_ops.send_gmail", side_effect=fake_send), \
                mock.patch("agent_core.google_auth.get_service_for_account") as gsfa:
            out = send_gmail_as(
                "owner@company.example", "owner@company.example", "subj", "body",
                sa_file=_FAKE_SA,
            )
            self.assertEqual(out, "✅ 已寄出")
            captured["get_service"]("gmail", "v1")
            _, kwargs = gsfa.call_args
            self.assertEqual(kwargs["subject"], "owner@company.example")
            self.assertIn("https://www.googleapis.com/auth/gmail.send", kwargs["scopes"])

    def test_self_addressed_to_matches_actor(self):
        """典型用法：actor_email 跟 to 是同一個地址（自己寄給自己）。"""
        from agent_core.actor_google_tools import send_gmail_as
        captured = {}

        def fake_send(to, subject, body, **k):
            captured["to"] = to
            return "✅ 已寄出"

        with mock.patch("agent_core.gmail_ops.send_gmail", side_effect=fake_send), \
                mock.patch("agent_core.google_auth.get_service_for_account"):
            send_gmail_as(
                "gm@company.example", "gm@company.example", "subj", "body",
                sa_file=_FAKE_SA,
            )
        self.assertEqual(captured["to"], "gm@company.example")

    def test_empty_actor_email_rejected_without_calling_api(self):
        from agent_core.actor_google_tools import send_gmail_as
        with mock.patch("agent_core.gmail_ops.send_gmail") as send:
            out = send_gmail_as("", "x@y.com", "s", "b", sa_file=_FAKE_SA)
        self.assertIn("❌", out)
        send.assert_not_called()

    def test_missing_sa_file_rejected_without_calling_api(self):
        """sa_file="" 且無法從環境解析出真的金鑰路徑時要拒絕——用主 checkout
        跑測試時 var/state 可能真的有一把 SA 金鑰，_sa_file() 要明確 mock 成
        空字串，不能依賴環境剛好沒設定（見 feedback_tests_immune_to_live_
        runtime_state）。"""
        from agent_core.actor_google_tools import send_gmail_as
        with mock.patch("agent_core.gmail_ops.send_gmail") as send, \
                mock.patch("agent_core.actor_google_tools._sa_file", return_value=""):
            out = send_gmail_as("owner@company.example", "owner@company.example", "s", "b", sa_file="")
        self.assertIn("❌", out)
        send.assert_not_called()

    def test_scope_error_becomes_friendly_hint(self):
        from agent_core.actor_google_tools import send_gmail_as
        err = "('unauthorized_client: Client is unauthorized to retrieve access tokens', {})"
        with mock.patch("agent_core.gmail_ops.send_gmail", return_value=f"發信失敗：{err}"), \
                mock.patch("agent_core.google_auth.get_service_for_account"):
            out = send_gmail_as(
                "owner@company.example", "owner@company.example", "s", "b", sa_file=_FAKE_SA
            )
        self.assertIn("尚未開通", out)

    def test_uses_actor_own_signature_not_owner(self):
        """append_signature_fn 是延遲呼叫的 closure（gmail_ops.send_gmail 內部
        才會用到），一定要在 mock.patch 的 with 區塊「裡面」呼叫它驗證——區塊
        外呼叫會打到真的 employee_registry（主 checkout 有真實員工資料，會讀到
        UserS 的真實簽名檔而非這裡故意留空模擬的 fallback，讓測試失去意義）。"""
        from agent_core.actor_google_tools import send_gmail_as
        captured = {}

        def fake_send(*a, **k):
            captured["append_signature_fn"] = k.get("append_signature_fn")
            return "✅ 已寄出"

        with mock.patch("agent_core.gmail_ops.send_gmail", side_effect=fake_send), \
                mock.patch("agent_core.google_auth.get_service_for_account"), \
                mock.patch(
                    "agent_core.web_server.employee_registry.get_employee",
                    return_value={"name": "UserS", "signature": ""},
                ):
            send_gmail_as(
                "gm@company.example", "gm@company.example", "s", "body 內容",
                sa_file=_FAKE_SA,
            )
            signed = captured["append_signature_fn"]("body 內容")
        self.assertIn("UserS", signed)
        self.assertNotIn("Owner", signed)


class ProxyGuardTests(unittest.TestCase):
    def test_actor_scoped_tools_never_proxied(self):
        from agent_core.tool_proxy import _should_proxy_tool

        def send_gmail():
            pass

        send_gmail._actor_scoped = True
        # 即使 mode=all 也不能 proxy（by-name 會在 worker 查到大王版本）
        self.assertFalse(_should_proxy_tool(send_gmail, "all"))


if __name__ == "__main__":
    unittest.main()
