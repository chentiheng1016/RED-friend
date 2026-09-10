"""Focused tests for recently extracted internal email modules.

These modules were split out during the structure cleanup, but the original
test suite mostly exercised them indirectly through larger imports. This file
adds direct coverage for the new module boundaries so future refactors have
better guardrails.
"""
import base64
import json
import os
import sys
import types
import unittest
from unittest import mock


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from agent_core import internal_emails_extract as ie_extract
from agent_core import internal_emails_orchestrator as ie_orchestrator
from agent_core import internal_emails_preview as ie_preview
from agent_core import tool_registry_catalog as catalog
from agent_core import tool_registry


def _gmail_text_part(text: str, mime_type: str = "text/plain") -> dict:
    encoded = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")
    return {"mimeType": mime_type, "body": {"data": encoded}}


def _message(msg_id: str, ts_ms: int, sender: str, subject: str, body: str) -> dict:
    return {
        "id": msg_id,
        "internalDate": str(ts_ms),
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "To", "value": "team@company.example"},
            ],
            **_gmail_text_part(body),
        },
    }


def _header(msg: dict, name: str) -> str:
    for header in msg.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


class InternalEmailPreviewTests(unittest.TestCase):
    def test_preview_builds_human_readable_report(self):
        service = mock.Mock()
        service.users.return_value.threads.return_value.list.return_value.execute.return_value = {
            "threads": [{"id": "t1"}]
        }
        service.users.return_value.threads.return_value.list_next.return_value = None
        service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
            "messages": [
                {
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Alice <alice@company.example>"},
                            {"name": "Subject", "value": "BrandA sample update"},
                        ]
                    }
                }
            ]
        }
        logger = types.SimpleNamespace(debug=lambda *args, **kwargs: None)

        report = ie_preview.preview_internal_ingest(
            days_back=30,
            max_threads=100,
            get_service=lambda *_args: service,
            header_fn=_header,
            classify_dept=lambda sender, subject: ("業務", ["業務"]),
            classify_direction=lambda sender: "internal",
            detect_brands=lambda subject: ["BrandA"],
            should_exclude=lambda sender, subject: False,
            dept_rules={"業務": {}, "開發": {}},
            brand_keywords=["BrandA", "BrandB"],
            logger=logger,
        )

        self.assertIn("Internal Email Ingest 乾跑報表", report)
        self.assertIn("業務", report)
        self.assertIn("BrandA", report)
        self.assertIn("方向：internal 1｜external 0", report)


class InternalEmailExtractTests(unittest.TestCase):
    def test_row_from_thread_marks_safety_blocked_threads(self):
        thread = {
            "id": "t1",
            "messages": [
                _message("m1", 1_700_000_000_000, "alice@company.example", "Need update", "hello"),
                _message("m2", 1_700_000_100_000, "bob@company.example", "Need update", "follow up"),
            ],
        }

        row = ie_extract.row_from_thread(
            thread,
            {"_safety_blocked": True},
            header_fn=_header,
            should_exclude=lambda *_args: False,
            classify_dept=lambda *_args: ("業務", ["業務", "客服"]),
            classify_direction=lambda *_args: "internal",
            detect_brands=lambda *_args: ["BrandA"],
        )

        self.assertEqual(row["summary"], ie_extract.SAFETY_BLOCKED_MARKER)
        self.assertEqual(row["state"], "safety_blocked")
        self.assertIn("safety_blocked", row["topic_tags"])
        self.assertIn("BrandA", row["brands"])

    def test_extract_prompt_declares_promised_dates(self):
        """承諾交期欄位（delay radar 的資料基礎）必須同時出現在 schema 行、
        欄位定義與範例輸出 — 三處缺一就會教壞模型。"""
        prompt = ie_extract.build_extract_prompt("subj", "body")
        self.assertGreaterEqual(prompt.count("promised_dates"), 3)
        # 範例的 entities 順序要跟 schema 行一致：dates_mentioned → promised_dates
        self.assertIn('"dates_mentioned": [], "promised_dates": []', prompt)
        # 明確日期才收 — 防「隨新訂單」這種無日期短語進欄位
        self.assertIn("必須含明確日期字樣", prompt)

    def test_row_from_thread_passes_promised_dates_through(self):
        """entities 不做 key 白名單 — 新欄位要原樣進 entities_json。"""
        thread = {
            "id": "t1",
            "messages": [
                _message("m1", 1_700_000_000_000, "alice@company.example", "PO 交期", "body"),
            ],
        }
        row = ie_extract.row_from_thread(
            thread,
            {
                "summary": "JF0P123 確認 6/30 出貨",
                "topic_tags": ["PO確認"],
                "state": "進行中",
                "entities": {
                    "po_numbers": ["JF0P123"],
                    "promised_dates": ["6/30 出貨"],
                },
            },
            header_fn=_header,
            should_exclude=lambda *_args: False,
            classify_dept=lambda *_args: ("業務", ["業務"]),
            classify_direction=lambda *_args: "internal",
            detect_brands=lambda *_args: [],
        )
        ents = json.loads(row["entities_json"])
        self.assertEqual(ents["promised_dates"], ["6/30 出貨"])
        self.assertEqual(ents["po_numbers"], ["JF0P123"])

    def test_gemini_extract_falls_back_to_flash_on_prohibited_block(self):
        blocked_resp = types.SimpleNamespace(
            text="",
            prompt_feedback=types.SimpleNamespace(block_reason=types.SimpleNamespace(name="PROHIBITED_CONTENT")),
        )
        ok_resp = types.SimpleNamespace(text='{"summary":"ok","topic_tags":[],"state":"進行中","entities":{}}')
        calls = []

        def fake_generate(*, model, contents):
            calls.append(model)
            return blocked_resp if model == ie_extract.INGEST_MODEL else ok_resp

        logger = types.SimpleNamespace(debug=lambda *args, **kwargs: None)
        out = ie_extract.gemini_extract(
            {"thread_id": "t1", "subject": "s", "combined_text": "b"},
            gemini_generate=fake_generate,
            logger=logger,
            timeout_retry=1,
        )

        self.assertEqual(out["summary"], "ok")
        self.assertEqual(calls[:2], [ie_extract.INGEST_MODEL, "gemini-2.5-flash"])

    def test_gemini_extract_uses_short_body_fallback_before_safety_marker(self):
        blocked_resp = types.SimpleNamespace(
            text="",
            prompt_feedback=types.SimpleNamespace(block_reason=types.SimpleNamespace(name="PROHIBITED_CONTENT")),
        )
        ok_resp = types.SimpleNamespace(text='{"summary":"short-ok","topic_tags":[],"state":"進行中","entities":{}}')
        long_body = "x" * 900
        seen_prompts = []

        def fake_generate(*, model, contents):
            prompt = contents[0]
            seen_prompts.append((model, len(prompt)))
            if model == ie_extract.INGEST_MODEL and len(prompt) < len(ie_extract.build_extract_prompt("s", long_body)):
                return ok_resp
            return blocked_resp

        logger = types.SimpleNamespace(debug=lambda *args, **kwargs: None)
        out = ie_extract.gemini_extract(
            {"thread_id": "t1", "subject": "s", "combined_text": long_body},
            gemini_generate=fake_generate,
            logger=logger,
            timeout_retry=1,
        )

        self.assertEqual(out["summary"], "short-ok")
        self.assertGreaterEqual(len(seen_prompts), 3)


class ThreadNeedsRefreshTests(unittest.TestCase):
    """重抽判定：Gmail internalDate(epoch ms) vs parquet last_message_date('%Y-%m-%d' 日粒度)。"""

    @staticmethod
    def _ms(y, m, d, hh=12):
        import datetime as _dt
        return int(_dt.datetime(y, m, d, hh).timestamp() * 1000)

    def test_newer_day_triggers_refresh(self):
        self.assertTrue(ie_orchestrator.thread_needs_refresh(
            self._ms(2026, 7, 5), 3, "2026-07-01", 3))

    def test_same_day_same_count_no_refresh(self):
        self.assertFalse(ie_orchestrator.thread_needs_refresh(
            self._ms(2026, 7, 1, hh=18), 3, "2026-07-01", 3))

    def test_same_day_new_message_caught_by_count(self):
        # 同一天內又來新信 → 日期字串看不出來，靠訊息數抓
        self.assertTrue(ie_orchestrator.thread_needs_refresh(
            self._ms(2026, 7, 1, hh=18), 4, "2026-07-01", 3))

    def test_tolerance_absorbs_clock_skew_just_past_midnight(self):
        # 記錄日隔天 00:30（容差 1h 內）→ 視為時鐘偏移、不重抽
        self.assertFalse(ie_orchestrator.thread_needs_refresh(
            self._ms(2026, 7, 2, hh=0) + 30 * 60 * 1000, 3, "2026-07-01", 3))

    def test_beyond_tolerance_refreshes(self):
        # 記錄日隔天 02:00（超過 1h 容差）→ 重抽
        self.assertTrue(ie_orchestrator.thread_needs_refresh(
            self._ms(2026, 7, 2, hh=2), 3, "2026-07-01", 3))

    def test_bad_parquet_date_or_count_refreshes(self):
        # parquet 資料解析不了 → 寧可重抽也不要凍結
        self.assertTrue(ie_orchestrator.thread_needs_refresh(
            self._ms(2026, 7, 1), 3, "", 3))
        self.assertTrue(ie_orchestrator.thread_needs_refresh(
            self._ms(2026, 7, 1), 3, "2026-07-01", None))

    def test_missing_gmail_ts_no_refresh(self):
        self.assertFalse(ie_orchestrator.thread_needs_refresh(0, 3, "2026-07-01", 3))


class InternalEmailOrchestratorTests(unittest.TestCase):
    def test_processed_thread_with_new_message_gets_reextracted(self):
        """finding: thread 一經抽取永不更新 → daily delta 要偵測新訊息並重抽覆蓋。"""
        import datetime as _dt
        appended = []
        reprocessed = []
        newer_ms = int(_dt.datetime(2026, 7, 10, 9).timestamp() * 1000)

        result = ie_orchestrator.ingest_internal_emails(
            days_back=3,
            max_threads=10,
            parallel_workers=1,
            flush_every=5,
            retry_failed=False,
            ensure_dir=lambda: None,
            load_id_set=lambda path: {"t-stale", "t-fresh"} if path.endswith("processed.json") else set(),
            save_id_set=lambda path, values: None,
            processed_path="/tmp/processed.json",
            failed_path="/tmp/failed.json",
            parquet_path="/tmp/emails.parquet",
            get_service=lambda *_args: object(),
            fetch_thread_ids_fn=lambda svc, days_back, max_threads: ["t-stale", "t-fresh"],
            process_thread_id_fn=lambda svc, tid: (
                reprocessed.append(tid) or (tid, {"thread_id": tid}, True, "")),
            append_parquet_fn=lambda rows: appended.extend(rows),
            logger=types.SimpleNamespace(debug=lambda *args, **kwargs: None),
            # t-stale：parquet 停在 7/1、1 封；Gmail 端 7/10、2 封 → 要重抽
            # t-fresh：parquet 與 Gmail 一致 → 不重抽
            fetch_thread_latest_ts_fn=lambda svc, tid: (
                (newer_ms, 2) if tid == "t-stale"
                else (int(_dt.datetime(2026, 7, 1, 9).timestamp() * 1000), 1)),
            load_thread_freshness_fn=lambda: {
                "t-stale": ("2026-07-01", 1),
                "t-fresh": ("2026-07-01", 1),
            },
        )

        self.assertEqual(reprocessed, ["t-stale"])                 # 只重抽有新信的
        self.assertEqual([r["thread_id"] for r in appended], ["t-stale"])  # 覆蓋舊快照
        self.assertIn("處理 1 個 thread", result)

    def test_processed_thread_absent_from_parquet_not_refreshed(self):
        """processed 但不在 parquet（被排除規則擋掉）→ 不重抽、也不炸。"""
        reprocessed = []
        result = ie_orchestrator.ingest_internal_emails(
            days_back=3,
            max_threads=10,
            parallel_workers=1,
            flush_every=5,
            retry_failed=False,
            ensure_dir=lambda: None,
            load_id_set=lambda path: {"t-excluded"} if path.endswith("processed.json") else set(),
            save_id_set=lambda path, values: None,
            processed_path="/tmp/processed.json",
            failed_path="/tmp/failed.json",
            parquet_path="/tmp/emails.parquet",
            get_service=lambda *_args: object(),
            fetch_thread_ids_fn=lambda svc, days_back, max_threads: ["t-excluded"],
            process_thread_id_fn=lambda svc, tid: reprocessed.append(tid) or (tid, None, True, ""),
            append_parquet_fn=lambda rows: None,
            logger=types.SimpleNamespace(debug=lambda *args, **kwargs: None),
            fetch_thread_latest_ts_fn=lambda svc, tid: (0, 0),
            load_thread_freshness_fn=lambda: {},
        )
        self.assertEqual(reprocessed, [])
        self.assertIn("都已處理過", result)

    def test_ingest_clears_stale_failed_and_reports_when_nothing_left(self):
        saved = []

        result = ie_orchestrator.ingest_internal_emails(
            days_back=10,
            max_threads=10,
            parallel_workers=1,
            flush_every=5,
            retry_failed=True,
            ensure_dir=lambda: None,
            load_id_set=lambda path: {"t1"} if path.endswith("processed.json") else {"t1"},
            save_id_set=lambda path, values: saved.append((path, set(values))),
            processed_path="/tmp/processed.json",
            failed_path="/tmp/failed.json",
            parquet_path="/tmp/emails.parquet",
            get_service=lambda *_args: object(),
            fetch_thread_ids_fn=lambda svc, days_back, max_threads: ["t1"],
            process_thread_id_fn=lambda svc, tid: (tid, None, True, ""),
            append_parquet_fn=lambda rows: None,
            logger=types.SimpleNamespace(debug=lambda *args, **kwargs: None),
        )

        self.assertIn("所有 1 個 thread 都已處理過", result)
        self.assertEqual(saved, [("/tmp/failed.json", set())])

    def test_vectorize_skips_existing_and_counts_batch_failures(self):
        class _FakeDataFrame:
            def __init__(self, rows):
                self._rows = rows
                self.empty = False
                self.iloc = _FakeILoc(rows)

            def __len__(self):
                return len(self._rows)

        class _FakeILoc:
            def __init__(self, rows):
                self.rows = rows

            def __getitem__(self, item):
                start = item.start or 0
                stop = item.stop or len(self.rows)
                batch = self.rows[start:stop]
                return types.SimpleNamespace(to_dict=lambda orient: list(batch))

        rows = [
            {"thread_id": "keep-1"},
            {"thread_id": "skip-me"},
            {"thread_id": "fail-1"},
        ]
        fake_df = _FakeDataFrame(rows)
        upserted = []
        warned = []

        fake_col = types.SimpleNamespace(
            get=lambda **kwargs: {"ids": ["skip-me"]}
        )

        with mock.patch("pandas.read_parquet", return_value=fake_df), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("agent_core.memory._get_memory_collection", return_value=fake_col):
            result = ie_orchestrator.vectorize_internal_lake(
                batch_size=2,
                skip_existing=True,
                parquet_path="/tmp/emails.parquet",
                upsert_to_chroma_fn=lambda batch: (
                    (_ for _ in ()).throw(RuntimeError("boom")) if batch[0]["thread_id"] == "fail-1"
                    else upserted.append([row["thread_id"] for row in batch])
                ),
                logger=types.SimpleNamespace(
                    debug=lambda *args, **kwargs: None,
                    warning=lambda *args, **kwargs: warned.append(args),
                ),
            )

        self.assertIn("新 upsert: 1", result)
        self.assertIn("已存在跳過: 1", result)
        self.assertIn("失敗（重跑可再試）: 1", result)
        self.assertEqual(upserted, [["keep-1"]])
        self.assertEqual(len(warned), 1)

    def test_sample_internal_ingest_formats_summary(self):
        rows = [
            {
                "primary_dept": "業務",
                "direction": "internal",
                "subject": "Sample subject",
                "date": "2026-04-24",
                "sender": "alice@company.example",
                "summary": "sample summary",
                "entities_json": '{"customers":["ACME"],"products":["SKU-1"],"po_numbers":["PO-7"],"amounts":["10"],"actions":["follow up"]}',
                "topic_tags": '["PO確認","追蹤"]',
            }
        ]

        report = ie_orchestrator.sample_internal_ingest(
            sample_size=1,
            days_back=7,
            get_service=lambda *_args: object(),
            fetch_thread_ids_fn=lambda svc, days_back, max_threads: ["t1"],
            process_thread_id_fn=lambda svc, tid: (tid, rows[0], True, ""),
            shuffle_fn=lambda items: None,
        )

        self.assertIn("📋 Sample 抽取結果", report)
        self.assertIn("Sample subject", report)
        self.assertIn("客戶: ACME", report)
        self.assertIn("PO#: PO-7", report)
        self.assertIn("開始全量", report)


class FreshnessDefaultImplTests(unittest.TestCase):
    """internal_emails 提供給 orchestrator 的預設注入實作。"""

    def test_fetch_thread_latest_ts_returns_max_and_count(self):
        from agent_core import internal_emails as ie
        svc = mock.Mock()
        svc.users.return_value.threads.return_value.get.return_value.execute.return_value = {
            "messages": [{"internalDate": "100"}, {"internalDate": "300"}, {"internalDate": "200"}]
        }
        self.assertEqual(ie._fetch_thread_latest_ts(svc, "t1"), (300, 3))
        svc.users.return_value.threads.return_value.get.assert_called_with(
            userId="me", id="t1", format="minimal")

    def test_load_thread_freshness_maps_parquet_columns(self):
        import tempfile

        import pandas as pd

        from agent_core import internal_emails as ie
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "emails.parquet")
            pd.DataFrame([
                {"thread_id": "t1", "last_message_date": "2026-07-01", "message_count": 3,
                 "subject": "x"},
                {"thread_id": "t2", "last_message_date": "2026-06-15", "message_count": 1,
                 "subject": "y"},
            ]).to_parquet(path)
            with mock.patch.object(ie, "_INTERNAL_PARQUET", path):
                fresh = ie._load_thread_freshness()
        self.assertEqual(fresh["t1"], ("2026-07-01", 3))
        self.assertEqual(fresh["t2"], ("2026-06-15", 1))

    def test_load_thread_freshness_missing_file_returns_empty(self):
        from agent_core import internal_emails as ie
        with mock.patch.object(ie, "_INTERNAL_PARQUET", "/nonexistent/emails.parquet"):
            self.assertEqual(ie._load_thread_freshness(), {})


class ToolRegistryCatalogTests(unittest.TestCase):
    def test_build_builtin_tools_keeps_extra_tool(self):
        def dummy_tool():
            return "ok"

        tools = catalog.build_builtin_tools(extra_tools=[dummy_tool])
        names = [tool.__name__ for tool in tools]

        self.assertIn("dummy_tool", names)
        self.assertGreaterEqual(len(tools), len(catalog.BASE_BUILTIN_TOOLS) + 1)


class ToolRegistrySchemaValidationTests(unittest.TestCase):
    def test_schema_validation_skips_when_gemini_client_exits(self):
        def dummy_tool():
            return "ok"

        with mock.patch("agent_core.gemini_client._get_gemini_client", side_effect=SystemExit(1)):
            tools, broken = tool_registry._validate_tool_schemas([dummy_tool])

        self.assertEqual(tools, [dummy_tool])
        self.assertEqual(broken, [])


class ToolRegistryReloadTests(unittest.TestCase):
    def test_reload_skills_mutates_tools_list_in_place_and_rebuilds_chat(self):
        old_tools_list = tool_registry.tools_list

        def old_skill():
            return "old"

        old_skill._is_skill = True

        def new_skill():
            return "new"

        new_skill._is_skill = True

        self.assertIs(tool_registry.tools_list, old_tools_list)
        original_contents = list(tool_registry.tools_list)
        tool_registry.tools_list.append(old_skill)
        try:
            with mock.patch.object(tool_registry, "_load_skills_from_dir", return_value=([new_skill], [{"name": "new_skill"}])), \
                 mock.patch.object(tool_registry._skills_mod, "_loaded_skills_info", []), \
                 mock.patch.object(tool_registry, "_build_chat_fn", lambda: "rebuilt-chat"), \
                 mock.patch.dict(tool_registry.chat_state, {"chat": "old-chat", "turns": 5}, clear=False):
                msg = tool_registry.reload_skills()
                self.assertEqual(tool_registry.chat_state["chat"], "rebuilt-chat")
                self.assertEqual(tool_registry.chat_state["turns"], 0)

            self.assertIs(tool_registry.tools_list, old_tools_list)
            self.assertNotIn(old_skill, tool_registry.tools_list)
            self.assertIn(new_skill, tool_registry.tools_list)
            self.assertIn("已重新載入 skills/", msg)
        finally:
            tool_registry.tools_list[:] = original_contents

    def test_reload_skills_without_builder_reports_but_still_refreshes_tools(self):
        old_tools_list = tool_registry.tools_list

        def old_skill():
            return "old"

        old_skill._is_skill = True

        def new_skill():
            return "new"

        new_skill._is_skill = True

        original_contents = list(tool_registry.tools_list)
        original_builder = tool_registry._build_chat_fn
        tool_registry.tools_list.append(old_skill)
        tool_registry._build_chat_fn = None
        try:
            with mock.patch.object(tool_registry, "_load_skills_from_dir", return_value=([new_skill], [{"name": "new_skill"}])):
                msg = tool_registry.reload_skills()

            self.assertIs(tool_registry.tools_list, old_tools_list)
            self.assertNotIn(old_skill, tool_registry.tools_list)
            self.assertIn(new_skill, tool_registry.tools_list)
            self.assertIn("build_chat_fn 尚未注入", msg)
        finally:
            tool_registry.tools_list[:] = original_contents
            tool_registry._build_chat_fn = original_builder


if __name__ == "__main__":
    unittest.main(verbosity=2)
