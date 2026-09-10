"""財務長工具（Phase 3）—— 降成本：材料漲價偵測/買貴清單/費用異常。

薄殼：邏輯全在 agent_core/finance_cost.py（可單元測試）。全部純讀 ERP 本地
鏡像、tier SAFE、``background_safe=True``（cost_review_autopush 給背景
dispatcher 的排程用）。

⚠️ 財務全貌屬敏感資料：這批工具**永不進** dept_tool_scope._HOME_TOOLS
（員工自由對話白名單）—— tests/test_finance_cost.py 有守門測試釘死。
"""
from agent_core import finance_cost as _fc


def material_price_watch(months: int = 3, top: int = 10) -> str:
    """材料漲價/降價偵測：近 N 月成交價 vs 前 12 月中位數，按金額影響排序。

    「最近什麼料變貴」「材料成本壓力在哪」用這個。只比同料同供應商同單位；
    價差 >10 倍的列「疑似單位/登打異常」——那是要人工看的資料問題，轉述時
    不要當成真漲價講。金額影響只換算 VND/USD，其他幣別照原幣列。免 +確認。

    Args:
        months: 觀察窗（預設 3，上限 12 個月）。
        top: 漲價榜最多列幾項（預設 10，上限 30）。
    Returns:
        漲價榜/降價榜/異常區三段。
    """
    return _fc.material_price_watch(months=months, top=top)


def overpriced_purchases(months: int = 3, top: int = 10) -> str:
    """買貴清單：近 N 月 PO 價超過標準價 5%+ 的料，按多付金額排序。

    「哪些採購買貴了」「跟供應商談判先抓誰」用這個。輸出帶標準價日期 ——
    標準價可能過時，這是談判線索不是指控，轉述時保持這個口吻。免 +確認。

    Args:
        months: 觀察窗（預設 3，上限 12 個月）。
        top: 最多列幾個料號（預設 10，上限 30）。
    Returns:
        買貴清單＋多付金額合計。
    """
    return _fc.overpriced_purchases(months=months, top=top)


def expense_anomaly(period: str = "") -> str:
    """費用/成本科目異常：某關帳月 vs 前 12 個關帳月中位數，超門檻才列。

    「這個月有什麼費用不對勁」用這個。門檻寫在輸出第一行（倍數＋絕對額），
    異常＝算術偏離不是結論，大額建議再用 expense_breakdown 下鑽。免 +確認。

    Args:
        period: 關帳月 YYYYMM 或 YYYY-MM，留空＝最新關帳月。
    Returns:
        異常科目清單（含新出現/歸零科目）。
    """
    return _fc.expense_anomaly(str(period or ""))


def cost_review_autopush() -> str:
    """（排程專用）偵測「新關帳月」：有新月結就回降成本月報（費用異常＋買貴摘要）。

    給每日 deterministic_tool 排程用，沒新關帳月回 "(無新發現)" dispatcher
    不推播。對話中沒事不要呼叫：會記錄已推播狀態。免 +確認。

    Returns:
        新關帳月的降成本月報，或 "(無新發現)"。
    """
    return _fc.cost_review_autopush()


SKILL_TOOLS = [
    material_price_watch, overpriced_purchases, expense_anomaly,
    cost_review_autopush,
]

# 全部純讀 → 背景 dispatcher 的排程任務可用（safe_tools 的第二條路徑）。
for _fn in SKILL_TOOLS:
    _fn.background_safe = True
