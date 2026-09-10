"""客戶貨款到帳通知（agent_core/payment_notice.py）的規則與解析。

三組守門：

  1. ``classify_notice`` 的**方向**判斷 —— 主旨全部取自 2026-08-12 對
     owner@ / twsales@ 近 365 天各 200 封候選信的實測（見模組 docstring）。
     命中的那些是「錢進來了」；不命中那些是反向的東西（我方催款／申請付款／
     付錢給供應商／自己的轉寄副本），錯放進來就會叫會計去對一筆不存在的帳。
  2. 三家銀行通知的**逐欄解析**。版面照實際信件抄，金額/戶名換成範例值——
     parser 認的是版面不是數字。
  3. **只講一次**：同一封信第二輪不再出現（狀態檔去重）。

⚠️ 測試跑的是 unittest（不是 pytest），隔離一律寫在 setUp/tearDown。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── 實測樣本：真的是「客戶付了錢」的通知 ────────────────────────────
_POSITIVES = [
    ("fx-desk@bank.example", "第一銀行 國外匯入匯款通知", "fcb_foreign_inward"),
    ("sms-adm@mail.bank.example",
     "第一銀行 國內匯入匯款通知 [郵件編號:20260812A2NqJDz2d9C5XENjSVOoPh]",
     "fcb_domestic_inward"),
    ('"國泰世華銀行" <service@pxbillrc01.cathaybk.com.tw>',
     "外匯匯入匯款通知 Forex inward remittance notice", "cub_forex_inward"),
    ("ContactJ Chan <finance-contact@customer-a.example>", "Payment on 11 Aug 2026 - Jai Jye",
     "supremo_payment_on"),
    # 轉寄前綴不影響判斷（Supremo 那封常被回覆成 RE:）。
    ("ContactJ Chan <finance-contact@customer-a.example>", "RE: Payment on 21 July 2026 - Jai Jye",
     "supremo_payment_on"),
    # Decathlon 原信寄到 UserS@，他再轉給大王/UserL/UserJ。
    ("Chen ping chun <gm@company.example>",
     "Fwd: Decathlon - Notice of transfer - Avis de virement",
     "decathlon_notice_of_transfer"),
    ("tms-customer-d@finance-portal.example",
     "Decathlon - Notice of transfer - Avis de virement",
     "decathlon_notice_of_transfer"),
    # 客戶明講已付款的斷言句型。
    ('"mh.seo" <mh.seo@daesung.net>',
     "RE: LOT 230-2025 -- US$18249.49, the payment has been completed.",
     "phrase_payment_done"),
]

# ── 實測樣本：**不是**客戶付款通知，放進來就是誤報 ──────────────────
_NEGATIVES = [
    # 匯出＝我方付錢出去，跟匯入只差一個字。
    ("sms-adm@mail.bank.example",
     "第一銀行 國內匯出匯款通知 [郵件編號:20251206JFHdsOBcSmQ66zp9qz5hQw]"),
    ('"【第一銀行 第e金網】" <ebank@bank.example>',
     "【第一銀行 第e金網】 外幣付款成功受款人通知"),
    ('"【第一銀行 第e金網】" <ebank@bank.example>',
     "【第一銀行 第e金網】 待辦事項通知-外幣單筆付款(含結購售)"),
    # 銀行對帳單／代繳扣帳（錢出去）。
    ("sms-adm@mail.bank.example", "第一銀行 115 年 07 月綜合業務對帳單"),
    ("sms-adm@mail.bank.example",
     "第一銀行 媒體劃撥轉帳代繳扣帳結果通知 [郵件編號:20260805H5zNrxijzC05yzIhHSbfA5]"),
    # 我方在催客戶付款。
    ("ContactW Cheung <shipping-contact@customer-a.example>",
     "RE: 26AW OUTSTAING PAYMENT LIST  18.07.2026 pm // 20.07.2026"),
    ("ContactC Le <contact.one@customer-c.example>",
     "RE: outstanding payment CI-202511010 (US$870) DN 20251230 (2281.29)"),
    ('"mh.seo" <mh.seo@daesung.net>', "RE: pending payment det"),
    # 我方向客戶申請付款 / 談付款排程。
    ("ContactC Le <contact.one@customer-c.example>",
     "RE: Jalas-CNT3 payment application US$64,600.40"),
    ("Zec ContactE <contact.one@customer-b.example>",
     "AW:  payment schedule for this shipment of PO 51865+51886"),
    # 我方付供應商／貨代（採購與出納在對帳）。
    ("UserA <twpurchase2@company.example>",
     "萬華新貨款(LOT 215-2026) , For Decathlon, 福群付款 , 付款日期 : 2026/8/10"),
    ('"出納小姐" <cashier@company.example>',
     "Re: RE-SENT: 貨代 A PLUS 艾圃貨款 , LOT 232-2025 (VETEX) 的運輸費用"),
    ("vsales11@vn.sanfang.com", "FUCHUN's PAYMENT ON MAY-2026.xls"),
    # 大王自己的轉寄副本：原信已經被規則抓過，這封再抓一次就是重複通知。
    ("owner chen <owner@company.example>", "Fwd: 第一銀行 國外匯入匯款通知"),
    # 自家網域講「已經付款」多半是我方付出去，泛用句型刻意不吃自家人。
    ("owner chen <owner@company.example>", "Pax 已經付款"),
    ('"張苑蘭" <accounting-vn@company.example>', "匯款水單"),
    # 主旨帶 advice 但跟付款無關。
    ("sales2005 <sales2005@wanhuanms.com>",
     "pre-order advice  J0M25120108  PU467 (required date early of March 2026)"),
    ('"Šídová Daniela" <sidova@tebo.cz>', "DISPATCH ADVICE JA251209-03 09.12.2025"),
    ("Hener ContactV <contact.two@customer-b.example>",
     "Automatische Antwort: shipping advice for PO 51865+51886"),
]


class ClassifyNoticeTests(unittest.TestCase):
    def setUp(self):
        from agent_core import payment_notice
        self.pn = payment_notice

    def test_real_payment_notices_are_matched(self):
        for sender, subject, expected_key in _POSITIVES:
            with self.subTest(subject=subject):
                rule = self.pn.classify_notice(sender, subject)
                self.assertIsNotNone(rule, f"漏抓：{subject}")
                self.assertEqual(rule["key"], expected_key)

    def test_reverse_direction_mail_is_not_matched(self):
        for sender, subject in _NEGATIVES:
            with self.subTest(subject=subject):
                rule = self.pn.classify_notice(sender, subject)
                self.assertIsNone(
                    rule, f"誤報成付款通知：{subject}（命中 {rule and rule['key']}）")

    def test_bank_rule_requires_both_sender_and_subject(self):
        # 主旨對、寄件人不對（有人轉述銀行主旨）→ 不算銀行通知。
        self.assertIsNone(
            self.pn.classify_notice("someone@example.com", "第一銀行 國外匯入匯款通知"))
        # 寄件人對、主旨不對 → 也不算。
        self.assertIsNone(
            self.pn.classify_notice("fx-desk@bank.example", "系統維護公告"))


# ── 銀行通知版面（照實際信件抄，金額/戶名換成範例值）────────────────
_FCB_FOREIGN_BODY = """國外匯入匯款通知
客戶名稱：佳桀有限公司 系統通知時間：2026/08/11 上午 11:35:06
統一編號：279***72
國外匯入匯款通知
帳號 159****6090
匯入款編號 S6EJ010927
銀行通知日期 2026/08/11
匯款生效日期 2026/08/12
匯款幣別 USD
匯款金額 12,345.67
受款人名稱 JAI JYE CORPORATION
匯款人名稱 EXAMPLE CUSTOMER CO LTD
匯款行名稱 HSBCHKHHXXX
付款明細 SETTLED YOUR INV NO. L200726-5 ANDL
130726-4
說明
匯款生效日期：「匯款行」指定之解款日，未達生效日期時，本行僅先通知。匯款幣別為新台幣(TWD)之匯入款時…
實際入帳金額須扣除手續費，本行匯入匯款手續費為匯款金額乘以萬分之5…
"""

_FCB_DOMESTIC_BODY = """第一銀行 國內匯入匯款通知
客戶名稱：佳Ｏ有限公司
系統通知時間：2026/8/12 上午 11:07:26
統一編號：279***72
國內匯入匯款通知
匯款日期
匯款序號
匯款時間
匯出銀行
匯款人戶名
匯款金額
收款銀行
收款帳號
摘要
2026/08/12 000034 10:46:30 臺銀營業部 財團法人中小企業信用保證基金 1,735.00 一銀吉林 159****1210 信保手續費退費
說明
本郵件為系統自動產生請勿回信！
"""

_CUB_FOREX_BODY = """【國泰世華銀行】
外匯匯入匯款通知
親愛的客戶 您好：
通知日期 Notice Date：2026/05/06
通知書編號 Notice No：6AFFHRI01448121
匯款日期
Date 2026/05/06
幣別金額
Currency and Amount USD 40,000.00
收款帳號
Payee Account 121XXXX22261
收款人名稱
Payee Name JAI JYE CORPORATION
原始匯款行
Original Remitting Bank FCBKTWTPXXX
匯款生效日期
Value Date 2026/05/07
匯款人名稱
Remitter's Name EXAMPLE BUYER GMBH
匯款行參考編號
Reference Number of Remitting Bank T6EJ006897
附言
Remark
"""


class BankBodyParserTests(unittest.TestCase):
    def setUp(self):
        from agent_core import payment_notice
        self.pn = payment_notice

    def test_fcb_foreign_fields(self):
        f = self.pn._parse_fcb_foreign(_FCB_FOREIGN_BODY)
        self.assertEqual(f["remitter"], "EXAMPLE CUSTOMER CO LTD")
        self.assertEqual(f["currency"], "USD")
        self.assertEqual(f["amount"], "12,345.67")
        self.assertEqual(f["payee"], "JAI JYE CORPORATION")
        self.assertEqual(f["ref"], "S6EJ010927")
        # 表格排在「說明」段落前面 → 抓到的是真值，不是說明文裡那句。
        self.assertEqual(f["value_date"], "2026/08/12")
        # 付款明細跨行續寫：第二張發票號在下一行，只取一行就會被切掉，而那正是
        # 會計拿去銷帳的東西（實測 2026-08-11 那封就長這樣）。
        self.assertEqual(f["remark"], "SETTLED YOUR INV NO. L200726-5 ANDL 130726-4")
        self.assertEqual(f["ref_label"], "匯入款編號")

    def test_multiline_field_stops_at_next_section(self):
        # 續行不可以吃進「說明」那段公版文字。
        f = self.pn._parse_fcb_foreign(_FCB_FOREIGN_BODY)
        self.assertNotIn("匯款生效日期：「匯款行」", f["remark"])
        self.assertNotIn("手續費", f["remark"])

    def test_fcb_domestic_row(self):
        f = self.pn._parse_fcb_domestic(_FCB_DOMESTIC_BODY)
        # 匯出銀行與戶名都含中文、長度不一，靠金額當錨點切開。
        self.assertEqual(f["remitter"], "財團法人中小企業信用保證基金")
        self.assertEqual(f["amount"], "1,735.00")
        self.assertEqual(f["currency"], "TWD")
        self.assertEqual(f["value_date"], "2026/08/12")
        # 摘要要原樣帶出——這條線大多是退費不是貨款，會計靠它一眼分辨。
        self.assertEqual(f["remark"], "信保手續費退費")

    def test_cub_forex_splits_currency_and_amount(self):
        f = self.pn._parse_cub_forex(_CUB_FOREX_BODY)
        self.assertEqual(f["currency"], "USD")
        self.assertEqual(f["amount"], "40,000.00")
        self.assertEqual(f["remitter"], "EXAMPLE BUYER GMBH")
        self.assertEqual(f["value_date"], "2026/05/07")
        self.assertEqual(f["ref"], "T6EJ006897")

    def test_unparsable_body_does_not_raise(self):
        # 銀行改版面時要退化成「欄位抓不到」而不是整輪任務炸掉。
        for parser in (self.pn._parse_fcb_foreign, self.pn._parse_fcb_domestic,
                       self.pn._parse_cub_forex):
            self.assertEqual(parser("完全不同的內容"), {})

    def test_html_body_is_flattened_into_labelled_lines(self):
        html = ('<table><tr><td>匯款人名稱</td><td>EXAMPLE CUSTOMER CO LTD</td></tr>'
                '<tr><td>匯款金額</td><td>1,234.00</td></tr></table>')
        text = self.pn._html_to_text(html)
        self.assertIn("匯款人名稱 EXAMPLE CUSTOMER CO LTD", text)
        self.assertIn("匯款金額 1,234.00", text)


class _FakeGmail:
    """最小 Gmail users() 替身：list 回固定 id、get 回固定信件。"""

    def __init__(self, messages: list[dict]):
        self._messages = {m["id"]: m for m in messages}

    def messages(self):
        return self

    def list(self, **kwargs):
        ids = [{"id": mid} for mid in self._messages]
        return mock.Mock(execute=mock.Mock(return_value={"messages": ids}))

    def get(self, *, userId, id, format):  # noqa: A002 —— 對齊 Google client 簽名
        return mock.Mock(execute=mock.Mock(return_value=self._messages[id]))


def _fake_message(mid: str, sender: str, subject: str, body: str,
                  message_id: str = "") -> dict:
    import base64
    headers = [{"name": "From", "value": sender},
               {"name": "Subject", "value": subject}]
    if message_id:
        headers.append({"name": "Message-ID", "value": message_id})
    return {
        "id": mid,
        "internalDate": "1786000000000",
        "snippet": subject,
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
        },
    }


class ScanPaymentNoticesTests(unittest.TestCase):
    """整支流程：命中 → 排版 → 記住 → 第二輪安靜。"""

    def setUp(self):
        from agent_core import payment_notice
        self.pn = payment_notice
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_seen_path = payment_notice._SEEN_PATH
        payment_notice._SEEN_PATH = os.path.join(self._tmp.name, "seen.json")
        # 只掃一個信箱，測試不必準備兩份假信箱。
        self._orig_mailboxes = payment_notice.PAYMENT_MAILBOXES
        payment_notice.PAYMENT_MAILBOXES = ("owner@company.example",)

    def tearDown(self):
        self.pn._SEEN_PATH = self._orig_seen_path
        self.pn.PAYMENT_MAILBOXES = self._orig_mailboxes
        self._tmp.cleanup()

    def _patch_gmail(self, messages):
        return mock.patch.object(
            self.pn, "_gmail_users", lambda *a, **k: _FakeGmail(messages))

    def test_bank_notice_is_rendered_with_fields(self):
        msgs = [_fake_message("m1", "fx-desk@bank.example",
                              "第一銀行 國外匯入匯款通知", _FCB_FOREIGN_BODY)]
        with self._patch_gmail(msgs):
            out = self.pn.scan_payment_notices(days=3)
        self.assertIn("EXAMPLE CUSTOMER CO LTD", out)
        self.assertIn("USD 12,345.67", out)
        self.assertIn("S6EJ010927", out)
        # 口徑：不替會計斷言這筆一定是客戶貨款。
        self.assertIn("匯入款不等於客戶貨款", out)

    def test_second_round_is_silent_for_the_same_message(self):
        msgs = [_fake_message("m1", "fx-desk@bank.example",
                              "第一銀行 國外匯入匯款通知", _FCB_FOREIGN_BODY)]
        with self._patch_gmail(msgs):
            first = self.pn.scan_payment_notices(days=3)
            second = self.pn.scan_payment_notices(days=3)
        self.assertIn("EXAMPLE CUSTOMER CO LTD", first)
        # dispatcher 認得這五個字 → 整輪不推播、不寄信。
        self.assertEqual(second, "(無新發現)")

    def test_persist_false_does_not_consume_the_notice(self):
        msgs = [_fake_message("m1", "fx-desk@bank.example",
                              "第一銀行 國外匯入匯款通知", _FCB_FOREIGN_BODY)]
        with self._patch_gmail(msgs):
            self.pn.scan_payment_notices(days=3, persist=False)
            again = self.pn.scan_payment_notices(days=3, persist=False)
        self.assertIn("EXAMPLE CUSTOMER CO LTD", again)
        self.assertFalse(os.path.exists(self.pn._SEEN_PATH))

    def test_non_payment_mail_is_ignored(self):
        msgs = [_fake_message("m1", "UserA <twpurchase2@company.example>",
                              "萬華新貨款(LOT 215-2026) , 福群付款", "內文")]
        with self._patch_gmail(msgs):
            self.assertEqual(self.pn.scan_payment_notices(days=3), "(無新發現)")

    def test_examined_but_unmatched_mail_is_not_refetched(self):
        """沒命中的信也要記下來，否則每輪都把同一批催款信全部重 get 一次。

        粗篩每輪撈回十幾封（實測 owner@ 近 3 天 14 封），回溯視窗又是 3 天 ——
        不記＝同一封信連續三天每輪各 get 一次，全是白花的 API。
        """
        msgs = [_fake_message("m1", "UserA <twpurchase2@company.example>",
                              "萬華新貨款(LOT 215-2026) , 福群付款", "內文")]
        fake = _FakeGmail(msgs)
        get_calls = []
        real_get = fake.get

        def counting_get(**kwargs):
            get_calls.append(kwargs["id"])
            return real_get(**kwargs)

        fake.get = counting_get
        with mock.patch.object(self.pn, "_gmail_users", lambda *a, **k: fake):
            self.pn.scan_payment_notices(days=3)
            self.pn.scan_payment_notices(days=3)
        self.assertEqual(get_calls, ["m1"], "第二輪不該再 get 同一封")

    def test_mailbox_error_is_surfaced_not_swallowed(self):
        # 兩個信箱都讀不到時的安靜失敗＝這條通知線死了沒人知道，必須出聲。
        with mock.patch.object(self.pn, "_gmail_users",
                               side_effect=ValueError("網域委派未開通")):
            out = self.pn.scan_payment_notices(days=3)
        self.assertIn("⚠️", out)
        self.assertIn("網域委派未開通", out)
        self.assertNotEqual(out, "(無新發現)")

    def test_fee_refund_goes_to_the_other_inflow_section(self):
        # 匯入款不等於客戶貨款：信保退費仍然列出（會計要對全部入帳），但獨立
        # 一區，不會被讀成「客戶付了 1,735」。
        msgs = [_fake_message("m1", "sms-adm@mail.bank.example",
                              "第一銀行 國內匯入匯款通知 [郵件編號:x]",
                              _FCB_DOMESTIC_BODY)]
        with self._patch_gmail(msgs):
            out = self.pn.scan_payment_notices(days=3)
        self.assertIn("其他匯入款", out)
        self.assertIn("信保手續費退費", out)
        self.assertNotIn("──── 銀行匯入款通知 ────", out)

    def test_customer_remittance_stays_in_the_main_section(self):
        msgs = [_fake_message("m1", "fx-desk@bank.example",
                              "第一銀行 國外匯入匯款通知", _FCB_FOREIGN_BODY)]
        with self._patch_gmail(msgs):
            out = self.pn.scan_payment_notices(days=3)
        self.assertIn("──── 銀行匯入款通知 ────", out)
        self.assertNotIn("其他匯入款", out)

    def test_same_mail_in_two_mailboxes_is_reported_once(self):
        # 客戶寄給 UserAng、副本給大王 → 兩個信箱各一個 Gmail id，但 RFC
        # Message-ID 相同。會計不該收到兩則一樣的通知。
        self.pn.PAYMENT_MAILBOXES = ("owner@company.example", "twsales@company.example")
        per_mailbox = {
            "owner@company.example": [_fake_message(
                "id-in-owner", "ContactJ Chan <finance-contact@customer-a.example>",
                "Payment on 11 Aug 2026 - Jai Jye", "內文",
                message_id="<abc@customer-a.example>")],
            "twsales@company.example": [_fake_message(
                "id-in-twsales", "ContactJ Chan <finance-contact@customer-a.example>",
                "Payment on 11 Aug 2026 - Jai Jye", "內文",
                message_id="<abc@customer-a.example>")],
        }
        with mock.patch.object(self.pn, "_gmail_users",
                               lambda mb, *a, **k: _FakeGmail(per_mailbox[mb])):
            out = self.pn.scan_payment_notices(days=3)
        self.assertIn("客戶貨款通知 1 則", out)
        self.assertEqual(out.count("📨"), 1)

    def test_seen_state_prunes_old_entries(self):
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(days=self.pn._SEEN_TTL_D + 5)).isoformat()
        with open(self.pn._SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "seen": {
                "owner@company.example:ancient": {"at": old, "subject": "舊的"},
            }}, f)
        msgs = [_fake_message("m1", "fx-desk@bank.example",
                              "第一銀行 國外匯入匯款通知", _FCB_FOREIGN_BODY)]
        with self._patch_gmail(msgs):
            self.pn.scan_payment_notices(days=3)
        with open(self.pn._SEEN_PATH, "r", encoding="utf-8") as f:
            seen = json.load(f)["seen"]
        self.assertNotIn("owner@company.example:ancient", seen)
        self.assertIn("owner@company.example:m1", seen)


class BackgroundReachabilityTests(unittest.TestCase):
    """排程要跑得到這顆工具：background_safe 旗標 + 進得了工具目錄。

    #350 的教訓：prompt/設定點名一顆不在背景工具集的工具**不會報錯**，只是
    靜默不生效。這裡兩邊都釘死。
    """

    def test_tools_are_background_safe(self):
        from agent_core.payment_notice import (
            payment_notice_alert, scan_payment_notices,
        )
        self.assertTrue(getattr(scan_payment_notices, "background_safe", False))
        self.assertTrue(getattr(payment_notice_alert, "background_safe", False))

    def test_alert_tool_is_in_the_builtin_catalog(self):
        from agent_core.tool_registry_catalog import BASE_BUILTIN_TOOLS
        names = {getattr(t, "__name__", "") for t in BASE_BUILTIN_TOOLS}
        self.assertIn("payment_notice_alert", names)
        self.assertIn("scan_payment_notices", names)

    def test_alert_tool_passes_dispatcher_safe_tools_filter(self):
        from agent_core.daemon_dispatcher import safe_tools
        from agent_core.payment_notice import payment_notice_alert
        reachable = {getattr(t, "__name__", "")
                     for t in safe_tools([payment_notice_alert])}
        self.assertIn("payment_notice_alert", reachable)


if __name__ == "__main__":
    unittest.main()
