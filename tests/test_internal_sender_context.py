"""內部人物背景注入 LLM prompt 的回歸測試。

2026-07-08 事故：ponder 把越南廠倉庫主管井戶良枝（warehouse-mgr@company.example，
日文名字）誤判成「日本客戶」，發出要求賠償協商的急件警報。根因是
ponder / summarize_inbox 的 prompt 只有原始信件摘要、沒有任何身分錨點。
這裡鎖住三件事：
  1. dept_rules.llm_internal_context() 含網域硬規則與井戶良枝註記
  2. ponder prompt 有帶這個背景區塊
  3. summarize_inbox prompt 有帶這個背景區塊
"""
import unittest
from types import SimpleNamespace

from agent_core import daemon_ponder
from agent_core.dept_rules import KNOWN_SENDER_NOTES, llm_internal_context
from agent_core.gmail_ops import summarize_inbox


class LlmInternalContextTests(unittest.TestCase):
    def test_domain_rule_and_yoshie_note_present(self):
        ctx = llm_internal_context()
        self.assertIn("@company.example", ctx)
        self.assertIn("不是客戶", ctx)
        self.assertIn("warehouse-mgr@company.example", ctx)
        self.assertIn("井戶良枝", ctx)
        self.assertIn("倉庫", ctx)

    def test_dept_senders_listed(self):
        ctx = llm_internal_context()
        self.assertIn("owner@company.example", ctx)
        self.assertIn("twsales@company.example", ctx)

    def test_known_sender_notes_all_rendered(self):
        ctx = llm_internal_context()
        for email, note in KNOWN_SENDER_NOTES.items():
            self.assertIn(email, ctx)
            self.assertIn(note, ctx)


class PonderPromptContextTests(unittest.TestCase):
    def test_ponder_prompt_carries_internal_context(self):
        captured = {}

        def fake_generate(*args, **kwargs):
            captured["prompt"] = kwargs["contents"][0]
            return SimpleNamespace(text="(無)")

        def raise_no_calendar(*args, **kwargs):
            raise RuntimeError("no calendar in test")

        daemon_ponder.task_ponder(
            in_working_hours=lambda: True,
            work_hour_start=8,
            work_hour_end=19,
            load_state=lambda: {},
            summarize_inbox=lambda hours: "[假摘要] 井戶良枝 品質異常 補運",
            get_service=raise_no_calendar,
            search_gmail=lambda q: "找不到符合的郵件",
            gemini_generate=fake_generate,
            gemini_model="fake-model",
            extract_fresh_insights_fn=daemon_ponder.extract_fresh_insights,
            remember_ponder_insights_fn=lambda seen, fresh: None,
            notify=lambda **kwargs: self.fail("(無) 不應觸發通知"),
        )
        self.assertIn("內部人物背景", captured["prompt"])
        self.assertIn("井戶良枝", captured["prompt"])
        self.assertIn("不是客戶", captured["prompt"])
        # 背景區塊必須在 untrusted 訊號之前（可信事實先立錨）
        self.assertLess(
            captured["prompt"].index("內部人物背景"),
            captured["prompt"].index("untrusted-signals"),
        )


class SummarizeInboxContextTests(unittest.TestCase):
    def test_summary_prompt_carries_internal_context(self):
        captured = {}
        msg_list = {"messages": [{"id": "m1"}]}
        msg_full = {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "玻纖鞋頭品質異常"},
                    {"name": "From", "value": '"井戶良枝" <warehouse-mgr@company.example>'},
                ]
            }
        }

        class FakeMessages:
            def list(self, **kwargs):
                return SimpleNamespace(execute=lambda: msg_list)

            def get(self, **kwargs):
                return SimpleNamespace(execute=lambda: msg_full)

        fake_service = SimpleNamespace(
            users=lambda: SimpleNamespace(messages=lambda: FakeMessages())
        )

        def fake_generate(*args, **kwargs):
            captured["prompt"] = kwargs["contents"][0]
            return SimpleNamespace(text="整理完成")

        out = summarize_inbox(
            2,
            get_service=lambda *args: fake_service,
            extract_body_fn=lambda payload: "需要補運 10,200 雙",
            gemini_generate_fn=fake_generate,
            gemini_model="fake-model",
        )
        self.assertIn("內部人物背景", captured["prompt"])
        self.assertIn("井戶良枝", captured["prompt"])
        self.assertIn("不是客戶", captured["prompt"])
        self.assertIn("整理完成", out)


if __name__ == "__main__":
    unittest.main()
