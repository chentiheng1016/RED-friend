"""排程任務 prompt 點名的工具，必須真的進得了 dispatcher 的背景工具集。

這是一整類「靜默失效」的守門：`daemon_dispatcher.safe_tools()` 會把不在白名單
的工具整個濾掉，Gemini 那邊**看不到**這支工具，於是它照 prompt 的退路走
（「工具回錯誤就略過此段」「查不到就略過」）——任務照常成功、照常寄信/推播，
只是少了一整段內容。沒有例外、沒有 last_error、日誌一片綠。

實際踩到的（2026-08-04 盤點，5 支）：
  - read_warehouse_stock   → daily_warehouse_report 的「庫存注意」段落，110 輪全空
  - production_alert       → daily_production_alert 的第 1 步（任務主體）
  - production_overdue_bom → 同上第 3 步
  - erp_delivery_risk_alert→ erp_delivery_risk_daily 的第 1 步，45 輪全空
  - kitting_alert          → kitting_alert_daily 的第 1 步，20 輪全空

同類前科：f6f1994f「背景任務 Drive 白名單寫了不存在的工具名」。

兩種進場方式（見 safe_tools docstring）：builtin 走 `_SAFE_TOOL_NAMES`，
skill 走 `background_safe = True`。這支測試不管走哪條，只問「最後進得去嗎」。
"""
from __future__ import annotations

import json
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 抽取/稽核邏輯住在 agent_core/daemon_dispatcher.py（跟 safe_tools 同一個模組，
# 單一真相）。這裡只是消費端；另一個消費端是 agent_core/health.py 的每 30 分鐘
# health_check——測試只在有人跑的時候跑，health_check 補的是沒人跑的空窗。
from agent_core.daemon_dispatcher import (  # noqa: E402
    audit_task_tool_refs,
    extract_tool_refs,
)


def _real_tool_names() -> set[str]:
    from agent_core.tool_registry import tools_list
    return {getattr(t, "__name__", "") for t in tools_list}


def _reachable_tool_names() -> set[str]:
    from agent_core.daemon_dispatcher import safe_tools
    from agent_core.tool_registry import tools_list
    return {getattr(t, "__name__", "") for t in safe_tools(tools_list)}


# 目前線上 daemon_tasks.json 九支任務 prompt 點名的工具（2026-08-04 盤點）。
# daemon_tasks.json 是 gitignore 的 runtime state、CI 上不存在，所以把當時的
# 快照凍在這裡當常駐防線；下面另有一支測試會在檔案存在時（部署機／本機）比對
# 真實 prompt，抓「新任務點名了沒開的工具」這種漂移。
SHIPPED_TASK_TOOL_REFS: dict[str, tuple[str, ...]] = {
    "daily_rag_health": ("rag_sync_health",),
    # daily_production_8am 2026-08-07 改走 deterministic_tool（見下面那份快照）——
    # 同一份資料 Gemini 每輪產出不同表格（實測一個上午四封、其中一封漏掉整家還有
    # 426 雙未完的 RICHTER），改成程式排版。
    "daily_production_alert": (
        "production_alert", "production_overdue_bom", "query_erp_order_materials",
    ),
    "email_pending_tracker": (
        "list_unanswered_company_threads", "cross_reference_order", "query_erp_order",
    ),
    "daily_warehouse_report": ("read_production_progress_sheet", "read_warehouse_stock"),
    "kitting_alert_daily": ("kitting_alert",),
    # UserY 每日兩次倉庫通知（#347）。
    "warehouse_brief_0900": (
        "warehouse_daily_brief", "warehouse_mail_digest", "warehouse_todo_board",
        "read_production_progress_sheet",
    ),
    "warehouse_todo_1500": ("warehouse_mail_digest", "warehouse_todo_board"),
    # UserA / UserM 採購每日簡報（#348）。UserA 2026-08-06 把自己的報表縮成兩個
    # 主題（未讀信＋2026 出口文件），下午那封「待辦複查」隨之退場（enabled=False，
    # 見 scripts/register_purchasing_brief_tasks.py 的 _RETIRED_TASKS）——停用的
    # 任務不會跑，就不列在這份快照裡。
    "ashley_daily_brief_am": ("mailbox_unread_digest", "vn_export_shipment_table"),
    "amanda_daily_brief_am": (
        "mailbox_unread_digest", "mailbox_todo_tracker",
        "open_payment_requests", "vn_export_shipment_table",
        "recent_purchase_orders", "vn_supplier_delivery_progress",
    ),
    "amanda_todo_pm": ("mailbox_unread_digest", "mailbox_todo_tracker"),
    # 週一會議未完成事項總報告（scripts/register_monday_pending_report_task.py）：
    # 每週一 06:00（weekdays=[1]）彙整近 21 天未完成事項寄 owner@／gm@。
    "monday_meeting_pending_report": (
        "list_unanswered_company_threads", "list_unanswered_sent_threads",
        "production_alert", "production_overdue_bom", "kitting_alert",
        "warehouse_todo_board", "query_erp_order", "cross_reference_order",
    ),
}

# 走 `deterministic_tool` 的任務：完全不經 LLM，dispatcher 直接呼叫這顆工具、把
# 回傳原樣當結果。它們的 prompt 只是寫給人看的說明，該檢查的是這個設定值。
SHIPPED_DETERMINISTIC_TASK_TOOLS: dict[str, str] = {
    "usd_twd_rate_am": "usd_twd_rate_brief",
    "usd_twd_rate_pm": "usd_twd_rate_brief",
    # 生管主管（生產管理部）每日簡報，早晚各一封（#361）：迪卡儂形體剩餘材料可做雙數
    # ＋ 針車/射出/包裝 每日產能折線圖。
    "production_capacity_brief_am": "production_capacity_brief",
    "production_capacity_brief_pm": "production_capacity_brief",
    # 交期風險表：工具自己已把已包裝/未完照生管日報念完（含來源欄），再讓 Gemini
    # 轉述一次只會多出「可再問我」這種邀請＋改寫數字的空間。2026-08-06 從
    # SHIPPED_TASK_TOOL_REFS 搬過來（scripts/register_erp_delivery_risk_task.py）。
    "erp_delivery_risk_daily": "erp_delivery_risk_alert",
    # 每日生產數量回報（#368 後續）：2026-08-07 從 SHIPPED_TASK_TOOL_REFS 搬過來
    # （scripts/register_daily_production_report_task.py）。表格由程式排版，客戶
    # 清單與數字每天固定；ERP 交期核對那段也一併確定性化。
    "daily_production_8am": "daily_production_report",
    # 大王本人寄出、對方沒回的每日提醒（scripts/register_sent_reply_task.py）。
    # 主旨/逾期天數是資料，讓 Gemini 轉述只會多出改寫的空間。
    "sent_reply_reminder_am": "sent_reply_reminder",
    # 客戶貨款到帳通知 → 台越會計（scripts/register_payment_notice_task.py）。
    # 金額／匯款人／發票號讓 LLM 轉述一次就有講錯的風險，這條線不讓它碰。
    "payment_notice_watch": "payment_notice_alert",
}


class ShippedTaskToolsReachableTests(unittest.TestCase):
    """凍結快照版——CI 上一定會跑（不依賴 runtime 的 daemon_tasks.json）。"""

    def test_every_shipped_task_tool_is_reachable(self):
        reachable = _reachable_tool_names()
        missing: list[str] = []
        for task, tools in SHIPPED_TASK_TOOL_REFS.items():
            for name in tools:
                if name not in reachable:
                    missing.append(f"{task} → {name}()")
        self.assertEqual(
            missing, [],
            "排程任務 prompt 點名的工具沒進 dispatcher 工具集，會靜默少一段內容。\n"
            "修法：builtin 加進 daemon_dispatcher._SAFE_TOOL_NAMES；\n"
            "      skill 在 skills/*.py 尾巴標 `<tool>.background_safe = True`。\n"
            f"不可達：{missing}",
        )

    def test_every_shipped_deterministic_tool_is_reachable(self):
        """deterministic_tool 也要進得了 safe_tools —— run_deterministic_task 從那裡解析。"""
        reachable = _reachable_tool_names()
        missing = sorted({
            f"{task} → {tool}"
            for task, tool in SHIPPED_DETERMINISTIC_TASK_TOOLS.items()
            if tool not in reachable
        })
        self.assertEqual(missing, [], f"deterministic_tool 不可達，該任務每輪都會失敗：{missing}")

    def test_every_shipped_task_tool_actually_exists(self):
        """點名的工具名要對得上真工具——防「白名單/prompt 寫了不存在的名字」。"""
        real = _real_tool_names()
        ghosts = sorted({
            name for tools in SHIPPED_TASK_TOOL_REFS.values()
            for name in tools if name not in real
        })
        self.assertEqual(ghosts, [], f"prompt 點名了不存在的工具：{ghosts}")


class SafeToolNamesAreLiveTests(unittest.TestCase):
    """白名單本身不能有死名字（上面那個 bug 的鏡像）。

    比不中的字串不會報錯、只是靜默不生效，但看名單的人會以為那個能力有開。
    2026-08-05 清掉 19 個這種名字前，名單裡有 `read_webpage`/`browser_search`/
    `query_calendar`… 讓人以為背景任務能讀網頁、查行事曆（前者其實不能）。
    前科 f6f1994f（Drive 白名單寫了不存在的工具名，背景任務一直讀不到 Drive）。
    """

    def test_no_dead_names_in_whitelist(self):
        from agent_core.daemon_dispatcher import _SAFE_TOOL_NAMES
        dead = sorted(_SAFE_TOOL_NAMES - _real_tool_names())
        self.assertEqual(
            dead, [],
            "_SAFE_TOOL_NAMES 有比不中任何真工具的死名字（靜默不生效、且會誤導）。\n"
            "要嘛改成真工具名，要嘛刪掉；真想記錄「還沒開的能力」請寫成註解，\n"
            f"不要留在集合裡。死名字：{dead}",
        )


class CostReportingReachabilityTests(unittest.TestCase):
    """排程要能做成本回報。

    2026-08-12 之前 **cost 工具一顆都不在背景工具集** —— 想排「每週成本報表」
    這種任務根本做不到，而且會照舊踩靜默失效（prompt 點名 cost_by_tool，
    Gemini 看不到那顆工具就照 prompt 退路走，任務照樣「成功」寄信、只是少一
    整段）。發現於一次要驗證 embedding 係數改動的排程需求。
    """

    def test_cost_summary_tool_is_reachable(self):
        self.assertIn(
            "cost_by_tool", _reachable_tool_names(),
            "cost_by_tool 被移出背景工具集 —— 排程的成本回報會靜默少一整段",
        )

    def test_key_fingerprint_view_stays_out(self):
        """cost_by_key 列的是 API key 指紋，背景任務沒有需要 —— 刻意不開。"""
        self.assertNotIn("cost_by_key", _reachable_tool_names())


class ExtractorSanityTests(unittest.TestCase):
    """守門的守門：抽取器壞掉會讓上面的測試變成空轉真空通過。"""

    def test_extractor_catches_call_form_and_bare_form(self):
        real = {"read_warehouse_stock", "search_gmail", "kitting_alert"}
        prompt = (
            "1) 用 read_warehouse_stock() 查目前庫存；\n"
            "2) 再用 search_gmail 查生管最新『生產日報』郵件；\n"
            "3) 呼叫 kitting_alert（）。\n"
            "（訊息開頭的「現在時間」是你判斷今天的依據。）"
        )
        self.assertEqual(
            extract_tool_refs(prompt, real),
            {"read_warehouse_stock", "search_gmail", "kitting_alert"},
        )

    def test_extractor_ignores_prose_and_flags_ghost_calls(self):
        # 中文散文不該誤命中；但寫成呼叫形式的假名字要被抓出來。
        self.assertEqual(extract_tool_refs("整理成表格回覆，開頭『📧 追蹤』。", set()), set())
        self.assertIn("no_such_tool", extract_tool_refs("呼叫 no_such_tool()。", set()))

    def test_manifest_is_not_empty(self):
        """避免有人清空 manifest 讓測試空轉。

        門檻 10 ＝ 2026-08-07 走 LLM 路徑的任務數。原本 13 支：ashley_todo_pm 退場
        （#364）、erp_delivery_risk_daily（#365）與 daily_production_8am（2026-08-07）
        改走 deterministic_tool 搬去下面那份 manifest —— 兩份加總沒變少。
        任務退場/搬家要連這兩個數字一起調，不要為了讓測試綠而整批刪。
        """
        self.assertGreaterEqual(len(SHIPPED_TASK_TOOL_REFS), 10)
        self.assertTrue(all(v for v in SHIPPED_TASK_TOOL_REFS.values()))
        self.assertGreaterEqual(len(SHIPPED_DETERMINISTIC_TASK_TOOLS), 6)

    def test_task_appears_in_exactly_one_manifest(self):
        """一支任務不可能同時走 LLM 路徑又走 deterministic_tool —— 重複＝有人只搬一半。"""
        both = sorted(set(SHIPPED_TASK_TOOL_REFS) & set(SHIPPED_DETERMINISTIC_TASK_TOOLS))
        self.assertEqual(both, [], f"同時列在兩份 manifest：{both}")


def _live_tasks_file() -> str:
    from agent_core.scheduler import DAEMON_TASKS_FILE
    return DAEMON_TASKS_FILE


class LiveTaskFileDriftTests(unittest.TestCase):
    """真實 daemon_tasks.json 存在時（部署機／本機）才跑——抓 prompt 漂移。

    新增或改寫排程任務時點到沒開的工具，這裡會紅；CI 上檔案不存在則 skip。
    """

    def setUp(self):
        path = _live_tasks_file()
        if not os.path.exists(path):
            self.skipTest(f"no live daemon_tasks.json at {path}（CI/worktree 正常）")
        with open(path, "r", encoding="utf-8") as f:
            self.tasks = json.load(f).get("tasks", [])

    def test_live_tasks_pass_the_shared_auditor(self):
        """線上排程全數過 audit_task_tool_refs（health_check 用的是同一支）。

        跑這支等於預演每 30 分鐘 health_check 會看到什麼：這裡綠，線上就不會
        因為排程漂移而告警；這裡紅，就是真的有任務點到沒開的工具。
        """
        from agent_core.tool_registry import tools_list
        problems = audit_task_tool_refs(self.tasks, tools_list)
        self.assertEqual(
            problems, [],
            "線上排程任務點名的工具有問題：\n  " + "\n  ".join(problems),
        )


class HealthCheckWiringTests(unittest.TestCase):
    """稽核要真的接上 health_check —— 否則守門只存在於「有人跑測試」時。"""

    def test_dispatcher_task_check_is_wired_into_health_check(self):
        import inspect

        from agent_core import health
        self.assertIn(
            "_check_dispatcher_task_tools", inspect.getsource(health.health_check),
            "health_check() 沒呼叫 _check_dispatcher_task_tools —— 每 30 分鐘的守門是斷的",
        )

    def test_health_check_reports_unreachable_tool_as_warning(self):
        """假造一支點名不可達工具的排程 → health_check 要吐 warning。

        warning（🟡）是 task_health_check 會通知的門檻，所以這條等於驗證
        「排程漂移真的推得到大王」。
        """
        from unittest import mock

        from agent_core import health
        fake_tasks = {"tasks": [{
            "name": "fake_task", "enabled": True,
            "prompt": "1) 呼叫 definitely_not_a_real_tool() 取得資料。",
        }]}
        with mock.patch("agent_core.scheduler._load_daemon_tasks",
                        return_value=fake_tasks):
            issues = health._check_dispatcher_task_tools()
        self.assertTrue(issues, "點名不存在的工具卻沒產生 issue")
        self.assertEqual(issues[0]["severity"], "warning")
        self.assertEqual(issues[0]["area"], "dispatcher_tasks")
        self.assertIn("definitely_not_a_real_tool", issues[0]["msg"])

    def test_health_check_quiet_when_tasks_are_fine(self):
        from unittest import mock

        from agent_core import health
        fake_tasks = {"tasks": [{
            "name": "fine_task", "enabled": True,
            "prompt": "1) 呼叫 rag_sync_health() 取得健康摘要。",
        }]}
        with mock.patch("agent_core.scheduler._load_daemon_tasks",
                        return_value=fake_tasks):
            self.assertEqual(health._check_dispatcher_task_tools(), [])

    def test_health_check_survives_a_broken_tasks_file(self):
        """稽核自己爆掉不可以讓整份 health_check 掛掉。"""
        from unittest import mock

        from agent_core import health
        with mock.patch("agent_core.scheduler._load_daemon_tasks",
                        side_effect=ValueError("boom")):
            issues = health._check_dispatcher_task_tools()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "warning")
        self.assertIn("稽核失敗", issues[0]["msg"])


if __name__ == "__main__":
    unittest.main()
