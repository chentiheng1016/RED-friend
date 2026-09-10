"""美金對台幣即時匯率工具 —— 每日 09:00／14:00 排程推播的工具面。

薄殼：邏輯全在 agent_core/fx_rates.py（可單元測試），這裡只掛工具簽名與給 LLM
看的用法說明。純讀、tier SAFE、``background_safe=True``（背景 dispatcher 要用）。

排程推播走 dispatcher 的 ``deterministic_tool`` 路徑 —— 直接呼叫這顆工具、把回傳
原樣送出，完全不經 LLM 改寫，數字不可能被轉述錯（〈員工零幻覺〉）。
"""
from agent_core import fx_rates as _fx


def usd_twd_rate_brief() -> str:
    """查美金對台幣**即時**匯率，回傳一份可直接發出去的完整訊息。

    國際外匯市場即時中價（多來源交叉核對），不是銀行牌告買入/賣出 —— 回傳內容
    已經把這個口徑、報價時間、與上次的漲跌一起寫進去了。抓不到來源時回 ❌ 開頭的
    失敗說明，不會猜一個數字。免 +確認。

    Returns:
        排版好的匯率訊息（純文字，Telegram 可直接顯示）。
    """
    return _fx.usd_twd_brief()


SKILL_TOOLS = [usd_twd_rate_brief]

# 純讀 → 背景 dispatcher 的排程任務可用（safe_tools 的第二條路徑）。
for _fn in SKILL_TOOLS:
    _fn.background_safe = True
