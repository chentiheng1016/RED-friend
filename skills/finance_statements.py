"""財務長工具（Phase 1）—— 福群（越南廠）月損益/趨勢/成本結構。

薄殼：邏輯全在 agent_core/finance_statements.py（可單元測試），這裡只掛工具
簽名與給 LLM 看的用法說明。全部純讀 ERP 本地鏡像、tier SAFE、
``background_safe=True``（income_statement_autopush 要給背景 dispatcher 的
deterministic_tool 排程用）。

⚠️ 財務全貌屬敏感資料：這批工具**永不進** dept_tool_scope._HOME_TOOLS
（員工自由對話白名單）—— tests/test_finance_statements.py 有守門測試釘死。
只有大王（RED 全工具通道）與排程推播看得到。
"""
from agent_core import finance_statements as _fin


def income_statement(period: str = "") -> str:
    """某月的月損益表（營收/成本/毛利/費用/營業利益/稅後淨利＋上月比、去年同月比）。

    資料源是 ERP 總帳傳票的確定性加總（已排除月結轉），**照表唸數字，不要自己
    加減或改寫**。目前活帳套是福群（越南廠）、幣別 VND、單位百萬；報表尾有美金
    參考換算與鏡像時間。「尚未關帳」的月份數字未定稿，工具會自帶警語——轉述時
    警語要一起講。免 +確認。

    Args:
        period: 期間，YYYY-MM 或 YYYYMM（如 2026-04）。留空＝最新已關帳月。
    Returns:
        月損益報表；期間不存在或未入帳時回原因。
    """
    return _fin.income_statement(str(period or ""))


def profit_trend(months: int = 6) -> str:
    """近 N 個已關帳月的損益趨勢表（營收/毛利/毛利率/營業利益/稅後淨利）。

    「最近幾個月賺錢嗎」「毛利率走勢」用這個。只列已關帳月。免 +確認。

    Args:
        months: 近幾個月（預設 6，上限 24）。
    Returns:
        逐月趨勢表（code block 等寬對齊）。
    """
    return _fin.profit_trend(months=months)


def expense_breakdown(period: str = "", top: int = 12) -> str:
    """某月的成本/費用結構：按科目群組列金額、占比、與上月變化。

    「錢花去哪」「哪個費用暴增」用這個。income_statement 只給大類合計，
    這裡下鑽到科目群組（如 6201 薪資）。免 +確認。

    Args:
        period: 期間 YYYY-MM，留空＝最新已關帳月。
        top: 每區最多列幾個群組（預設 12，上限 40）。
    Returns:
        成本、費用兩區的結構表。
    """
    return _fin.expense_breakdown(str(period or ""), top=top)


def income_statement_autopush() -> str:
    """（排程專用）偵測「新關帳月」：有新月結就回完整月損益，沒有回 (無新發現)。

    給每日 deterministic_tool 排程用 —— dispatcher 看到 (無新發現) 不會推播，
    所以效果等於「會計關帳後自動推當月損益」。對話中沒事不要呼叫：它會記錄
    已推播狀態，白耗一次月報額度。免 +確認。

    Returns:
        新關帳月的完整月損益，或 "(無新發現)"。
    """
    return _fin.income_statement_autopush()


SKILL_TOOLS = [
    income_statement, profit_trend, expense_breakdown, income_statement_autopush,
]

# 全部純讀 → 背景 dispatcher 的排程任務可用（safe_tools 的第二條路徑）。
for _fn in SKILL_TOOLS:
    _fn.background_safe = True
