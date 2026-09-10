"""Supremo（Lurchi）出貨文件追蹤：期限閘門、email 查核口徑、結案路徑、排程註冊。

守的事：
  1. 結案只有兩條路——「主旨含櫃號的寄件涵蓋兩位 ContactW」自動結案，或
     「業務確認＋查有寄件（可無櫃號）」。只有口頭確認、信箱查無 → 絕不結案。
  2. 兩位 ContactW 缺一不可（客人要求原文：文件須要電郵給兩位 WENDY）。
  3. 每櫃每天最多問一次；沒到期／全結案回「(無新發現)」讓 dispatcher 安靜。
  4. 櫃號逐年重複（2022–2026 都有 LURCHI-CONT7）——視窗外的舊信不算證據。
  5. 註冊腳本冪等＋deterministic_tool 進得了背景工具集（靜默失效前科 #350）。

⚠️ 測試跑的是 unittest（不是 pytest），隔離一律寫在 setUp/tearDown。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import agent_core.shipping_doc_tracker as sdt  # noqa: E402
import scripts.register_shipping_doc_task as reg  # noqa: E402

_BOTH = set(sdt.DOC_RECIPIENTS)
_CHEUNG = "shipping-contact@customer-a.example"
_LAW = "docs-contact@customer-a.example"


def _ev(subject: str, on: date, covered: set[str], mailbox: str = "twsales@company.example",
        has_excel: bool = False):
    return {"mailbox": mailbox, "subject": subject, "date": on,
            "when": on.strftime("%m/%d"), "covered": set(covered),
            "has_excel": has_excel}


class RefMatchTests(unittest.TestCase):
    """櫃號比對：全名、短形（cont 7）、大小寫連字號都要認得；別撞無關字。"""

    def test_ref_parts_splits_brand_and_number(self):
        self.assertEqual(sdt._ref_parts("LURCHI-CONT7"), ("LURCHI", 7))
        self.assertEqual(sdt._ref_parts("LURCHI ( KIENAST)-CONT 10"), ("LURCHI", 10))
        self.assertEqual(sdt._ref_parts("DRAFT DOCS LURCHI-CONT 3"), ("LURCHI", 3))

    def test_container_numbers_handles_combined_shipments(self):
        """實測 2026-08-21 主旨：一封信涵蓋三櫃，逐櫃都要認得出來。"""
        subject = ("RE: LURCHI ( KIENAST)-CONT 9+ 10 and Lurchi CNT-8 "
                   "shipping documents運單號 SF0213866905821")
        self.assertEqual(sdt._container_numbers(subject), {8, 9, 10})
        self.assertEqual(sdt._container_numbers("FYI 快遞單號 for CONT8,9,10"), {8, 9, 10})

    def test_combined_subject_matches_each_container(self):
        """這條紅了＝文件其實寄了卻天天催業務（併寄信被判成沒寄）。"""
        subject = ("RE: LURCHI ( KIENAST)-CONT 9+ 10 and Lurchi CNT-8 "
                   "shipping documents")
        for ref in ("LURCHI-CONT8", "LURCHI-CONT9", "LURCHI-CONT10"):
            with self.subTest(ref=ref):
                self.assertTrue(sdt._subject_matches(ref, subject))
        self.assertFalse(sdt._subject_matches("LURCHI-CONT7", subject))

    def test_container_number_must_match_exactly(self):
        """CONT1 不可以撞 CONT10、CONT7 不可以撞 CONT8。"""
        self.assertFalse(sdt._subject_matches("LURCHI-CONT1", "RE: LURCHI-CONT10"))
        self.assertFalse(sdt._subject_matches("LURCHI-CONT7", "RE: LURCHI-CONT8"))

    def test_other_brand_same_number_does_not_match(self):
        self.assertFalse(sdt._subject_matches("LURCHI-CONT7", "RE: RICHTER-CONT7"))

    def test_subject_variants_match(self):
        for subj in ("RE: LURCHI-CONT7 // 2454 PRS // ETD 07 AUG 2026",
                     "FW: LURCHI-CONT7",
                     "HW24 DR Z-LURCHI - cont 7 invoice#050824(3065prs)",
                     "lurchi cont7 掃描檔"):
            with self.subTest(subj=subj):
                self.assertTrue(sdt._subject_matches("LURCHI-CONT7", subj))

    def test_unrelated_subjects_do_not_match(self):
        for subj in ("RE: LURCHI-CONT8 // 6241 PRS", "CONTAINER 7 booking",
                     "應收帳款 FYI", ""):
            with self.subTest(subj=subj):
                self.assertFalse(sdt._subject_matches("LURCHI-CONT7", subj))

    def test_find_ref_tolerates_case_and_short_form(self):
        shipments = {"LURCHI-CONT7": {}, "LURCHI-CONT8": {}}
        self.assertEqual(sdt._find_ref(shipments, "lurchi-cont7"), "LURCHI-CONT7")
        self.assertEqual(sdt._find_ref(shipments, "CONT8"), "LURCHI-CONT8")
        self.assertIsNone(sdt._find_ref(shipments, "CONT9"))


class VerifyShipmentTests(unittest.TestCase):
    """查核口徑：strict＝主旨含櫃號；候選只在 ETD-7 天後算；視窗外舊信不算。"""

    _REC = {"ref": "LURCHI-CONT7", "etd": "2026-08-07"}

    def test_strict_coverage_accumulates_across_messages(self):
        verdict = sdt._verify_shipment(self._REC, [
            _ev("FW: LURCHI-CONT7", date(2026, 8, 5), {_CHEUNG}),
            _ev("RE: LURCHI-CONT7 docs", date(2026, 8, 6), {_LAW}),
        ])
        self.assertEqual(verdict["strict"], _BOTH)

    def test_old_year_same_ref_outside_window_is_ignored(self):
        """2024 年也有 cont 7 的信——視窗（ETD-21 天）外一律不算證據。"""
        verdict = sdt._verify_shipment(self._REC, [
            _ev("HW24 DR Z-LURCHI - cont 7 invoice#050824", date(2024, 8, 6), _BOTH),
        ])
        self.assertEqual(verdict["strict"], set())
        self.assertEqual(verdict["any"], set())
        self.assertEqual(verdict["hits"], [])

    def test_candidate_without_ref_counts_only_near_etd(self):
        verdict = sdt._verify_shipment(self._REC, [
            _ev("出貨文件", date(2026, 8, 5), {_CHEUNG}),      # ETD-2 → 候選
            _ev("出貨文件", date(2026, 7, 20), {_LAW}),        # ETD-18 → 太早不算
        ])
        self.assertEqual(verdict["strict"], set())
        self.assertEqual(verdict["any"], {_CHEUNG})


class EvidenceBookkeepingTests(unittest.TestCase):
    """證據的保存與視窗上界 —— 兩個都是 2026-08-26 首輪上線後查出來的真缺陷。"""

    _REC = {"ref": "LURCHI-CONT5", "etd": "2026-07-23"}

    def test_candidate_window_has_an_upper_bound(self):
        """CONT5 的 ETD 是 07-23，08-24 那封講 CONT9+10 的信不可以算它的候選 ——
        配上「業務確認＋候選」那條結案路徑，等於舊櫃被一個月後的無關信件結掉。"""
        verdict = sdt._verify_shipment(self._REC, [
            _ev("RE: LURCHI ( KIENAST)-CONT 9+ 10 docs", date(2026, 8, 24), _BOTH),
        ])
        self.assertEqual(verdict["any"], set())
        self.assertEqual(verdict["hits"], [])

    def test_candidate_just_inside_upper_bound_still_counts(self):
        verdict = sdt._verify_shipment(self._REC, [
            _ev("出貨文件補寄", date(2026, 8, 22), {_CHEUNG}),   # ETD+30 = 08-22
        ])
        self.assertEqual(verdict["any"], {_CHEUNG})

    def test_stored_evidence_puts_real_matches_first(self):
        """存證據前要把「主旨命中櫃號」的排前面：照掃描順序硬切前 10 筆會被同期
        別櫃的信塞滿（實測 CONT5 結案後存的 10 筆全是別櫃），事後查起來像是拿
        別櫃的信結案。"""
        hits = [{**_ev(f"別櫃 {i}", date(2026, 7, 20), {_CHEUNG}), "ref_match": False}
                for i in range(sdt._EVIDENCE_CAP)]
        hits.append({**_ev("FW: LURCHI-CONT5", date(2026, 7, 21), _BOTH),
                     "ref_match": True})
        rec: dict = {}
        sdt._store_evidence(rec, hits)
        self.assertTrue(rec["evidence"][0]["ref_match"])
        self.assertIn("CONT5", rec["evidence"][0]["subject"])
        self.assertEqual(len(rec["evidence"]), sdt._EVIDENCE_CAP)


class OriginalsTests(unittest.TestCase):
    """正本文件（實體快遞）追蹤。fixture 取自實際往來的信。"""

    def _rec(self, etd="2026-08-07"):
        return {"ref": "LURCHI-CONT7", "etd": etd}

    def _sig(self, kind, subject, on, tracking=(), quote=""):
        return {"kind": kind, "subject": subject, "date": on,
                "tracking": set(tracking), "quote": quote}

    def test_ack_phrases_are_recognised(self):
        """實測 ContactW 的回覆：「今天簽收文件 24 AUG 2026」「已收件」。"""
        self.assertTrue(sdt._ack_lines("Dear UserAng,\n今天簽收文件 24 AUG 2026\n請確認"))
        self.assertTrue(sdt._ack_lines("Dear UserAng 已收件 提單是否已經安排電放"))

    def test_chasing_lines_are_never_read_as_acknowledgement(self):
        """🚨 ContactW 也會寫「未收到文件」「請馬上補回」——那是**催件**，
        跟簽收相反。讀錯＝系統以為正本到了，罰款靜靜發生。"""
        for line in ("L060726-3未收到文件 US$114685.90順豐快遞派送",
                     "未有收到7月9日修改後的 正本發票, 請馬上補回, 以便安排付款",
                     "尚未收到文件",
                     "必須要補回正本發票"):
            with self.subTest(line=line):
                self.assertEqual(sdt._ack_lines(line), [])

    def test_tracking_numbers_parsed_from_real_formats(self):
        text = ("快遞單號 SF0213866905821 for CONT8,9,10 / 運單號 SF0214996834262 / "
                "AWB tracking no. CX3483757676544042")
        found = {m.group(1).upper() for m in sdt._TRACKING_RE.finditer(text)}
        self.assertEqual(found, {"SF0213866905821", "SF0214996834262",
                                 "CX3483757676544042"})

    def test_sent_then_acked_transitions(self):
        rec = self._rec()
        st = sdt._apply_originals(rec, [
            self._sig("sent", "RE: LURCHI-CONT7 shipping documents",
                      date(2026, 8, 12), ["SF0214996834262"]),
        ], date(2026, 8, 13))
        self.assertEqual(st["status"], "sent")
        self.assertEqual(st["tracking"], ["SF0214996834262"])
        st = sdt._apply_originals(rec, [
            self._sig("ack", "RE: LURCHI-CONT7", date(2026, 8, 15),
                      quote="今天簽收文件 15 AUG 2026"),
        ], date(2026, 8, 16))
        self.assertEqual(st["status"], "acked")
        self.assertEqual(st["acked_at"], "2026-08-15")

    def test_failed_scan_does_not_downgrade_to_overdue(self):
        """掃描失敗時「沒有簽收訊號」不是事實，只是沒資料。"""
        rec = self._rec()          # ETD 08-07 → 正本期限 08-17
        st = sdt._apply_originals(rec, [], date(2026, 8, 18), scan_ok=False)
        self.assertEqual(st["status"], "unverified")
        self.assertIsNone(st.get("last_ok_at"))
        text = sdt._render_originals(rec, date(2026, 8, 18))
        self.assertIn("未經查核", text)
        self.assertNotIn("🔴", text)

    def test_positive_signals_still_count_during_a_partial_failure(self):
        """掃到的簽收是**證據**，就算同輪有錯誤也照樣採信；只有「判逾期」需要
        掃描完全成功。"""
        rec = self._rec()
        st = sdt._apply_originals(rec, [
            self._sig("ack", "RE: LURCHI-CONT7", date(2026, 8, 15),
                      quote="今天簽收文件"),
        ], date(2026, 8, 18), scan_ok=False)
        self.assertEqual(st["status"], "acked")

    def test_successful_scan_records_last_ok(self):
        rec = self._rec()
        st = sdt._apply_originals(rec, [], date(2026, 8, 18), scan_ok=True)
        self.assertEqual(st["status"], "overdue")
        self.assertEqual(st["last_ok_at"], "2026-08-18")

    def test_overdue_when_deadline_passed_without_ack(self):
        rec = self._rec()          # ETD 08-07 → 正本期限 08-17
        st = sdt._apply_originals(rec, [], date(2026, 8, 18))
        self.assertEqual(st["status"], "overdue")

    def test_other_container_signals_never_count(self):
        """正本沒有「候選」那種寬鬆路徑：別櫃的簽收算成自己的＝謊報正本已到。"""
        rec = self._rec()
        st = sdt._apply_originals(rec, [
            self._sig("ack", "RE: LURCHI-CONT8 documents", date(2026, 8, 10),
                      quote="今天簽收文件"),
        ], date(2026, 8, 11))
        self.assertEqual(st["status"], "sent" if st.get("sent_at") else "pending")
        self.assertIsNone(st["acked_at"])

    def test_quoted_history_is_not_read_as_a_fresh_acknowledgement(self):
        """🚨 ContactW 回信會整串引用舊信。實測她的回覆裡夾著 UserAng 一個月前寫的
        「(前收到文件) 即安排付款」——不切引言就會把舊話讀成「客人剛簽收」。"""
        body = ("Dear UserAng,\n請問付款進度\n\n"
                "寄件者: TW UserAng <twsales@company.example>\n"
                "之前文件寄出 隔週二(前收到文件) 即安排付款\n")
        self.assertEqual(sdt._ack_lines(body), [])
        # 引言前自己寫的簽收語照樣要抓到。
        ok = ("今天簽收文件 24 AUG 2026\n\nFrom: TW UserAng\n(前收到文件) 即安排付款")
        self.assertTrue(sdt._ack_lines(ok))

    def test_tracking_number_is_attributed_to_the_right_container(self):
        """實測併櫃信同時寫兩組對應：不解析就會對 CONT8 報出 CONT7 的單號，
        人拿去順豐查會查到別櫃的貨。"""
        text = ("快遞單號 SF0213866905821 for CONT8,9,10 (Total US$254785.50)\n"
                "快遞單號 SF0214996834262 for Lurchi CONT-7 (US$44150.20) 已於8/21簽收")
        mapping = sdt._tracking_by_container(text)
        self.assertEqual(mapping["SF0213866905821"], {8, 9, 10})
        self.assertEqual(mapping["SF0214996834262"], {7})

        sig = {"kind": "sent", "date": date(2026, 8, 21), "quote": "",
               "subject": "RE: LURCHI ( KIENAST)-CONT 9+ 10 and Lurchi CNT-8",
               "tracking": {"SF0213866905821", "SF0214996834262"},
               "tracking_for": mapping}
        rec8 = {"ref": "LURCHI-CONT8", "etd": "2026-08-15"}
        st8 = sdt._apply_originals(rec8, [sig], date(2026, 8, 22))
        self.assertEqual(st8["tracking"], ["SF0213866905821"])
        rec7 = {"ref": "LURCHI-CONT7", "etd": "2026-08-07"}
        st7 = sdt._apply_originals(rec7, [sig], date(2026, 8, 22))
        self.assertEqual(st7["tracking"], ["SF0214996834262"])

    def test_combined_mail_without_mapping_attaches_no_tracking(self):
        """併櫃信沒寫對應時寧可不掛單號 —— 掛錯比沒有更糟。"""
        sig = {"kind": "sent", "date": date(2026, 8, 21), "quote": "",
               "subject": "RE: LURCHI-CONT 9+ 10 docs",
               "tracking": {"SF0213866905821"}, "tracking_for": {}}
        st = sdt._apply_originals({"ref": "LURCHI-CONT9", "etd": "2026-08-15"},
                                  [sig], date(2026, 8, 22))
        self.assertEqual(st["tracking"], [])
        self.assertEqual(st["sent_at"], "2026-08-21")   # 寄出這件事仍然成立

    def test_self_reported_ack_is_labelled_not_treated_as_customer_confirmation(self):
        """實測 CONT7：UserAng 寫「已於8/21簽收」而客人沒再回一封。要擋掉
        「🔴逾期未見簽收」的誤報，但**必須標明來源是我方而非客人**。"""
        rec = self._rec()          # ETD 08-07 → 正本期限 08-17
        st = sdt._apply_originals(rec, [
            {"kind": "ack_self", "subject": "RE: LURCHI-CONT7",
             "date": date(2026, 8, 21), "tracking": set(),
             "quote": "快遞單號 SF0214996834262 for Lurchi CONT-7 已於8/21簽收"},
        ], date(2026, 8, 27))
        self.assertEqual(st["status"], "acked_by_us")
        self.assertIsNone(st["acked_at"])           # 不可冒充客人確認
        text = sdt._render_originals(rec, date(2026, 8, 27))
        self.assertIn("我方回報已簽收", text)
        self.assertIn("客人尚未回信確認", text)
        self.assertNotIn("🔴", text)

    def test_watch_stays_quiet_until_close_to_the_deadline(self):
        """通知信常在 ETD 前三週就到；那時天天報「正本尚未寄出」是純噪音。"""
        rec = self._rec()          # 正本期限 08-17，開口日 = 08-15
        self.assertTrue(sdt._originals_watch_over(rec, date(2026, 7, 25)))
        self.assertFalse(sdt._originals_watch_over(rec, date(2026, 8, 15)))

    def test_watch_stops_after_ack_and_after_long_overdue(self):
        rec = self._rec()
        sdt._apply_originals(rec, [
            self._sig("ack", "RE: LURCHI-CONT7", date(2026, 8, 15),
                      quote="簽收")], date(2026, 8, 16))
        self.assertTrue(sdt._originals_watch_over(rec, date(2026, 8, 16)))
        fresh = self._rec()
        self.assertTrue(sdt._originals_watch_over(fresh, date(2026, 12, 31)))


class _FakeGmail:
    """假的 Gmail users() —— 只實作分頁列表與逐封 metadata 取回。"""

    def __init__(self, messages, page_size=2):
        self._messages = messages          # [{id, to, cc, subject, ts}]
        self._page_size = page_size
        self.list_calls = 0

    def messages(self):
        return self

    def list(self, userId=None, q=None, pageToken=None, maxResults=None):
        self.list_calls += 1
        start = int(pageToken or 0)
        size = min(self._page_size, maxResults or self._page_size)
        page = self._messages[start:start + size]
        nxt = start + size
        out = {"messages": [{"id": m["id"]} for m in page]}
        if nxt < len(self._messages):
            out["nextPageToken"] = str(nxt)
        return _FakeExec(out)

    def get(self, userId=None, id=None, format=None, metadataHeaders=None):
        m = next(x for x in self._messages if x["id"] == id)
        return _FakeExec({
            "internalDate": str(m.get("ts", 1_754_000_000_000)),
            "payload": {"headers": [
                {"name": "To", "value": m.get("to", "")},
                {"name": "Cc", "value": m.get("cc", "")},
                {"name": "From", "value": m.get("from", "")},
                {"name": "Subject", "value": m.get("subject", "")},
            ]},
        })


class _FakeExec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class EvidenceScanPaginationTests(unittest.TestCase):
    """證據掃描要翻頁翻到底；真的到上限必須出聲 —— 靜默截斷是實際發生過的 bug
    （2026-08-26 twsales@ 視窗內 47 封、舊上限 40，每輪靜默丟 7 封，而 Gmail
    是新到舊排序 → 丟掉的正是舊櫃最需要的證據）。"""

    def _msgs(self, n):
        return [{"id": f"m{i}", "to": "shipping-contact@customer-a.example",
                 "cc": "docs-contact@customer-a.example", "subject": f"FW: LURCHI-CONT{i}"}
                for i in range(n)]

    def test_pagination_walks_every_page(self):
        fake = _FakeGmail(self._msgs(7), page_size=2)
        ids, capped = sdt._list_sent_ids(fake, "q")
        self.assertEqual(len(ids), 7)          # 不是只拿第一頁的 2 筆
        self.assertFalse(capped)
        self.assertGreater(fake.list_calls, 1)

    def test_hard_cap_is_reported_not_silent(self):
        with mock.patch.object(sdt, "_MAX_PER_MAILBOX", 4):
            fake = _FakeGmail(self._msgs(10), page_size=2)
            ids, capped = sdt._list_sent_ids(fake, "q")
        self.assertEqual(len(ids), 4)
        self.assertTrue(capped, "到頂了卻回 False＝又變成靜默截斷")

    def test_collect_evidence_surfaces_the_truncation(self):
        with mock.patch.object(sdt, "_MAX_PER_MAILBOX", 3), \
             mock.patch.object(sdt, "DOC_SENDER_MAILBOXES", ("twsales@company.example",)), \
             mock.patch.object(sdt, "_gmail_users",
                               lambda mb: _FakeGmail(self._msgs(9), page_size=2)):
            evidence, errors = sdt._collect_evidence(date(2026, 7, 1))
        self.assertEqual(len(evidence), 3)
        self.assertTrue(errors, "截斷了卻沒有任何訊息＝人不會知道這輪不可信")
        self.assertIn("超過單輪上限", errors[0])
        self.assertIn("RED_SHIPDOC_MAX_PER_MAILBOX", errors[0])

    def test_no_truncation_means_no_noise(self):
        with mock.patch.object(sdt, "DOC_SENDER_MAILBOXES", ("twsales@company.example",)), \
             mock.patch.object(sdt, "_gmail_users",
                               lambda mb: _FakeGmail(self._msgs(5), page_size=2)):
            evidence, errors = sdt._collect_evidence(date(2026, 7, 1))
        self.assertEqual(len(evidence), 5)
        self.assertEqual(errors, [])


class _StateIsolatedCase(unittest.TestCase):
    # _run_check 會先掃 ContactW 通知信自動建案。多數測試不驗這條，預設 patch 掉
    # （否則會真的打 Gmail）。要驗建案本體的 case 把這個關成 False —— 用開關
    # 而不是在子類 stopall()：那會連別人的 patch 一起停掉（測試互污的老坑）。
    patch_discovery = True
    # 正本掃描同理：預設 patch 掉（不打 Gmail），驗正本的 case 自己關掉。
    patch_originals = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = sdt._STATE_PATH
        sdt._STATE_PATH = os.path.join(self._tmp.name, "state.json")
        if self.patch_discovery:
            patcher = mock.patch.object(sdt, "_discover_new_shipments",
                                        return_value=([], [], []))
            patcher.start()
            self.addCleanup(patcher.stop)
        if self.patch_originals:
            op = mock.patch.object(sdt, "_collect_originals", return_value=([], []))
            op.start()
            self.addCleanup(op.stop)

    def tearDown(self):
        sdt._STATE_PATH = self._orig_path
        self._tmp.cleanup()

    def _patch_evidence(self, evidence, errors=()):
        return mock.patch.object(sdt, "_collect_evidence",
                                 return_value=(list(evidence), list(errors)))


class ExcelPackingListTests(_StateIsolatedCase):
    """客人明文要求「電子版的裝箱單 (EXCEL)」——只查 has:attachment 的話附 PDF 也算過。
    實測附件名 `PKL LURCHI 2454PRS-CONT7.XLS`。只警示、不擋結案。"""

    def test_missing_excel_is_flagged_on_close(self):
        sdt.track_shipment("LURCHI-CONT7", "2026-08-07")
        with self._patch_evidence([
            _ev("FW: LURCHI-CONT7", date(2026, 8, 5), _BOTH, has_excel=False),
        ]):
            out = sdt._run_check(date(2026, 8, 20))
        self.assertIn("結案", out)          # 仍然結案 —— 只警示不擋
        self.assertIn("EXCEL", out)
        self.assertEqual(sdt._read_state()["shipments"]["LURCHI-CONT7"]["status"],
                         "closed")

    def test_present_excel_produces_no_warning(self):
        sdt.track_shipment("LURCHI-CONT7", "2026-08-07")
        with self._patch_evidence([
            _ev("FW: LURCHI-CONT7", date(2026, 8, 5), _BOTH, has_excel=True),
        ]):
            out = sdt._run_check(date(2026, 8, 20))
        self.assertIn("結案", out)
        self.assertNotIn("沒看到 Excel", out)

    def test_excel_flag_only_counts_mails_naming_this_container(self):
        """別櫃的信帶 Excel 不算這一櫃有交裝箱單。"""
        verdict = sdt._verify_shipment(
            {"ref": "LURCHI-CONT7", "etd": "2026-08-07"},
            [_ev("出貨文件", date(2026, 8, 5), _BOTH, has_excel=True)])
        self.assertFalse(verdict["excel"])


class RunCheckTests(_StateIsolatedCase):
    """排程主流程：到期才問、每天一次、查核通過自動結案。"""

    def test_quiet_when_nothing_tracked(self):
        with self._patch_evidence([]):
            self.assertEqual(sdt._run_check(date(2026, 8, 20)), "(無新發現)")

    def test_quiet_before_ask_window_and_no_gmail_call(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with self._patch_evidence([]) as collect:
            # ask_start = ETD+5-2 = 08-18；08-17 還不到 → 整輪安靜、連信箱都不掃。
            self.assertEqual(sdt._run_check(date(2026, 8, 17)), "(無新發現)")
            collect.assert_not_called()

    def test_ask_fires_once_per_day_and_mentions_missing_recipients(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15", pairs="6241 PRS")
        with self._patch_evidence([_ev("RE: LURCHI-CONT8", date(2026, 8, 20), {_CHEUNG})]):
            out = sdt._run_check(date(2026, 8, 20))
            self.assertIn("@UserAng", out)
            self.assertIn(_LAW, out)          # 缺 contact-w.law 要點名
            self.assertIn("6241 PRS", out)
            self.assertIn("兩位 ContactW", out)  # 要求摘要要帶到
            # 同一天再跑 → 不重複問。
            self.assertEqual(sdt._run_check(date(2026, 8, 20)), "(無新發現)")
            # 隔天沒結案 → 再問。
            self.assertIn("@UserAng", sdt._run_check(date(2026, 8, 21)))

    def test_strict_evidence_auto_closes(self):
        sdt.track_shipment("LURCHI-CONT7", "2026-08-07")
        with self._patch_evidence([
            _ev("FW: LURCHI-CONT7", date(2026, 8, 5), {_CHEUNG, _LAW}),
        ]):
            out = sdt._run_check(date(2026, 8, 20))
        self.assertIn("結案", out)
        rec = sdt._read_state()["shipments"]["LURCHI-CONT7"]
        self.assertEqual(rec["status"], "closed")
        self.assertTrue(rec["evidence"])

    def test_copies_closed_does_not_silence_the_originals_watch(self):
        """副本電郵結案 **不等於** 正本到了，而罰款條款主要就在正本那條 ——
        結案後仍要盯到客人簽收（這條紅了＝正本逾期會靜靜發生）。"""
        sdt.track_shipment("LURCHI-CONT7", "2026-08-07")   # 正本期限 08-17
        with self._patch_evidence([
            _ev("FW: LURCHI-CONT7", date(2026, 8, 5), {_CHEUNG, _LAW}),
        ]):
            sdt._run_check(date(2026, 8, 20))
            out = sdt._run_check(date(2026, 8, 21))
        self.assertIn("正本文件", out)
        self.assertIn("已過期限未見簽收", out)
        self.assertEqual(sdt._read_state()["shipments"]["LURCHI-CONT7"]["status"],
                         "closed")   # 結案狀態不因正本而被翻掉

    def test_sales_confirm_alone_never_closes(self):
        """只有口頭確認、信箱查無 → 不結案，訊息照實說查核未過。"""
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with sdt.locked_json(sdt._STATE_PATH, default={}) as st:
            st["shipments"]["LURCHI-CONT8"]["sales_confirmed_at"] = "2026-08-19T10:00:00"
        with self._patch_evidence([]):
            out = sdt._run_check(date(2026, 8, 20))
        self.assertIn("查核未過", out)
        self.assertEqual(sdt._read_state()["shipments"]["LURCHI-CONT8"]["status"], "open")

    def test_sales_confirm_plus_candidate_coverage_closes(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with sdt.locked_json(sdt._STATE_PATH, default={}) as st:
            st["shipments"]["LURCHI-CONT8"]["sales_confirmed_at"] = "2026-08-19T10:00:00"
        with self._patch_evidence([
            _ev("出貨文件如附", date(2026, 8, 19), {_CHEUNG}),
            _ev("出貨文件補寄", date(2026, 8, 19), {_LAW}),
        ]):
            out = sdt._run_check(date(2026, 8, 20))
        self.assertIn("結案", out)
        self.assertIn("主旨未含櫃號", out)
        self.assertEqual(sdt._read_state()["shipments"]["LURCHI-CONT8"]["status"], "closed")

    def test_mailbox_errors_surface_instead_of_silent_quiet(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        # 已問過今天 → 沒有新訊息可推；但信箱掛了要出聲，不能長得像「沒事」。
        with self._patch_evidence([], errors=["twsales@company.example：SA 金鑰讀不到"]):
            sdt._run_check(date(2026, 8, 20))
            out = sdt._run_check(date(2026, 8, 20))
        self.assertIn("信箱查核有問題", out)
        self.assertIn("SA 金鑰讀不到", out)


class ConfirmToolTests(_StateIsolatedCase):
    """業務在 Telegram 回「已處理」→ confirm_shipping_docs 的記錄＋查核＋結案。"""

    def test_unknown_ref_lists_tracked(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with self._patch_evidence([]):
            out = sdt.confirm_shipping_docs("CONT9")
        self.assertIn("查無櫃號", out)
        self.assertIn("LURCHI-CONT8", out)

    def test_empty_ref_with_single_open_shipment_auto_selects(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with self._patch_evidence([]):
            out = sdt.confirm_shipping_docs(note="已寄出")
        self.assertIn("LURCHI-CONT8", out)
        rec = sdt._read_state()["shipments"]["LURCHI-CONT8"]
        self.assertTrue(rec["sales_confirmed_at"])
        self.assertEqual(rec["sales_confirmed_note"], "已寄出")

    def test_empty_ref_with_multiple_open_asks_to_specify(self):
        sdt.track_shipment("LURCHI-CONT7", "2026-08-07")
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with self._patch_evidence([]):
            self.assertIn("請指定櫃號", sdt.confirm_shipping_docs())

    def test_confirm_with_strict_evidence_closes(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with self._patch_evidence([
            _ev("RE: LURCHI-CONT8 // 6241 PRS", date(2026, 8, 20), _BOTH),
        ]):
            out = sdt.confirm_shipping_docs("cont8", note="文件已安排寄出")
        self.assertIn("結案", out)
        self.assertEqual(sdt._read_state()["shipments"]["LURCHI-CONT8"]["status"], "closed")

    def test_confirm_without_evidence_stays_open_and_says_so(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with self._patch_evidence([]):
            out = sdt.confirm_shipping_docs("LURCHI-CONT8")
        self.assertIn("查核還沒過", out)
        self.assertIn(_CHEUNG, out)
        self.assertIn(_LAW, out)
        self.assertEqual(sdt._read_state()["shipments"]["LURCHI-CONT8"]["status"], "open")

    def test_confirm_on_closed_shipment_reports_closed(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with sdt.locked_json(sdt._STATE_PATH, default={}) as st:
            st["shipments"]["LURCHI-CONT8"]["status"] = "closed"
            st["shipments"]["LURCHI-CONT8"]["close_reason"] = "email 查核通過"
        with self._patch_evidence([]) as collect:
            out = sdt.confirm_shipping_docs("LURCHI-CONT8")
            collect.assert_not_called()
        self.assertIn("已經結案", out)


class TrackAndStatusTests(_StateIsolatedCase):
    def test_track_is_idempotent_and_preserves_progress(self):
        key, created = sdt.track_shipment("LURCHI-CONT8", "2026-08-14")
        self.assertTrue(created)
        with sdt.locked_json(sdt._STATE_PATH, default={}) as st:
            st["shipments"][key]["asked_dates"] = ["2026-08-19"]
        key2, created2 = sdt.track_shipment("lurchi-cont8", "2026-08-15", pairs="6241 PRS")
        self.assertEqual((key2, created2), (key, False))
        rec = sdt._read_state()["shipments"][key]
        self.assertEqual(rec["etd"], "2026-08-15")            # 新 ETD 蓋上去
        self.assertEqual(rec["asked_dates"], ["2026-08-19"])  # 進度保留

    def test_track_rejects_bad_etd(self):
        with self.assertRaises(ValueError):
            sdt.track_shipment("LURCHI-CONT9", "15 AUG 2026")
        self.assertIn("失敗", sdt.track_shipping_docs("LURCHI-CONT9", "15 AUG 2026"))

    def test_status_lists_open_and_closed(self):
        self.assertIn("沒有追蹤中", sdt.shipping_doc_status())
        sdt.track_shipment("LURCHI-CONT7", "2026-08-07")
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with sdt.locked_json(sdt._STATE_PATH, default={}) as st:
            sdt._close(st["shipments"]["LURCHI-CONT7"], "email 查核通過")
        out = sdt.shipping_doc_status()
        self.assertIn("✅ LURCHI-CONT7", out)
        self.assertIn("⏳ LURCHI-CONT8", out)
        self.assertIn("正本期限", out)
        # 指定櫃號 → 只看單櫃。
        self.assertNotIn("CONT8", sdt.shipping_doc_status("cont7"))


# ContactW 通知信的內文骨架（照 2026-08-20 那封原文濃縮，判準字樣逐字保留）。
_NOTICE_BODY = (
    "Dear UserAng,\n{lead}正本文件情況\n注意事項:\n"
    "文件須要電郵給兩位 WENDY (shipping-contact@customer-a.example & docs-contact@customer-a.example)\n"
    "*** 工廠必須提供電子版的裝箱單 (EXCEL) || 部份客戶要求指定格式\n"
    "*** 船開 5天 之內工廠須要以電郵方式提供副本文件(掃瞄), 10天 之內正本文件寄到本公司\n"
)


class NoticeParsingTests(unittest.TestCase):
    """ContactW 通知信 → 自動建案的解析。fixture 全部取自實際收到的信。"""

    def test_subject_with_explicit_etd(self):
        got = sdt.parse_doc_notice(
            "RE: LURCHI-CONT7 // 2454 PRS // ETD 07 AUG 2026",
            _NOTICE_BODY.format(lead=""))
        self.assertEqual(got["ref"], "LURCHI-CONT7")
        self.assertEqual(got["etd"], "2026-08-07")
        self.assertEqual(got["pairs"], "2454 PRS")
        self.assertEqual(got["problem"], "")

    def test_subject_with_bare_spaced_date(self):
        """CONT8 主旨沒寫 ETD 兩個字，但「15 AUG 2026」就是 ETD。"""
        got = sdt.parse_doc_notice(
            "RE: LURCHI-CONT8 // 6241 PRS // 15 AUG 2026",
            _NOTICE_BODY.format(lead=""))
        self.assertEqual((got["ref"], got["etd"], got["pairs"]),
                         ("LURCHI-CONT8", "2026-08-15", "6241 PRS"))

    def test_dotted_date_in_subject_is_never_the_etd(self):
        """🚨最關鍵的一條：CONT10 主旨的 21.08.2026 是**發文日**，
        真 ETD 10 AUG 2026 在內文。抓錯會讓期限整整晚 11 天、催信全部失準。"""
        got = sdt.parse_doc_notice(
            "RE: LURCHI ( KIENAST)-CONT 10 // 21.08.2026",
            _NOTICE_BODY.format(lead="26A014062 26A014064 26A014066 ETD 10 AUG 2026\n"))
        self.assertEqual(got["ref"], "LURCHI-CONT10")
        self.assertEqual(got["etd"], "2026-08-10")
        self.assertNotEqual(got["etd"], "2026-08-21")

    def test_body_etd_with_full_month_name(self):
        got = sdt.parse_doc_notice(
            "RE: LURCHI-CONT5 // L200726-5 // 28.07.2026",
            _NOTICE_BODY.format(lead="ETD 23 JULY 2026 2812 PRS 339 CTNS\n"))
        self.assertEqual((got["ref"], got["etd"], got["pairs"]),
                         ("LURCHI-CONT5", "2026-07-23", "2812 PRS"))

    def test_draft_docs_prefix_does_not_become_the_brand(self):
        got = sdt.parse_doc_notice(
            "RE: DRAFT DOCS LURCHI-CONT 3 // L060726-3 ETD 10 JULY 2026",
            _NOTICE_BODY.format(lead=""))
        self.assertEqual((got["ref"], got["etd"]), ("LURCHI-CONT3", "2026-07-10"))

    def test_missing_etd_is_reported_not_guessed(self):
        """只有點分日期時寧可回報請人補，也不可拿發文日當 ETD。"""
        got = sdt.parse_doc_notice("RE: LURCHI-CONT4 // 04.08.2026",
                                   _NOTICE_BODY.format(lead=""))
        self.assertEqual(got["ref"], "LURCHI-CONT4")
        self.assertIsNone(got["etd"])
        self.assertIn("ETD", got["problem"])

    def test_multi_container_with_one_etd_creates_them_all(self):
        """整封只有一個 ETD 時，那個 ETD 是全櫃的共同前提、不是猜的。"""
        got = sdt.parse_doc_notice("RE: LURCHI-CONT 9+ 10 // ETD 10 AUG 2026",
                                   _NOTICE_BODY.format(lead=""))
        self.assertEqual(got["problem_kind"], "multi_same_etd")
        self.assertEqual(got["etd"], "2026-08-10")
        self.assertEqual(got["numbers"], [9, 10])
        self.assertEqual(got["brand"], "LURCHI")
        self.assertEqual(got["pairs"], "", "雙數是整封總數，不可掛到單一櫃上")

    def test_multi_container_with_conflicting_etds_stays_manual(self):
        """兩個不同 ETD → 不知道哪個屬於哪櫃，一律丟人工，絕不分配。"""
        got = sdt.parse_doc_notice(
            "RE: LURCHI-CONT 9+ 10",
            _NOTICE_BODY.format(lead="CONT9 ETD 10 AUG 2026 / CONT10 ETD 15 AUG 2026\n"))
        self.assertEqual(got["problem_kind"], "multi")
        self.assertIsNone(got["etd"])
        self.assertIn("沒有唯一的 ETD", got["problem"])

    def test_multi_container_without_etd_stays_manual(self):
        got = sdt.parse_doc_notice("RE: LURCHI-CONT 9+ 10 // 21.08.2026",
                                   _NOTICE_BODY.format(lead=""))
        self.assertEqual(got["problem_kind"], "multi")
        self.assertIsNone(got["etd"])

    def test_subject_without_container_number(self):
        got = sdt.parse_doc_notice("RE: Lurchi sample QC",
                                   _NOTICE_BODY.format(lead=""))
        self.assertIn("櫃號", got["problem"])


class DiscoveryTests(_StateIsolatedCase):
    """掃通知信 → 自動建案：只建新的、處理過的不重報、抓不到 ETD 要出聲。"""

    patch_discovery = False  # 這個 case 驗的就是 _discover_new_shipments 本體

    def _patch_notices(self, notices, errors=()):
        return mock.patch.object(sdt, "_fetch_notices",
                                 return_value=(list(notices), list(errors)))

    def _notice(self, mid, subject, body_lead=""):
        parsed = sdt.parse_doc_notice(subject, _NOTICE_BODY.format(lead=body_lead))
        return {"id": mid, "skip": False, "subject": subject, **parsed}

    def test_new_container_is_created_and_not_recreated(self):
        notices = [self._notice("m1", "RE: LURCHI-CONT11 // 3000 PRS // ETD 05 SEP 2026")]
        with self._patch_notices(notices):
            created, manual, errors = sdt._discover_new_shipments(30)
        self.assertEqual(len(created), 1)
        self.assertIn("LURCHI-CONT11", created[0])
        self.assertEqual((manual, errors), ([], []))
        rec = sdt._read_state()["shipments"]["LURCHI-CONT11"]
        self.assertEqual((rec["etd"], rec["pairs"]), ("2026-09-05", "3000 PRS"))
        # 同一封再掃到 → 已記錄過，不重報（_fetch_notices 收到 seen 就會跳過）。
        with mock.patch.object(sdt, "_fetch_notices") as fetch:
            fetch.return_value = ([], [])
            sdt._discover_new_shipments(30)
            seen_arg = fetch.call_args[0][1]
        self.assertIn("m1", seen_arg)

    def test_notice_without_etd_asks_for_human_help(self):
        with self._patch_notices([self._notice("m2", "RE: LURCHI-CONT4 // 04.08.2026")]):
            created, manual, _ = sdt._discover_new_shipments(30)
        self.assertEqual(created, [])
        self.assertEqual(len(manual), 1)
        self.assertIn("ETD", manual[0])
        # 建不了案就不可以留半截紀錄。
        self.assertNotIn("LURCHI-CONT4", sdt._read_state()["shipments"])

    def test_existing_container_is_not_reported_as_new(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with self._patch_notices([
            self._notice("m3", "RE: LURCHI-CONT8 // 6241 PRS // 15 AUG 2026")]):
            created, manual, _ = sdt._discover_new_shipments(30)
        self.assertEqual((created, manual), ([], []))

    def test_boilerplate_on_other_topics_is_silent(self):
        """ContactW 把「注意事項」那段當罐頭簽名附在別的主題上（實測近 60 天 3 封）
        —— 主旨沒櫃號就安靜跳過，不可報成「待人工處理」洗版。"""
        notices = [self._notice("m4", "RE: 26AW OUTSTAING PAYMENT LIST 18.07.2026"),
                   self._notice("m5", "RE: LURCHI- 26A292040 072 074 ((2871 PRS)")]
        with self._patch_notices(notices):
            created, manual, _ = sdt._discover_new_shipments(30)
        self.assertEqual((created, manual), ([], []))

    def test_same_container_two_notices_reports_once_regardless_of_order(self):
        """同櫃常有兩封（一封帶 ETD、一封只有發文日）。不管 Gmail 回哪個在前，
        都該建案成功且**不**再喊「請補 ETD」——忽報忽不報的噪音最難查。"""
        good = self._notice("g", "RE: LURCHI-CONT5 // L200726-5 // 28.07.2026",
                            body_lead="ETD 23 JULY 2026 2812 PRS 339 CTNS\n")
        bad = self._notice("b", "RE: LURCHI-CONT5 // 2812 PRS // FUCHUN")
        for order in ([good, bad], [bad, good]):
            with self.subTest(order=[n["id"] for n in order]):
                sdt.track_shipment  # noqa: B018 —— 只是讓意圖清楚
                with mock.patch.object(sdt, "_STATE_PATH",
                                       os.path.join(self._tmp.name, f"s{order[0]['id']}.json")):
                    with self._patch_notices(order):
                        created, manual, _ = sdt._discover_new_shipments(30)
                self.assertEqual(manual, [])
                self.assertEqual(len(created), 1)
                self.assertIn("2026-07-23", created[0])

    def test_multi_container_with_one_etd_is_created_for_every_container(self):
        notice = self._notice("m9", "RE: LURCHI-CONT 9+ 10 // ETD 10 AUG 2026")
        with self._patch_notices([notice]):
            created, manual, _ = sdt._discover_new_shipments(30)
        self.assertEqual(manual, [])
        self.assertEqual(len(created), 1)
        shipments = sdt._read_state()["shipments"]
        self.assertIn("LURCHI-CONT9", shipments)
        self.assertIn("LURCHI-CONT10", shipments)
        self.assertEqual(shipments["LURCHI-CONT9"]["etd"], "2026-08-10")
        # 雙數是整封總數 → 不可掛到單櫃身上（會被當成該櫃的雙數念出去）。
        self.assertEqual(shipments["LURCHI-CONT9"]["pairs"], "")

    def test_multi_container_notice_silent_when_all_already_tracked(self):
        """業務把三櫃文件併成一封寄 —— 那是查核證據不是「有新櫃」，別喊人工處理。"""
        for n in (8, 9, 10):
            sdt.track_shipment(f"LURCHI-CONT{n}", "2026-08-15")
        notice = self._notice(
            "m6", "RE: LURCHI ( KIENAST)-CONT 9+ 10 and Lurchi CNT-8 shipping documents")
        with self._patch_notices([notice]):
            created, manual, _ = sdt._discover_new_shipments(30)
        self.assertEqual((created, manual), ([], []))

    def test_multi_container_notice_reported_once_when_untracked(self):
        notices = [self._notice("m7", "RE: LURCHI-CONT 9+ 10 docs"),
                   self._notice("m8", "RE: LURCHI-CONT 9+ 10 docs")]
        with self._patch_notices(notices):
            _, manual, _ = sdt._discover_new_shipments(30)
        self.assertEqual(len(manual), 1)

    def test_multi_notice_never_overwrites_a_containers_own_etd(self):
        """🚨 實測：「CONT 9+ 10 // ETD 10 AUG」那封也提到 CNT-8，但 CONT8 自己的
        通知寫 15 AUG。多櫃信只能補**還沒有的櫃**，不可蓋掉精確的 ETD。"""
        single = self._notice("s1", "RE: LURCHI-CONT8 // 6241 PRS // 15 AUG 2026")
        multi = self._notice("m10",
                             "RE: LURCHI ( KIENAST)-CONT 9+ 10 and Lurchi CNT-8 "
                             "// ETD 10 AUG 2026")
        for order in ([single, multi], [multi, single]):
            with self.subTest(order=[n["id"] for n in order]):
                with mock.patch.object(sdt, "_STATE_PATH",
                                       os.path.join(self._tmp.name,
                                                    f"o{order[0]['id']}.json")):
                    with self._patch_notices(order):
                        sdt._discover_new_shipments(30)
                    ships = sdt._read_state()["shipments"]
                self.assertEqual(ships["LURCHI-CONT8"]["etd"], "2026-08-15")
                self.assertEqual(ships["LURCHI-CONT9"]["etd"], "2026-08-10")

    def test_draft_docs_for_other_customers_are_ignored(self):
        """🚨 Ms. Hao 也負責 JALAS 等別的客戶。這套是 **Supremo** 的規則
        （兩位 ContactW、ETD+5/+10、罰款條款），別拿去催不同要求的客戶。"""
        fake = _FakeGmail([{"id": "j1", "to": "twsales@company.example",
                            "subject": "JALAS-CONT5",
                            "from": "shipping@company.example"}], page_size=5)
        with mock.patch.object(sdt, "_gmail_users", lambda mb: fake):
            notices, _ = sdt._fetch_notices(30, set())
        self.assertEqual([n for n in notices if not n.get("skip")], [],
                         "JALAS 不該進 Supremo 的追蹤流程")

    def test_draft_docs_from_shipping_flags_a_new_container_needing_etd(self):
        """船務 Ms. Hao 的草稿文件信比 ContactW 的通知早好幾天，但**沒有 ETD**
        （實測內文只有 "Pls check & CFM"）——只能報「請補 ETD」，不可自己生期限。"""
        parsed = sdt.parse_doc_notice("LURCHI-CONT11", "Dear Ms UserAng\nPls check & CFM")
        self.assertEqual(parsed["ref"], "LURCHI-CONT11")
        self.assertIsNone(parsed["etd"])
        notice = {"id": "d1", "skip": False, "subject": "LURCHI-CONT11", **parsed,
                  "problem": "船務已在跑這一櫃的文件，但信中沒有 ETD",
                  "problem_kind": "no_etd"}
        with self._patch_notices([notice]):
            created, manual, _ = sdt._discover_new_shipments(30)
        self.assertEqual(created, [])
        self.assertEqual(len(manual), 1)
        self.assertIn("LURCHI-CONT11", manual[0] + str(notice))
        self.assertNotIn("LURCHI-CONT11", sdt._read_state()["shipments"])

    def test_draft_docs_silent_once_the_container_is_already_tracked(self):
        """ContactW 的通知已經把櫃建好時，船務那封不該再喊一次。"""
        sdt.track_shipment("LURCHI-CONT11", "2026-09-05")
        parsed = sdt.parse_doc_notice("LURCHI-CONT11", "Pls check & CFM")
        notice = {"id": "d2", "skip": False, "subject": "LURCHI-CONT11", **parsed,
                  "problem": "船務已在跑這一櫃的文件，但信中沒有 ETD",
                  "problem_kind": "no_etd"}
        with self._patch_notices([notice]):
            created, manual, _ = sdt._discover_new_shipments(30)
        self.assertEqual((created, manual), ([], []))

    def test_run_check_surfaces_new_containers(self):
        with mock.patch.object(sdt, "_discover_new_shipments",
                               return_value=(["　- LURCHI-CONT11　ETD 2026-09-05"],
                                             ["　- 「X」：信中找不到 ETD"], [])), \
             mock.patch.object(sdt, "_collect_evidence", return_value=([], [])):
            out = sdt._run_check(date(2026, 8, 25))
        self.assertIn("偵測到新櫃", out)
        self.assertIn("LURCHI-CONT11", out)
        self.assertIn("自動建案失敗", out)

    def test_notice_scan_error_is_not_silent(self):
        with mock.patch.object(sdt, "_discover_new_shipments",
                               return_value=([], [], ["twsales@：SA 金鑰讀不到"])), \
             mock.patch.object(sdt, "_collect_evidence", return_value=([], [])):
            out = sdt._run_check(date(2026, 8, 25))
        self.assertIn("通知信掃描有問題", out)


class EscalationTests(_StateIsolatedCase):
    """升級線：平常安靜，逾期超過門檻才吵大王。"""

    def test_quiet_when_nothing_is_late(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")   # 副本期限 08-20
        self.assertEqual(sdt._run_escalation(date(2026, 8, 21)), "(無新發現)")

    def test_copies_overdue_beyond_grace_escalates(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        # 期限 08-20 + grace 3 → 08-24 才越線；08-23 還不吵。
        self.assertEqual(sdt._run_escalation(date(2026, 8, 23)), "(無新發現)")
        out = sdt._run_escalation(date(2026, 8, 24))
        self.assertIn("副本文件電郵逾期", out)
        self.assertIn("業務尚未在 Telegram 回覆", out)

    def test_closed_container_is_not_escalated_for_copies(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with sdt.locked_json(sdt._STATE_PATH, default={}) as st:
            sdt._close(st["shipments"]["LURCHI-CONT8"], "email 查核通過")
            sdt._originals_state(st["shipments"]["LURCHI-CONT8"])["status"] = "acked"
        self.assertEqual(sdt._run_escalation(date(2026, 9, 30)), "(無新發現)")

    def test_originals_overdue_escalates_even_when_copies_closed(self):
        """副本結案但正本沒簽收 —— 這正是罰款會靜靜發生的那個缺口。"""
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")   # 正本期限 08-25
        with sdt.locked_json(sdt._STATE_PATH, default={}) as st:
            rec = st["shipments"]["LURCHI-CONT8"]
            sdt._close(rec, "email 查核通過")
            # 掃描成功過、確認信箱裡真的沒有簽收 → 才算得上逾期。
            sdt._apply_originals(rec, [], date(2026, 8, 26), scan_ok=True)
        out = sdt._run_escalation(date(2026, 8, 29))
        self.assertIn("正本文件未見簽收", out)
        self.assertIn("查不到我方寄出紀錄", out)

    def test_failed_scan_never_escalates_as_overdue(self):
        """🚨 2026-08-27 09:00 實際踩過：機器 DNS 掛掉 → Gmail 全失敗 → 正本掃描
        零訊號 → 四櫃被標 overdue → 10:02 對大王發出四櫃假警報，而那些正本其實
        早就簽收了。掃描失敗只能說「查核不到」，不能說「逾期」。"""
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with sdt.locked_json(sdt._STATE_PATH, default={}) as st:
            rec = st["shipments"]["LURCHI-CONT8"]
            sdt._close(rec, "email 查核通過")
            sdt._apply_originals(rec, [], date(2026, 8, 26), scan_ok=False)
        out = sdt._run_escalation(date(2026, 8, 29))
        self.assertIn("查核不到", out)
        self.assertNotIn("正本文件未見簽收", out)
        self.assertEqual(
            sdt._read_state()["shipments"]["LURCHI-CONT8"]["originals"]["status"],
            "unverified")

    def test_self_reported_ack_stops_the_escalation(self):
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with sdt.locked_json(sdt._STATE_PATH, default={}) as st:
            rec = st["shipments"]["LURCHI-CONT8"]
            sdt._close(rec, "email 查核通過")
            sdt._originals_state(rec)["status"] = "acked_by_us"
        self.assertEqual(sdt._run_escalation(date(2026, 8, 29)), "(無新發現)")

    def test_escalation_never_touches_the_mailbox(self):
        """升級線只讀狀態檔 —— 掃信箱是主任務的事，重複掃會多花額度也會打架。"""
        sdt.track_shipment("LURCHI-CONT8", "2026-08-15")
        with mock.patch.object(sdt, "_gmail_users",
                               side_effect=AssertionError("不該碰 Gmail")):
            sdt._run_escalation(date(2026, 8, 29))


class TaskDefinitionTests(unittest.TestCase):
    """排程設定：走 deterministic、推橙色、09:00 視窗一次。"""

    def test_schedule_window(self):
        self.assertEqual(reg._TASK["deterministic_tool"], "shipping_doc_check")
        self.assertEqual(reg._TASK["start_hour"], 9)
        self.assertEqual(reg._TASK["end_hour"], 10)
        self.assertEqual(reg._TASK["interval_minutes"], 60)

    def test_notifies_orange_only_no_email(self):
        self.assertEqual(reg._TASK["notify_agent_colors"], ["orange"])
        self.assertEqual(reg._TASK["notify_channel"], "telegram")
        self.assertEqual(reg._TASK["notify_emails"], [])

    def test_escalation_task_runs_after_the_main_task(self):
        """升級線只讀主任務寫好的狀態 → 視窗必須排在主任務**之後**。
        ⚠️ scheduler 沒有 start_minute 欄位，不可用同視窗+分鐘數來排序。"""
        self.assertNotIn("start_minute", reg._ESCALATION_TASK)
        self.assertGreaterEqual(reg._ESCALATION_TASK["start_hour"],
                                reg._TASK["end_hour"])
        self.assertEqual(reg._ESCALATION_TASK["notify_agent_colors"], ["red"])

    def test_deterministic_tool_reachable_for_background_dispatcher(self):
        """預演 health_check / 部署機測試：工具進不了背景工具集＝每輪都失敗（#350）。"""
        from agent_core.daemon_dispatcher import audit_task_tool_refs
        from agent_core.tool_registry import tools_list
        task = dict(reg._TASK)
        task.update({"last_run_at": None, "run_count": 0, "dedup_hashes": []})
        esc = dict(reg._ESCALATION_TASK)
        esc.update({"last_run_at": None, "run_count": 0, "dedup_hashes": []})
        problems = audit_task_tool_refs([task, esc], tools_list)
        self.assertEqual(problems, [], "任務點名了進不了背景工具集的工具：\n  "
                         + "\n  ".join(problems))

    def test_seed_shipments_match_wendy_notices(self):
        refs = {s["ref"]: s for s in reg._SEED_SHIPMENTS}
        self.assertEqual(refs["LURCHI-CONT7"]["etd"], "2026-08-07")
        self.assertEqual(refs["LURCHI-CONT8"]["etd"], "2026-08-15")


class RegisterScriptTests(_StateIsolatedCase):
    """冪等：新增 → 更新（保留 runtime 狀態）→ 刪除；種子櫃跟著種進狀態檔。"""

    def _run_seed(self, data: dict) -> list[str]:
        with mock.patch.object(reg, "update_daemon_tasks",
                               side_effect=lambda fn: (fn(data), data)[1]):
            return reg.seed()

    def test_seed_add_then_update_preserves_runtime_fields(self):
        data: dict = {"tasks": []}
        self._run_seed(data)
        names = [t["name"] for t in data["tasks"]]
        self.assertEqual(names, [reg.TASK_NAME, reg.ESCALATION_TASK_NAME])
        # 模擬兩支都跑過幾輪。
        for task in data["tasks"]:
            task["last_run_at"] = datetime(2026, 8, 21, 9, 1).isoformat()
            task["run_count"] = 3
        self._run_seed(data)
        self.assertEqual(len(data["tasks"]), 2, "重跑不可長出重複任務")
        for task in data["tasks"]:
            self.assertEqual(task["run_count"], 3)
            self.assertIsNotNone(task["last_run_at"])
        # 種子櫃寫進（隔離後的）狀態檔，重跑不重置。
        shipments = sdt._read_state()["shipments"]
        self.assertIn("LURCHI-CONT7", shipments)
        self.assertIn("LURCHI-CONT8", shipments)

    def test_remove_deletes_task_but_keeps_state(self):
        data: dict = {"tasks": []}
        self._run_seed(data)
        with mock.patch.object(reg, "update_daemon_tasks",
                               side_effect=lambda fn: (fn(data), data)[1]):
            reg.remove()
        self.assertEqual(data["tasks"], [], "兩支排程都要移除，別留下孤兒升級線")
        self.assertIn("LURCHI-CONT7", sdt._read_state()["shipments"])


class FreeformWhitelistTests(unittest.TestCase):
    """橙色員工 freeform 拿得到兩顆工具；confirm 不隨矩陣外流到別色。"""

    def test_orange_gets_both_tools(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        allowed = allowed_tool_names_for_color("orange")
        self.assertIn("shipping_doc_status", allowed)
        self.assertIn("confirm_shipping_docs", allowed)

    def test_confirm_is_home_only(self):
        from agent_core.dept_tool_scope import allowed_tool_names_for_color
        # yellow/blue 等色經 QUERY_MATRIX 可查 orange → status 可繼承、confirm 不行。
        for color in ("yellow", "blue", "purple", "gray", "black"):
            with self.subTest(color=color):
                allowed = allowed_tool_names_for_color(color)
                self.assertIn("shipping_doc_status", allowed)
                self.assertNotIn("confirm_shipping_docs", allowed)

    def test_both_tools_are_safe_tier(self):
        """freeform 白名單會再與 SAFE tier 交集——不是 SAFE 等於整條流程靜默斷掉。"""
        from agent_core.tool_tiers import get_tier, TIER_SAFE
        self.assertEqual(get_tier("shipping_doc_status"), TIER_SAFE)
        self.assertEqual(get_tier("confirm_shipping_docs"), TIER_SAFE)


if __name__ == "__main__":
    unittest.main()
