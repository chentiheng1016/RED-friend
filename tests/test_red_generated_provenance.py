"""小紅自產內容的出處標記（X-RED-Generated）與 ingest 端排除。

背景：排程報表由小紅產出、寄給各部門，然後三條 ingest 路徑會把它當一般公司信
吃回來（gmail_sync → ChromaDB、email lake、internal_emails）。報表是從 lake /
Drive 算出來的**衍生品**，再吃回去等於讓它跟原始資料同級 —— 錯的數字下一輪會
被檢索到、被覆述。dispatcher 又是讓員工「自己寄給自己」（冒名自寄），From 就是
真人地址，所以光看寄件者永遠分不出來，非得有個信頭標記不可。

這個檔涵蓋整條迴路：
  寄件端 —— header 有沒有寫進去、CRLF 注入、中文任務名
  gmail_sync —— thread 判定規則（全部帶標記才算）
  email lake —— 自產信不進 lake
  internal_emails —— 自產 thread 不進 parquet
  rag_gateway —— 檢索預設濾掉，且**不能誤殺沒有這個欄位的存量資料**
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_core import gmail_ops  # noqa: E402


def _msg(headers: dict) -> dict:
    """組一個 Gmail API 形狀的 message（只有 headers 有意義）。"""
    return {"payload": {"headers": [{"name": k, "value": v} for k, v in headers.items()]}}


# ── 寄件端：header 寫入 ──────────────────────────────────────────────

class SendGmailProvenanceHeaderTests(unittest.TestCase):
    def _send(self, **kwargs):
        built = {}

        def fake_build_mime(body, attachments, html=False, html_alt=""):
            from email.mime.text import MIMEText
            msg = MIMEText("x", "plain", "utf-8")
            built["msg"] = msg
            return msg

        indexed = {}

        def fake_index(text, source, metadata=None):
            indexed["metadata"] = metadata or {}

        res = gmail_ops.send_gmail(
            "a@x.com", "主旨", "內文",
            cc="", bcc="", attachments="",
            get_service=lambda *a: mock.Mock(),
            append_signature_fn=lambda b: b,
            build_mime_fn=fake_build_mime,
            split_paths_fn=lambda s: [],
            index_memory_fn=fake_index,
            **kwargs,
        )
        return res, built["msg"], indexed["metadata"]

    def test_generated_by_writes_header(self):
        _res, msg, _meta = self._send(generated_by="dispatcher:生產日報")
        self.assertIn(gmail_ops.RED_GENERATED_HEADER, msg)

    def test_without_generated_by_no_header(self):
        """一般人寄的信不能被標記——否則真信會被 ingest 濾掉。"""
        _res, msg, _meta = self._send()
        self.assertNotIn(gmail_ops.RED_GENERATED_HEADER, msg)

    def test_chinese_task_name_serialises(self):
        """中文任務名要能序列化（stdlib 會自動 RFC 2047 編碼，這裡把它釘住）。"""
        _res, msg, _meta = self._send(generated_by="dispatcher:生產日報")
        self.assertIn(b"X-RED-Generated", msg.as_bytes())

    def test_crlf_in_task_name_does_not_break_sending(self):
        """任務名帶 CRLF 時仍要寄得出去，而且不能多出信頭。

        未消毒時 stdlib 會在 as_bytes() 丟 HeaderParseError（它自己擋掉了真正
        的 header injection），但那個例外會被 send_gmail 的 except 接住變成
        「發信失敗」—— 也就是整份報表**寄不出去**。所以這裡斷言的是「有寄出」，
        那才是沒消毒時真正會壞掉的東西。
        """
        res, msg, _meta = self._send(
            generated_by="rpt\r\nBcc: attacker@evil.com\r\nX-Spoof: 1",
        )
        self.assertIn("已寄出", res)
        self.assertIsNone(msg.get("Bcc"))
        self.assertIsNone(msg.get("X-Spoof"))
        value = msg[gmail_ops.RED_GENERATED_HEADER]
        self.assertNotIn("\r", value)
        self.assertNotIn("\n", value)

    def test_empty_after_sanitising_still_marks(self):
        """標籤被清成空字串時仍要留下標記本身（偵測看的是「在不在」）。"""
        _res, msg, _meta = self._send(generated_by="\r\n\t ")
        self.assertEqual(msg[gmail_ops.RED_GENERATED_HEADER], "1")

    def test_memory_index_records_provenance(self):
        """寄出當下就寫記憶庫的快捷路徑也要帶旗標。"""
        _res, _msg, meta = self._send(generated_by="dispatcher:日報")
        self.assertTrue(meta["generated_by_red"])

    def test_memory_index_marks_human_mail_as_not_generated(self):
        _res, _msg, meta = self._send()
        self.assertFalse(meta["generated_by_red"])


# ── gmail_sync：thread 判定 ─────────────────────────────────────────

class GmailSyncThreadDetectionTests(unittest.TestCase):
    def setUp(self):
        from agent_core.ingest import gmail_sync
        self.gs = gmail_sync

    def test_all_messages_marked_is_generated(self):
        msgs = [_msg({"X-RED-Generated": "dispatcher:日報"})]
        self.assertTrue(self.gs._thread_is_red_generated(msgs))

    def test_human_reply_in_thread_keeps_it_indexed(self):
        """有真人回覆就照常索引——誤殺同事的討論比漏放一份報表嚴重。"""
        msgs = [
            _msg({"X-RED-Generated": "dispatcher:日報"}),
            _msg({"From": "someone@company.example"}),
        ]
        self.assertFalse(self.gs._thread_is_red_generated(msgs))

    def test_header_name_is_case_insensitive(self):
        """Gmail 回傳的信頭大小寫不保證。"""
        self.assertTrue(
            self.gs._thread_is_red_generated([_msg({"x-red-generated": "1"})])
        )

    def test_empty_thread_is_not_generated(self):
        self.assertFalse(self.gs._thread_is_red_generated([]))

    def test_plain_thread_is_not_generated(self):
        self.assertFalse(
            self.gs._thread_is_red_generated([_msg({"From": "a@b.com"})])
        )


# ── email lake ──────────────────────────────────────────────────────

class EmailLakeExclusionTests(unittest.TestCase):
    def setUp(self):
        from agent_core import email_classify
        self.ec = email_classify

    def test_detects_marker_case_insensitively(self):
        self.assertTrue(self.ec._is_red_generated({"X-RED-Generated": "1"}))
        self.assertTrue(self.ec._is_red_generated({"x-red-generated": "1"}))

    def test_plain_headers_are_not_generated(self):
        self.assertFalse(self.ec._is_red_generated({"From": "a@b.com"}))
        self.assertFalse(self.ec._is_red_generated({}))

    def test_generated_mail_skipped_before_gemini_call(self):
        """自產信不進 lake，而且要擋在 Gemini 分類前面（省一次 API）。"""
        service = mock.Mock()
        service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "threadId": "t1",
            "payload": {"headers": [
                {"name": "From", "value": "owner@company.example"},
                {"name": "Subject", "value": "【生產日報】"},
                {"name": "X-RED-Generated", "value": "dispatcher:生產日報"},
            ]},
        }
        with mock.patch.object(self.ec, "get_service", return_value=service), \
             mock.patch.object(self.ec, "_gemini_generate") as gen:
            row = self.ec._classify_email_for_lake("m1")
        self.assertTrue(row["skipped"])
        self.assertIn("X-RED-Generated", row["skipped_reason"])
        gen.assert_not_called()


# ── internal_emails ─────────────────────────────────────────────────

class InternalEmailsExclusionTests(unittest.TestCase):
    def setUp(self):
        from agent_core import internal_emails_extract
        self.ie = internal_emails_extract

    @staticmethod
    def _header_fn(msg, name):
        for h in (msg.get("payload", {}).get("headers") or []):
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
        return ""

    def _thread(self, *header_dicts):
        return {
            "id": "t1",
            "messages": [
                {**_msg(h), "id": f"m{i}", "internalDate": str(1000 + i)}
                for i, h in enumerate(header_dicts)
            ],
        }

    def test_context_flags_generated_thread(self):
        ctx = self.ie.thread_to_context(
            self._thread({"Subject": "【日報】", "X-RED-Generated": "1"}),
            header_fn=self._header_fn,
        )
        self.assertTrue(ctx["generated_by_red"])

    def test_context_keeps_human_thread(self):
        ctx = self.ie.thread_to_context(
            self._thread({"Subject": "報價", "From": "a@b.com"}),
            header_fn=self._header_fn,
        )
        self.assertFalse(ctx["generated_by_red"])

    def test_row_builder_drops_generated_thread(self):
        row = self.ie.row_from_thread(
            self._thread({"Subject": "【日報】", "X-RED-Generated": "1"}),
            {"summary": "s"},
            header_fn=self._header_fn,
            should_exclude=lambda _s, _subj: False,
            classify_dept=lambda _s, _subj: ("gray", ["gray"]),
            classify_direction=lambda _s: "internal",
            detect_brands=lambda _t: [],
        )
        self.assertIsNone(row)

    def test_process_thread_id_marks_generated_as_handled(self):
        """要回報「已處理」，否則每輪都會重試同一批報表。"""
        svc = mock.Mock()
        svc.users.return_value.threads.return_value.get.return_value.execute.return_value = (
            self._thread({"Subject": "【日報】", "X-RED-Generated": "1"})
        )
        gen = mock.Mock()
        tid, row, processed, reason = self.ie.process_thread_id(
            "t1",
            get_service=lambda *a: svc,
            header_fn=self._header_fn,
            should_exclude=lambda _s, _subj: False,
            gemini_generate=gen,
            classify_dept=lambda _s, _subj: ("gray", ["gray"]),
            classify_direction=lambda _s: "internal",
            detect_brands=lambda _t: [],
            logger=mock.Mock(),
        )
        self.assertIsNone(row)
        self.assertTrue(processed)
        self.assertEqual(reason, "red_generated")
        gen.assert_not_called()


# ── rag_gateway：檢索端過濾 ─────────────────────────────────────────

class SemanticSearchFilterTests(unittest.TestCase):
    def setUp(self):
        from agent_core import rag_gateway
        self.rg = rag_gateway

    def _search(self, **kwargs):
        store = mock.Mock()
        store.query.return_value = []
        with mock.patch("agent_core.ingest.vector_store.get_store",
                        return_value=store), \
             mock.patch.object(self.rg, "log_rag_access_event"):
            self.rg.semantic_search("gmail_threads", "問題", **kwargs)
        return store.query.call_args.kwargs["where"]

    def _flatten(self, where):
        """把 where 樹攤平成 {field: cond}，不管包了幾層 $and。"""
        out = {}
        stack = [where]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            for key, val in node.items():
                if key in ("$and", "$or"):
                    stack.extend(val)
                else:
                    out[key] = val
        return out

    def test_default_excludes_generated(self):
        where = self._search(caller="red")
        self.assertEqual(
            self._flatten(where).get("generated_by_red"), {"$ne": True}
        )

    def test_include_generated_opt_in(self):
        """要回答「上週寄了哪些報表」時才打開。"""
        where = self._search(caller="red", include_generated=True)
        self.assertNotIn("generated_by_red", self._flatten(where or {}))

    def test_filter_composes_with_department_acl(self):
        where = self._search(caller="orange")
        flat = self._flatten(where)
        self.assertEqual(flat.get("generated_by_red"), {"$ne": True})
        self.assertEqual(flat.get("access_orange"), {"$eq": True})

    def test_filter_composes_with_base_where(self):
        where = self._search(caller="red", base_where={"mailbox_email": {"$eq": "a@b.com"}})
        flat = self._flatten(where)
        self.assertEqual(flat.get("generated_by_red"), {"$ne": True})
        self.assertEqual(flat.get("mailbox_email"), {"$eq": "a@b.com"})


class LegacyChunkSafetyTests(unittest.TestCase):
    """存量安全：15 萬+ 既有 chunk 沒有 generated_by_red 這個欄位。

    這組是整個改動最危險的地方——過濾條件寫錯會讓整個既有語料在檢索中消失，
    而且是靜默的（只是「查不到東西」，不會報錯）。$ne 對缺欄位必須放行。
    """

    def setUp(self):
        from agent_core.ingest import vector_store
        self.vs = vector_store
        from agent_core import rag_gateway
        self.cond = rag_gateway._EXCLUDE_RED_GENERATED

    def test_legacy_chunk_without_field_survives(self):
        legacy = {"doc_id": "t1", "subject": "舊報價單"}
        self.assertTrue(self.vs._eval_where(self.cond, legacy))

    def test_generated_chunk_is_dropped(self):
        self.assertTrue(
            not self.vs._eval_where(self.cond, {"generated_by_red": True})
        )

    def test_explicitly_marked_human_chunk_survives(self):
        self.assertTrue(
            self.vs._eval_where(self.cond, {"generated_by_red": False})
        )

    def test_predicate_is_evaluated_in_python_not_pushed_to_chroma(self):
        """布林 term 必須走 Python 後過濾。

        推到 Chroma 的話會踩到「缺欄位不匹配」的伺服器語意，把存量全濾掉；
        而且布林述詞在 server 端沒有索引，會變成全表掃描。
        """
        pushdown, has_bool = self.vs._split_where_pushdown(dict(self.cond))
        self.assertTrue(has_bool)
        self.assertNotIn("generated_by_red", pushdown or {})


# ── 記憶庫召回過濾 ──────────────────────────────────────────────────

class MemoryRecallFilterTests(unittest.TestCase):
    """記憶庫是「寄出當下」就寫入的快捷路徑，不等夜間 RAG，所以也要濾。"""

    def setUp(self):
        from agent_core import memory_ops
        self.mo = memory_ops

    def test_missing_field_is_not_generated(self):
        """舊記憶沒有這個欄位——必須保守放行，否則既有記憶會靜默消失。"""
        self.assertFalse(self.mo.is_red_generated_meta({"source": "email"}))
        self.assertFalse(self.mo.is_red_generated_meta({}))
        self.assertFalse(self.mo.is_red_generated_meta(None))

    def test_bool_and_string_forms_both_detected(self):
        """Chroma 可能把 bool 存成字串。"""
        self.assertTrue(self.mo.is_red_generated_meta({"generated_by_red": True}))
        self.assertTrue(self.mo.is_red_generated_meta({"generated_by_red": "True"}))
        self.assertFalse(self.mo.is_red_generated_meta({"generated_by_red": False}))
        self.assertFalse(self.mo.is_red_generated_meta({"generated_by_red": "false"}))

    def _recall(self, **kwargs):
        """跑一次 recall，餵一筆自產、一筆真人記憶。"""
        col = mock.MagicMock()
        col.query.return_value = {
            "documents": [["自產報表內文", "同事寫的信"]],
            "metadatas": [[
                {"source": "email", "generated_by_red": True},
                {"source": "email"},
            ]],
            "ids": [["gen1", "human1"]],
            "distances": [[0.10, 0.20]],
        }
        return self.mo.recall(
            "產能",
            k=5,
            mode="vector",
            get_memory_collection_fn=lambda: col,
            build_bm25_index_fn=lambda: None,
            simple_tokenize_fn=lambda q: [q],
            fetch_docs_by_ids_fn=lambda _c, ids: {},
            logger_obj=mock.Mock(),
            **kwargs,
        )

    def test_generated_memory_filtered_by_default(self):
        out = self._recall()
        self.assertNotIn("自產報表內文", out)
        self.assertIn("同事寫的信", out)

    def test_include_generated_opt_in(self):
        out = self._recall(include_generated=True)
        self.assertIn("自產報表內文", out)

    def test_recall_tool_signature_unchanged(self):
        """memory.recall 掛在 tool catalog 上，簽名動了會改到 Gemini
        function declaration —— 過濾必須靠 memory_ops 的預設值生效，
        不能在工具層多開參數。"""
        import inspect
        from agent_core import memory
        params = inspect.signature(memory.recall).parameters
        self.assertNotIn("include_generated", params)


# ── backfill 啟發式 ─────────────────────────────────────────────────

class BackfillHeuristicTests(unittest.TestCase):
    """存量報表沒有信頭，只能靠主旨 + 自寄判斷。

    誤判方向不對稱：漏抓只是維持現狀，誤抓會讓真人的信從檢索裡靜默消失。
    所以這組的重點是「不該抓的絕對不能抓」。
    """

    def setUp(self):
        import importlib.util
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "backfill_red_generated_meta.py",
        )
        spec = importlib.util.spec_from_file_location("_backfill_gen", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.bf = mod

    def test_scheduled_prefix_matches(self):
        self.assertEqual(
            self.bf.looks_red_generated({"subject": "【排程: 生產日報】"}),
            "scheduled_prefix",
        )

    def test_self_sent_bracket_subject_matches(self):
        rule = self.bf.looks_red_generated({
            "subject": "【生產日報】",
            "sender": "Owner Name <owner@company.example>",
            "mailbox_email": "owner@company.example",
        })
        self.assertEqual(rule, "self_sent_bracket")

    def test_bracket_subject_from_colleague_is_not_matched(self):
        """同事也會用【】當主旨——沒有自寄條件就不能抓。"""
        self.assertEqual(self.bf.looks_red_generated({
            "subject": "【急件】PAX 報價",
            "sender": "coworker@company.example",
            "mailbox_email": "owner@company.example",
        }), "")

    def test_self_sent_but_normal_subject_is_not_matched(self):
        """自己寄給自己的備忘錄不是報表。"""
        self.assertEqual(self.bf.looks_red_generated({
            "subject": "記得回覆 PAX",
            "sender": "owner@company.example",
            "mailbox_email": "owner@company.example",
        }), "")

    def test_empty_subject_is_not_matched(self):
        self.assertEqual(self.bf.looks_red_generated({}), "")
        self.assertEqual(self.bf.looks_red_generated({"subject": "   "}), "")

    def test_bracket_subject_without_mailbox_meta_is_not_matched(self):
        """舊 chunk 可能沒有 mailbox_email——沒有自寄證據就不准抓。"""
        self.assertEqual(self.bf.looks_red_generated({
            "subject": "【生產日報】",
            "sender": "owner@company.example",
        }), "")

    def test_already_marked_detection_is_idempotent(self):
        self.assertTrue(self.bf.already_marked({"generated_by_red": True}))
        self.assertTrue(self.bf.already_marked({"generated_by_red": "true"}))
        self.assertFalse(self.bf.already_marked({}))

    def test_defaults_embed_dim_so_it_opens_the_production_collection(self):
        """忘了帶 RED_EMBED_DIM 會開到裸名 3072 空 collection，掃出 seen=0
        卻不報錯（2026-08-12 實際踩到：--apply 安靜地什麼都沒寫）。

        用乾淨的 environ 重載一次，才不會被跑測試時的外部 env 影響。"""
        import importlib.util
        clean = {k: v for k, v in os.environ.items() if k != "RED_EMBED_DIM"}
        with mock.patch.dict(os.environ, clean, clear=True):
            spec = importlib.util.spec_from_file_location(
                "_backfill_gen_dim", self.bf.__file__)
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
            self.assertEqual(os.environ.get("RED_EMBED_DIM"), "768")

    def test_empty_collection_aborts_instead_of_reporting_success(self):
        """空 collection＝開錯地方，不是「沒東西要補」。必須擋在掃描前，
        否則 --apply 會印出一份跟正常結束一模一樣的 seen=0 報告。"""
        class _EmptyStore:
            def count(self):
                return 0

            def _with_collection(self, fn):  # pragma: no cover — 不該被呼叫
                raise AssertionError("空 collection 不該進入掃描/寫入")

        with mock.patch.object(self.bf, "get_store", lambda *a, **k: _EmptyStore()), \
                mock.patch.object(sys, "argv", ["backfill", "--apply"]):
            rc = self.bf.main()
        self.assertEqual(rc, 2, "空 collection 要用非 0 退出碼中止")
        self.assertFalse(self.bf.already_marked({"generated_by_red": False}))


# ── 防回歸：程式路徑不准用未標記的寄件器 ────────────────────────────

class UnmarkedSenderGuardTests(unittest.TestCase):
    """`agent_core.gmail.send_gmail` 是 LLM 工具、不收 generated_by。

    它的簽名綁著 Gemini function declaration 所以動不得，但這造成一個陷阱：
    任何**程式路徑**（daemon / 排程 / agent）誤用它，寄出去的小紅產出就沒有
    出處標記，隔天靜靜被 RAG 當公司原始資料吃回去，沒有任何錯誤訊息。

    這個坑踩過一次了：第一版只接了 dispatcher 和 notify，漏掉 quote_gen /
    qc / alert_pusher / briefing 四條。人工記憶顯然不夠可靠，改用測試釘死。

    程式路徑一律走 send_gmail_internal(generated_by=...)。
    """

    # 唯一合法的例外：工具目錄要 import 它才能註冊成 LLM 工具。
    _ALLOWED = {os.path.join("agent_core", "tool_registry_catalog.py")}

    def _repo_root(self):
        # 不 hardcode 絕對路徑，worktree 裡才跑得動。
        from agent_core import path_safety
        return path_safety._REPO_ROOT

    def test_no_programmatic_path_imports_bare_send_gmail(self):
        import ast
        root = self._repo_root()
        offenders = []
        for sub in ("agent_core", "skills"):
            base = os.path.join(root, sub)
            for dirpath, dirnames, filenames in os.walk(base):
                # 別掃到其他 Claude session 的工作樹
                dirnames[:] = [d for d in dirnames if d != ".claude"]
                for fn in filenames:
                    if not fn.endswith(".py"):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, root)
                    if rel in self._ALLOWED:
                        continue
                    try:
                        with open(full, encoding="utf-8") as handle:
                            tree = ast.parse(handle.read())
                    except SyntaxError:
                        continue
                    for node in ast.walk(tree):
                        if not isinstance(node, ast.ImportFrom):
                            continue
                        if node.module != "agent_core.gmail":
                            continue
                        for alias in node.names:
                            if alias.name == "send_gmail":
                                offenders.append(f"{rel}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            "這些程式路徑 import 了未標記的 send_gmail，寄出的小紅產出會沒有"
            f"出處標記、被 RAG 當原始資料吃回去：{offenders}\n"
            "改用 send_gmail_internal(..., generated_by='<來源標籤>')。",
        )

    def test_known_generated_senders_pass_generated_by(self):
        """四條已知的小紅產出寄件路徑都要帶 generated_by。"""
        import ast
        root = self._repo_root()
        targets = [
            os.path.join("agent_core", "qc.py"),
            os.path.join("agent_core", "alert_pusher.py"),
            os.path.join("agent_core", "agents", "orange_sales", "quote_gen.py"),
            os.path.join("skills", "briefing.py"),
        ]
        for rel in targets:
            with self.subTest(module=rel):
                with open(os.path.join(root, rel), encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
                calls = [
                    node for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "send_gmail_internal"
                ]
                self.assertTrue(calls, f"{rel} 應該用 send_gmail_internal 寄信")
                for call in calls:
                    kwargs = {kw.arg for kw in call.keywords}
                    self.assertIn(
                        "generated_by", kwargs,
                        f"{rel}:{call.lineno} 的 send_gmail_internal 少了 generated_by",
                    )


# ── 反思層不反思自己的產出 ──────────────────────────────────────────

class ReflectionIntakeFilterTests(unittest.TestCase):
    """反思層會把「剛進 RAG 的新文件」拿去反思。報表進了索引就會被當成新
    文件讀 —— 等於反思自己上一輪寫的東西。模組本來就刻意不追蹤
    xiaohong_reflections（同一個理由），只是漏了從 gmail 線繞進來這條。
    """

    def setUp(self):
        from agent_core.ingest import reflection_intake
        self.ri = reflection_intake

    def _record(self, metas):
        written = []
        with mock.patch.object(self.ri, "json") as fake_json, \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch.object(self.ri.os, "makedirs"), \
             mock.patch.object(self.ri, "fcntl"):
            fake_json.dumps.side_effect = lambda payload, **kw: written.append(payload) or "{}"
            self.ri.record_batch(
                "gmail_threads",
                [f"c{i}" for i in range(len(metas))],
                metas,
            )
        return written[0]["docs"] if written else []

    def test_generated_thread_not_queued_for_reflection(self):
        docs = self._record([
            {"doc_id": "gen1", "subject": "【生產日報】", "generated_by_red": True},
            {"doc_id": "human1", "subject": "PAX 報價討論"},
        ])
        ids = {d["d"] for d in docs}
        self.assertNotIn("gen1", ids)
        self.assertIn("human1", ids)

    def test_legacy_docs_without_flag_still_queued(self):
        """舊資料沒有欄位，不能被順手濾掉。"""
        docs = self._record([{"doc_id": "old1", "subject": "舊報價"}])
        self.assertEqual({d["d"] for d in docs}, {"old1"})


# ── 出處判定的單一來源 ──────────────────────────────────────────────

class ProvenanceSingleSourceTests(unittest.TestCase):
    """判定規則只准有一份。CLAUDE.md 對 env_int 已經立過同樣的規矩：
    同一個判斷散在各模組各寫一份，遲早漂移成兩套語意。"""

    def test_modules_reuse_the_shared_helper(self):
        from agent_core import memory_ops, provenance
        from agent_core.ingest import reflection_intake
        self.assertIs(memory_ops.is_red_generated_meta, provenance.is_red_generated_meta)
        self.assertIs(
            reflection_intake.is_red_generated_meta, provenance.is_red_generated_meta
        )

    def test_no_module_hardcodes_the_metadata_field(self):
        """除了 provenance 自己，不准再有人手寫 'generated_by_red' 字面值。"""
        import ast
        from agent_core import path_safety, provenance
        root = path_safety._REPO_ROOT
        allowed = {
            os.path.join("agent_core", "provenance.py"),
            # gmail_sync 寫入 metadata、gmail_ops 寫記憶 metadata：欄位名出現在
            # dict literal 的 key 上，改用常數反而更難讀，這兩處允許字面值。
            os.path.join("agent_core", "ingest", "gmail_sync.py"),
            os.path.join("agent_core", "gmail_ops.py"),
        }
        offenders = []
        for sub in ("agent_core", "skills", "scripts"):
            for dirpath, dirnames, filenames in os.walk(os.path.join(root, sub)):
                dirnames[:] = [d for d in dirnames if d != ".claude"]
                for fn in filenames:
                    if not fn.endswith(".py"):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, root)
                    if rel in allowed:
                        continue
                    try:
                        with open(full, encoding="utf-8") as handle:
                            tree = ast.parse(handle.read())
                    except SyntaxError:
                        continue
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Constant) and \
                                node.value == provenance.METADATA_FIELD:
                            offenders.append(f"{rel}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            f"這些地方手寫了 metadata 欄位名，改用 provenance.METADATA_FIELD：{offenders}",
        )


# ── 三道檢索門都要走同一個 chokepoint ───────────────────────────────

class AllRetrievalDoorsFilteredTests(unittest.TestCase):
    """檢索有三道門：semantic_search、drive_search、chat_search。

    後兩道不走 semantic_search，是自己組 where 再直接打 store.query —— 修這輪
    之前它們完全沒有自產內容過濾。現在統一由 rag_gateway.access_where 供應
    filter，這組測試釘住「三道門都真的帶了排除條件」。
    """

    def _capture_where(self, run):
        store = mock.MagicMock()
        store.is_empty.return_value = False
        store.query.return_value = []
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store):
            run()
        self.assertTrue(store.query.called, "沒有真的打到 store.query")
        return store.query.call_args.kwargs.get("where")

    def _flatten(self, where):
        out = {}
        stack = [where]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            for key, val in node.items():
                if key in ("$and", "$or"):
                    stack.extend(val)
                else:
                    out[key] = val
        return out

    def test_drive_search_excludes_generated(self):
        from agent_core.ingest import drive_search
        where = self._capture_where(
            lambda: drive_search.search_drive_docs("保固條款", k=3)
        )
        self.assertEqual(
            self._flatten(where).get("generated_by_red"), {"$ne": True}
        )

    def test_chat_search_excludes_generated(self):
        from agent_core.ingest import chat_search
        where = self._capture_where(
            lambda: chat_search.search_google_chat("交期", k=3)
        )
        self.assertEqual(
            self._flatten(where).get("generated_by_red"), {"$ne": True}
        )

    def test_semantic_search_still_excludes_generated(self):
        from agent_core import rag_gateway
        store = mock.MagicMock()
        store.query.return_value = []
        with mock.patch("agent_core.ingest.vector_store.get_store", return_value=store), \
             mock.patch.object(rag_gateway, "log_rag_access_event"):
            rag_gateway.semantic_search("gmail_threads", "報價", caller="red")
        self.assertEqual(
            self._flatten(store.query.call_args.kwargs["where"]).get("generated_by_red"),
            {"$ne": True},
        )

    def test_no_retrieval_path_builds_its_own_acl_filter(self):
        """新的檢索路徑必須跟 access_where 拿 filter。

        自己手寫 access_<color> 條件的話，就會複製出一條繞過自產內容過濾的
        新門 —— 正是這輪修掉的那種漏洞（drive_search / chat_search 原本就是
        自己組 where、完全沒有過濾）。

        只認「access_ + 真實部門色」的字面值，避開 access_token / access_type
        這類同前綴但無關的字串。
        """
        import ast
        from agent_core import path_safety
        from agent_core.agents.permission_matrix import Agent
        root = path_safety._REPO_ROOT
        acl_keys = {f"access_{a.value}" for a in Agent}
        allowed = {
            # filter 的定義處
            os.path.join("agent_core", "rag_gateway.py"),
            # 布林述詞 pushdown 偵測要認得這些欄位名（不是在組 filter）
            os.path.join("agent_core", "ingest", "vector_store.py"),
            # ACL metadata 的維護/回填，本來就得直接寫欄位
            os.path.join("agent_core", "ingest", "acl_reconcile.py"),
        }
        offenders = []
        for sub in ("agent_core", "skills"):
            for dirpath, dirnames, filenames in os.walk(os.path.join(root, sub)):
                dirnames[:] = [d for d in dirnames if d != ".claude"]
                for fn in filenames:
                    if not fn.endswith(".py"):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, root)
                    if rel in allowed:
                        continue
                    try:
                        with open(full, encoding="utf-8") as handle:
                            tree = ast.parse(handle.read())
                    except SyntaxError:
                        continue
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Constant) and node.value in acl_keys:
                            offenders.append(f"{rel}:{node.lineno} {node.value!r}")
        self.assertEqual(
            offenders, [],
            "這些地方自己組了 ACL filter，會繞過 access_where 的自產內容過濾，"
            f"改成呼叫 rag_gateway.access_where：{offenders}",
        )


if __name__ == "__main__":
    unittest.main()
