"""採購每日簡報（UserA/UserM）—— 出貨通知信解析、待辦分類、附件白名單。

主旨解析的 fixture 全部抄自 2026-04~08 email lake 的**真實主旨**（不是想像的
格式）：出貨通知的欄位順序、標點、有無「For 客戶」、括號裡放什麼，各封都不太
一樣，測試要蓋住的就是這些變體。運費/付款通知那兩則是回歸案例——它們也帶 LOT
與 ETD，曾經蓋掉同 LOT 欄位齊全的真通知。
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, timedelta
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# 真實出貨通知主旨（email lake 原文）。
_NOTICE_FULL = ("21 Rolls on 2 Pallets --For Jalas , LOT 302-2026 ( STOCKMAYER ), "
                "CFS , ETD: 4/20 , ETA (CAT LAI): 6/8  -- 預計 6/11可到福群工廠 。")
_NOTICE_NO_FOR = ("  79 PACKAGES -- LOT 106-2026 (三宏 ,江門利華新), LURCHI/RICHTER,  "
                  "CFS , ETD: 5/12, ETA (CAT LAI): 5/16 , 預計 5/20可到福群工廠 。")
_NOTICE_UNDERSCORE = ("10 PKG -For Lurchi_ LOT.JFH177-2026 (隆丰 模具 ),  ETD: 4/23 , "
                      "ETA (CAT LAI): 4/28, 預計 5/1 可到福群工廠 。")
_NOTICE_NO_CUSTOMER = ("80 ROLLS in 4 boxes , LOT 214-2026,(威德適 ), CFS , "
                       "ETD: 7/16 , ETA (CAT LAI): 7/18, 預計 7/22 可到福群工廠 。")
_NOTICE_TIGHT = ("69 ROLLS --For Decathlon LOT 209-2026 (富泰),CFS, ETD: 4/16 ,"
                 "ETA(CAT LAI):4/21 -- 預計 4/24可到福群工廠 。")
_NOTICE_REPLY = ("Re: 9 PACKAGES -- For Jalas , LOT 321-2026(BIAGIOLI) , CFS , "
                 "ETD: 7/15, ETA (CAT LAI): 7/18 -- 預計 7/22可到福群工廠 。")
# 不是出貨通知，但都帶 LOT + ETD —— 曾經被誤收成出口明細。
_FREIGHT_MAIL = ("鴻泰 -- LOT 317-2026 (TEBO) -- JALAS , 佳桀付運費 -- NT$ 45258 "
                 "-- 付款到期日: 2026/9/19 (=ETD:7/19+60天)")
_ADVICE_UNLABELLED = "VN Shipping advice LOT325-2026 ISCO SEA (ETD 7/13 ETA 9/5    )"


class ShipmentSubjectParseTests(unittest.TestCase):
    def setUp(self):
        from agent_core import purchasing_brief
        self.pb = purchasing_brief
        self.anchor = date(2026, 7, 20)

    def _parse(self, subject, anchor=None):
        return self.pb.parse_shipment_subject(subject, anchor or self.anchor)

    def test_full_notice_all_columns(self):
        row = self._parse(_NOTICE_FULL, date(2026, 4, 22))
        self.assertEqual(row["lot"], "302-2026")
        self.assertEqual(row["supplier"], "STOCKMAYER")
        self.assertEqual(row["customer"], "Jalas")
        self.assertEqual(row["qty"], "21 Rolls on 2 Pallets")
        self.assertEqual(row["etd"], date(2026, 4, 20))
        self.assertEqual(row["eta_catlai"], date(2026, 6, 8))
        self.assertEqual(row["eta_fuchun"], date(2026, 6, 11))

    def test_customer_after_supplier_when_no_for_keyword(self):
        row = self._parse(_NOTICE_NO_FOR, date(2026, 5, 13))
        self.assertEqual(row["lot"], "106-2026")
        self.assertEqual(row["supplier"], "三宏、江門利華新")
        self.assertEqual(row["customer"], "LURCHI/RICHTER")
        self.assertEqual(row["qty"], "79 PACKAGES")

    def test_lot_with_dot_and_underscore_customer(self):
        row = self._parse(_NOTICE_UNDERSCORE, date(2026, 4, 23))
        self.assertEqual(row["lot"], "JFH177-2026")
        self.assertEqual(row["customer"], "Lurchi")
        self.assertEqual(row["qty"], "10 PKG")

    def test_notice_without_customer_still_parses(self):
        row = self._parse(_NOTICE_NO_CUSTOMER, date(2026, 7, 16))
        self.assertEqual(row["supplier"], "威德適")
        self.assertEqual(row["customer"], "")
        self.assertEqual(row["qty"], "80 ROLLS in 4 boxes")

    def test_tight_punctuation_variant(self):
        row = self._parse(_NOTICE_TIGHT, date(2026, 4, 16))
        self.assertEqual(row["customer"], "Decathlon")
        self.assertEqual(row["supplier"], "富泰")
        self.assertEqual(row["eta_catlai"], date(2026, 4, 21))

    def test_reply_prefix_does_not_eat_quantity(self):
        """Re:/REVISED 前綴把數量錨點擋掉 —— 轉寄鏈上的同一封通知會少一欄。"""
        row = self._parse(_NOTICE_REPLY, date(2026, 7, 16))
        self.assertEqual(row["qty"], "9 PACKAGES")
        self.assertEqual(row["supplier"], "BIAGIOLI")

    def test_freight_mail_is_not_a_shipment_notice(self):
        """運費/付款信也有 LOT+ETD，但沒有標名的 ETA → 不算出貨通知。"""
        self.assertIsNone(self._parse(_FREIGHT_MAIL, date(2026, 7, 24)))

    def test_unlabelled_eta_advice_rejected(self):
        """ETA 沒標 CAT LAI / FU CHUN 時無法歸欄，寧可不收也不要放錯欄。"""
        self.assertIsNone(self._parse(_ADVICE_UNLABELLED, date(2026, 7, 16)))

    def test_non_shipment_subject_returns_none(self):
        self.assertIsNone(self._parse("RE: 明天的會議", date(2026, 7, 20)))
        self.assertIsNone(self._parse("", date(2026, 7, 20)))


class SupplierExtractionTests(unittest.TestCase):
    def setUp(self):
        from agent_core import purchasing_brief
        self.pb = purchasing_brief

    def test_shipping_dates_in_parens_are_not_a_supplier(self):
        self.assertEqual(self.pb._first_supplier(_ADVICE_UNLABELLED), "")

    def test_cat_lai_paren_skipped_in_favour_of_real_supplier(self):
        self.assertEqual(self.pb._first_supplier(_NOTICE_FULL), "STOCKMAYER")


class DateResolutionTests(unittest.TestCase):
    def setUp(self):
        from agent_core import purchasing_brief
        self.pb = purchasing_brief

    def test_picks_year_closest_to_mail_date(self):
        self.assertEqual(self.pb._resolve_md("6/8", date(2026, 4, 22)),
                         date(2026, 6, 8))

    def test_crosses_year_boundary_forward(self):
        """12 月寄的信講「1/5 到」是明年 —— 直接套信件年份會差一整年。"""
        self.assertEqual(self.pb._resolve_md("1/5", date(2026, 12, 29)),
                         date(2027, 1, 5))

    def test_crosses_year_boundary_backward(self):
        self.assertEqual(self.pb._resolve_md("12/28", date(2027, 1, 3)),
                         date(2026, 12, 28))

    def test_impossible_date_returns_none(self):
        self.assertIsNone(self.pb._resolve_md("2/30", date(2026, 4, 1)))
        self.assertIsNone(self.pb._resolve_md("亂寫", date(2026, 4, 1)))


class ShipmentStatusTests(unittest.TestCase):
    def setUp(self):
        from agent_core import purchasing_brief
        self.pb = purchasing_brief
        self.row = {"etd": date(2026, 7, 15), "eta_catlai": date(2026, 7, 18),
                    "eta_fuchun": date(2026, 7, 22)}

    def test_before_etd(self):
        self.assertEqual(self.pb._shipment_status(self.row, date(2026, 7, 10)),
                         "待出貨")

    def test_at_sea(self):
        self.assertEqual(self.pb._shipment_status(self.row, date(2026, 7, 16)),
                         "海上運送中")

    def test_landed_at_cat_lai(self):
        self.assertEqual(self.pb._shipment_status(self.row, date(2026, 7, 20)),
                         "已抵 CAT LAI（清關/內陸）")

    def test_should_have_arrived(self):
        self.assertEqual(self.pb._shipment_status(self.row, date(2026, 7, 25)),
                         "應已到福群（預計）")


class LotDeduplicationTests(unittest.TestCase):
    """同 LOT 多封信：欄位齊的優先，一樣齊才比新。"""

    def setUp(self):
        from agent_core import purchasing_brief
        self.pb = purchasing_brief

    def _rows(self, records):
        import pandas as pd

        from agent_core import email_lake
        frame = pd.DataFrame(records)
        # 要 patch 的是**來源模組**的屬性：_lake_shipment_rows 是在函式內
        # `from agent_core.email_lake import _lake_load_df`，patch 到
        # purchasing_brief 身上會靜默失效（記憶「Shim patch gotcha」）。
        with mock.patch.object(email_lake, "_lake_load_df", return_value=frame):
            rows, warn = self.pb._lake_shipment_rows(date(2026, 7, 1),
                                                     date(2026, 8, 1))
        self.assertEqual(warn, "")
        return rows

    def test_later_incomplete_mail_does_not_override_full_notice(self):
        rows = self._rows([
            {"subject": ("14 PACKAGES --For Jalas , LOT 317-2026 ( TEBO ), CFS , "
                         "ETD: 7/19 , ETA (CAT LAI): 7/26-- 預計 7/29到福群工廠 。"),
             "date": "2026-07-22", "attachment_names": "", "body_snippet": ""},
            {"subject": _FREIGHT_MAIL, "date": "2026-07-24",
             "attachment_names": "", "body_snippet": ""},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], "14 PACKAGES")
        self.assertEqual(rows[0]["eta_fuchun"], date(2026, 7, 29))

    def test_revised_notice_wins_when_equally_complete(self):
        original = ("26 PACKAGES --For Jalas , LOT 303-2026 ( TEBO ), CFS , "
                    "ETD: 7/21 , ETA (CAT LAI): 7/15-- 預計 7/18 可到福群工廠 。")
        revised = ("REVISED : 26 PACKAGES --For Jalas , LOT 303-2026 ( TEBO ), CFS , "
                   "ETD: 7/21 , ETA (CAT LAI): 7/25-- 預計 7/28 可到福群工廠 。")
        rows = self._rows([
            {"subject": original, "date": "2026-07-05",
             "attachment_names": "", "body_snippet": ""},
            {"subject": revised, "date": "2026-07-08",
             "attachment_names": "", "body_snippet": ""},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["eta_fuchun"], date(2026, 7, 28))

    def test_month_filter_keeps_in_transit_batches(self):
        """ETD 在上個月、本月才到廠的在途批也要留 —— 追的是同一批貨。"""
        rows = self._rows([
            {"subject": ("11 PACKAGES -- LOT 107-2026 (三宏) , CFS , ETD: 6/27, "
                         "ETA (CAT LAI): 7/1 , 預計 7/4 可到福群工廠 。"),
             "date": "2026-06-27", "attachment_names": "", "body_snippet": ""},
        ])
        self.assertEqual([r["lot"] for r in rows], ["107-2026"])

    def test_out_of_month_batch_excluded(self):
        rows = self._rows([
            {"subject": ("11 PACKAGES -- LOT 001-2026 (三宏) , CFS , ETD: 1/5, "
                         "ETA (CAT LAI): 1/9 , 預計 1/12 可到福群工廠 。"),
             "date": "2026-01-05", "attachment_names": "", "body_snippet": ""},
        ])
        self.assertEqual(rows, [])


class PeriodBoundsTests(unittest.TestCase):
    def setUp(self):
        from agent_core import purchasing_brief
        self.pb = purchasing_brief

    def test_explicit_month(self):
        start, end, label = self.pb._period_bounds("2026-07")
        self.assertEqual((start, end, label),
                         (date(2026, 7, 1), date(2026, 8, 1), "2026-07"))

    def test_december_rolls_into_next_year(self):
        start, end, _ = self.pb._period_bounds("2026-12")
        self.assertEqual((start, end), (date(2026, 12, 1), date(2027, 1, 1)))

    def test_bare_year_covers_whole_year(self):
        """UserA 的「2026 出口文件整理」是整年，不是本月。"""
        start, end, label = self.pb._period_bounds("2026")
        self.assertEqual((start, end, label),
                         (date(2026, 1, 1), date(2027, 1, 1), "2026"))

    def test_out_of_range_year_falls_back_to_current_month(self):
        """四位數字什麼都收會讓 date(year + 1, …) 在 9999 炸掉。"""
        today = date.today()
        start, _, label = self.pb._period_bounds("9999")
        self.assertEqual(start, date(today.year, today.month, 1))
        self.assertEqual(label, f"{today.year}-{today.month:02d}")

    def test_garbage_falls_back_to_current_month(self):
        today = date.today()
        start, _, label = self.pb._period_bounds("не месяц")
        self.assertEqual(start, date(today.year, today.month, 1))
        self.assertEqual(label, f"{today.year}-{today.month:02d}")

    def test_bad_month_number_falls_back(self):
        today = date.today()
        start, _, _ = self.pb._period_bounds("2026-13")
        self.assertEqual(start, date(today.year, today.month, 1))


# UserA 2026-08-06 給的範例表欄序 —— Excel 要一模一樣、一欄不多。
_ASHLEY_COLUMNS = ["客戶", "供應商", "LOT", "數量", "ETD", "ETA (CAT LAI)",
                   "ETA (FU CHUN)", "庫存編號"]


def _shipment_row(lot="317-2026", **over):
    row = {"lot": lot, "supplier": "TEBO", "customer": "Jalas",
           "qty": "14 PACKAGES", "etd": date(2026, 7, 19),
           "eta_catlai": date(2026, 8, 26), "eta_fuchun": date(2026, 8, 31),
           "attachments": f"LOT {lot} (TEBO).xlsx", "receipt_no": ""}
    row.update(over)
    return row


class ExportTableLayoutTests(unittest.TestCase):
    """出口明細的排版：Excel 照 UserA 的範例表，狀態欄只留在信件內文。"""

    def setUp(self):
        from agent_core import purchasing_brief
        self.pb = purchasing_brief

    def _render(self, rows, period="2026"):
        captured: dict = {}

        def fake_excel(label, columns, table):
            captured.update(label=label, columns=list(columns),
                            table=[list(r) for r in table])
            return "/x/exports/purchasing/out.xlsx"

        with mock.patch.object(self.pb, "_lake_shipment_rows",
                               return_value=(rows, "")), \
                mock.patch.object(self.pb, "_erp_item_codes", return_value={}), \
                mock.patch.object(self.pb, "_write_export_excel",
                                  side_effect=fake_excel):
            text = self.pb.vn_export_shipments(period, to_excel=True)
        return text, captured

    def test_excel_columns_and_order_match_the_sample_sheet(self):
        _, captured = self._render([_shipment_row()])
        self.assertEqual(captured["columns"], _ASHLEY_COLUMNS)
        self.assertEqual(captured["table"][0], [
            "Jalas", "TEBO", "317-2026", "14 PACKAGES",
            "07/19", "08/26", "08/31", "見附件 LOT 317-2026 (TEBO).xlsx",
        ])

    def test_status_column_stays_out_of_the_excel(self):
        """狀態是本系統推算的、不在 UserA 的表裡；但 UserM 的晨報要讀內文那欄。"""
        text, captured = self._render([_shipment_row()])
        self.assertNotIn("狀態", captured["columns"])
        self.assertIn("狀態", text)

    def test_year_period_labels_file_and_keeps_attachment_marker(self):
        text, captured = self._render([_shipment_row()])
        self.assertEqual(captured["label"], "2026")
        self.assertIn("📦 2026 出口越南福群 1 批", text)
        self.assertIn("[[MAIL_FILE:/x/exports/purchasing/out.xlsx]]", text)

    def test_long_year_table_trims_body_but_never_the_excel(self):
        """整年上百批全貼進信裡會把未讀重點洗掉 —— 信裡留最近的，附件留全部。"""
        rows = [_shipment_row(f"{i:03d}-2026", etd=date(2026, 1, 1) + timedelta(days=i))
                for i in range(1, 41)]
        text, captured = self._render(rows)
        self.assertEqual(len(captured["table"]), 40)
        self.assertIn("較早的 15 批", text)
        self.assertIn("040-2026", text)          # 尾端（ETD 最近）留著
        self.assertNotIn("001-2026", text)       # 最早那批只在 Excel 裡

    def test_cross_year_dates_carry_their_year(self):
        """在途批的 ETD 可能落在前一年 —— 只寫 12/28 會被看成今年。"""
        _, captured = self._render([_shipment_row(etd=date(2025, 12, 28))])
        self.assertEqual(captured["table"][0][4], "2025/12/28")

    def test_erp_item_code_wins_over_packing_list_hint(self):
        """到貨後有收貨單就填真的庫存編號（範例表的 PU441 那列）。"""
        with mock.patch.object(self.pb, "_lake_shipment_rows",
                               return_value=([_shipment_row(receipt_no="LJF12345678")], "")), \
                mock.patch.object(self.pb, "_erp_item_codes",
                                  return_value={"LJF12345678": "PU441"}), \
                mock.patch.object(self.pb, "_write_export_excel", return_value=""):
            text = self.pb.vn_export_shipments("2026", to_excel=False)
        self.assertIn("PU441", text)
        self.assertNotIn("見附件", text)


class OutboundDetectionTests(unittest.TestCase):
    def setUp(self):
        from agent_core import purchasing_brief
        self.pb = purchasing_brief

    def test_sent_label_wins_over_missing_from_header(self):
        """本人回自己那條 thread 時 metadata 的 From 可能是空的 —— 純比網域會把
        『已回』誤判成『待辦』。"""
        msg = {"labelIds": ["SENT", "INBOX"], "payload": {"headers": []}}
        self.assertTrue(self.pb._is_outbound(msg, "company.example"))

    def test_same_domain_fallback(self):
        msg = {"labelIds": [], "payload": {"headers": [
            {"name": "From", "value": "UserA <twpurchase2@company.example>"}]}}
        self.assertTrue(self.pb._is_outbound(msg, "company.example"))

    def test_external_sender_is_inbound(self):
        msg = {"labelIds": ["INBOX"], "payload": {"headers": [
            {"name": "From", "value": "kelly <contact-k@supplier-b.example>"}]}}
        self.assertFalse(self.pb._is_outbound(msg, "company.example"))


class AutomatedMailFilterTests(unittest.TestCase):
    def setUp(self):
        from agent_core import purchasing_brief
        self.pb = purchasing_brief

    def test_noreply_senders_flagged(self):
        for sender in ("noreply@customer-d.example",
                       "<noreply-iam@customer-d.example>",
                       "Mailer-Daemon <mailer-daemon@googlemail.com>"):
            self.assertTrue(self.pb._AUTOMATED_SENDER_RE.search(sender), sender)

    def test_real_supplier_contact_not_flagged(self):
        for sender in ('kelly <contact-k@supplier-b.example>',
                       '"Fwd Tpe / contact.one" <contact.one@forwarder-a.example>'):
            self.assertIsNone(self.pb._AUTOMATED_SENDER_RE.search(sender), sender)

    def test_out_of_office_subject_flagged(self):
        self.assertTrue(self.pb._AUTOMATED_SUBJECT_RE.search("Out of Office"))
        self.assertTrue(self.pb._AUTOMATED_SUBJECT_RE.search("自動回覆：休假中"))


class MailboxAllowlistTests(unittest.TestCase):
    """排程任務的 LLM 只能查公司信箱清單裡的信箱。"""

    def setUp(self):
        from agent_core import purchasing_brief
        self.pb = purchasing_brief

    def test_unknown_mailbox_is_refused(self):
        with mock.patch.object(self.pb, "_company_mailboxes",
                               return_value={"twpurchase2@company.example": "/k.json"}):
            out = self.pb.unread_digest("attacker@example.com")
        self.assertIn("不是已設定的公司信箱", out)

    def test_refusal_does_not_raise_for_todo_tracker(self):
        with mock.patch.object(self.pb, "_company_mailboxes", return_value={}):
            out = self.pb.todo_tracker("anyone@example.com")
        self.assertTrue(out.startswith("⚠️"))


class MailAttachmentMarkerTests(unittest.TestCase):
    """[[MAIL_FILE:]] 標記 —— 只認小紅自己的匯出區，其餘一律丟棄。"""

    def setUp(self):
        from agent_core import daemon_dispatcher
        self.dd = daemon_dispatcher
        self.tmp = os.path.realpath(
            os.path.join(_REPO_ROOT, "var", "data", "exports"))
        os.makedirs(self.tmp, exist_ok=True)
        self.good = os.path.join(self.tmp, "_pb_marker_test.xlsx")
        with open(self.good, "w", encoding="utf-8") as handle:
            handle.write("x")

    def tearDown(self):
        try:
            os.remove(self.good)
        except OSError:
            pass

    def _extract(self, text):
        with mock.patch.object(self.dd, "_attachment_allow_root",
                               return_value=self.tmp):
            return self.dd.extract_mail_attachments(text)

    def test_marker_stripped_and_path_accepted(self):
        body, paths = self._extract(f"報表內容\n[[MAIL_FILE:{self.good}]]")
        self.assertEqual(paths, [self.good])
        self.assertNotIn("MAIL_FILE", body)
        self.assertIn("報表內容", body)

    def test_path_outside_exports_is_dropped(self):
        body, paths = self._extract(
            "看這個\n[[MAIL_FILE:/etc/passwd]]")
        self.assertEqual(paths, [])
        self.assertNotIn("MAIL_FILE", body)

    def test_traversal_out_of_exports_is_dropped(self):
        _, paths = self._extract(
            f"[[MAIL_FILE:{os.path.join(self.tmp, '..', '..', '..', 'agent.py')}]]")
        self.assertEqual(paths, [])

    def test_missing_file_is_dropped(self):
        _, paths = self._extract(
            f"[[MAIL_FILE:{os.path.join(self.tmp, 'nope.xlsx')}]]")
        self.assertEqual(paths, [])

    def test_duplicate_markers_collapse(self):
        _, paths = self._extract(
            f"[[MAIL_FILE:{self.good}]]\n[[MAIL_FILE:{self.good}]]")
        self.assertEqual(paths, [self.good])

    def test_text_without_marker_is_untouched(self):
        body, paths = self.dd.extract_mail_attachments("純文字報表")
        self.assertEqual((body, paths), ("純文字報表", []))


class NotifyAttachmentDeliveryTests(unittest.TestCase):
    """有附件時走 send_gmail_as 的 attachments 參數；沒有 notify_emails 的通道
    至少要把檔名寫進正文，不能讓產好的 Excel 靜靜消失。"""

    def setUp(self):
        from agent_core import daemon_dispatcher
        self.dd = daemon_dispatcher

    def test_attachment_passed_to_send_gmail_as(self):
        sent = []

        def fake_send(actor, to, subject, body, **kwargs):
            sent.append((to, subject, body, kwargs))
            return "✅"

        with mock.patch.object(self.dd, "extract_mail_attachments",
                               return_value=("正文", ["/x/exports/a.xlsx"])):
            self.dd.notify_dispatcher_result(
                {"name": "t", "notify_emails": ["a@company.example"]}, "原始",
                notify=lambda **kw: None, send_gmail_as=fake_send)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][3].get("attachments"), "/x/exports/a.xlsx")
        self.assertIn("a.xlsx", sent[0][2])

    def test_telegram_path_mentions_attachment_in_body(self):
        pushed = []
        with mock.patch.object(self.dd, "extract_mail_attachments",
                               return_value=("正文", ["/x/exports/a.xlsx"])):
            self.dd.notify_dispatcher_result(
                {"name": "t", "notify_channel": "telegram"}, "原始",
                notify=lambda **kw: None,
                telegram_push_agent=lambda color, body: pushed.append(body) or "✅")
        self.assertEqual(len(pushed), 1)
        self.assertIn("a.xlsx", pushed[0])


class UserABriefScopeTests(unittest.TestCase):
    """UserA 2026-08-06 縮小後的範圍：只有未讀信＋2026 出口文件那兩段。

    這幾條是「別好心加回來」的守門 —— 待辦追蹤/付款那兩段被拿掉是她本人的要求，
    不是漏寫；下午那封信的存在理由（待辦複查）也隨之消失。
    """

    def setUp(self):
        import scripts.register_purchasing_brief_tasks as reg
        self.reg = reg
        self.tasks = {t["name"]: t for t in reg.build_tasks()}

    def test_ashley_morning_has_only_the_two_topics(self):
        prompt = self.tasks["ashley_daily_brief_am"]["prompt"]
        self.assertIn("mailbox_unread_digest", prompt)
        self.assertIn('vn_export_shipment_table("2026")', prompt)
        self.assertNotIn("mailbox_todo_tracker", prompt)
        self.assertNotIn("open_payment_requests", prompt)

    def test_ashley_afternoon_task_is_retired(self):
        self.assertNotIn("ashley_todo_pm", self.tasks)
        self.assertIn("ashley_todo_pm", self.reg._RETIRED_TASKS)

    def test_amanda_keeps_all_seven_topics(self):
        prompt = self.tasks["amanda_daily_brief_am"]["prompt"]
        for tool in ("mailbox_unread_digest", "mailbox_todo_tracker",
                     "vn_export_shipment_table", "open_payment_requests",
                     "vn_supplier_delivery_progress", "recent_purchase_orders"):
            self.assertIn(tool, prompt)
        self.assertIn("amanda_todo_pm", self.tasks)

    def test_amanda_export_is_full_year(self):
        """2026-08-10 大王指示：UserM 也改整年，跟 UserA 同口徑。

        原本兩人都是「本月」，#364 只改了 UserA 那支。
        """
        prompt = self.tasks["amanda_daily_brief_am"]["prompt"]
        self.assertIn('vn_export_shipment_table("2026")', prompt)
        self.assertNotIn("vn_export_shipment_table()", prompt)

    def test_amanda_both_export_calls_share_one_period(self):
        """TW 出貨進度與出口明細必須用**同一個參數**。

        兩段參數不同 → 產生兩份檔名不同的 Excel；dispatcher 的
        extract_mail_attachments 以路徑去重，不同路徑＝兩個附件一起寄給 UserM。
        參數相同時是同一個檔案路徑，自然只夾一份。
        """
        import re
        prompt = self.tasks["amanda_daily_brief_am"]["prompt"]
        periods = set(re.findall(r'vn_export_shipment_table\(([^)]*)\)', prompt))
        self.assertEqual(len(periods), 1, f"出口參數不一致：{periods}")

    def test_register_disables_retired_task_in_place(self):
        """從 build_tasks() 拿掉不等於停用 —— 檔案裡那筆會繼續每天 15:00 寄。"""
        import contextlib
        import io

        from agent_core import scheduler

        data = {"tasks": [
            {"name": "ashley_todo_pm", "enabled": True, "run_count": 12},
            {"name": "amanda_todo_pm", "enabled": True},
        ]}
        # register() 是 `from agent_core.scheduler import update_daemon_tasks`
        # （函式內、呼叫時才取屬性），所以 patch 來源模組就攔得到。
        with mock.patch.object(scheduler, "update_daemon_tasks",
                               side_effect=lambda mutate: mutate(data)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.reg.register()

        by_name = {t["name"]: t for t in data["tasks"]}
        self.assertFalse(by_name["ashley_todo_pm"]["enabled"])
        self.assertEqual(by_name["ashley_todo_pm"]["run_count"], 12)  # runtime 狀態不洗掉
        self.assertTrue(by_name["amanda_todo_pm"]["enabled"])
        self.assertIn("ashley_daily_brief_am", by_name)               # 新任務照樣寫入


if __name__ == "__main__":
    unittest.main()
