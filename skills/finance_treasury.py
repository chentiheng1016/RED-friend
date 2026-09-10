"""財務長工具（Phase 2）—— 福群資金面：現金水位/現金流/待付壓力/資金展望。

薄殼：邏輯全在 agent_core/finance_treasury.py（可單元測試）。全部純讀 ERP 本地
鏡像、tier SAFE、``background_safe=True``（cash_outlook 要給每週一晨推的
deterministic_tool 排程用）。

⚠️ 財務全貌屬敏感資料：這批工具**永不進** dept_tool_scope._HOME_TOOLS
（員工自由對話白名單）—— tests/test_finance_treasury.py 有守門測試釘死。
"""
from agent_core import finance_treasury as _ft


def cash_position() -> str:
    """資金水位快照：逐銀行帳戶現金餘額＋應收/應付/借款＋淨部位＋美金參考。

    「現在有多少錢」「銀行還剩多少」用這個。金額是 GL 本位幣 VND（百萬），
    傳票累加口徑、已排除作廢傳票、含開放月草稿（取最即時）。照表唸數字，
    不要自己加減。免 +確認。

    Returns:
        資金水位快照。
    """
    return _ft.cash_position()


def cash_flow_monthly(months: int = 6) -> str:
    """逐月現金流實績表：銀行＋現金的流入/流出/淨額/期末現金（帳戶互轉已軋）。

    「每個月現金進出多少」「上月燒了多少錢」用這個。免 +確認。

    Args:
        months: 近幾個月（預設 6，上限 24）。
    Returns:
        逐月現金流表（code block 等寬對齊）。
    """
    return _ft.cash_flow_monthly(months=months)


def payment_pressure(top: int = 8) -> str:
    """待付壓力：全部門未付請示單（幣別彙總＋大額清單）＋ERP 付款排程逾期/未來 60 天。

    「有哪些錢要付」「付款壓力多大」用這個。未付判準是確定性欄位
    （PAYMENT_ID 空／PAY_NO 空），舊殭屍項已用日期窗濾掉。現金蓋不住未付
    請示時會標 🚨 缺口。免 +確認。

    Args:
        top: 大額請示單最多列幾張（預設 8，上限 30）。
    Returns:
        待付壓力報告。
    """
    return _ft.payment_pressure(top=top)


def cash_outlook(weeks: int = 8) -> str:
    """資金展望：現金水位 → 逐週計畫付款 → 缺口判定（每週一早自動推的就是這份）。

    「接下來資金夠不夠」「幾週內要準備多少錢」用這個。逐週數字來自 ERP 對帳
    付款排程的計畫付款日（確定性），「月均流入/流出」是歷史算術、輸出有標明
    不是預測 —— 轉述時保持這個誠實度。應收沒有逐筆到期日（AR 模組福群沒在用），
    收款時程要問業務/會計，工具不會編。免 +確認。

    Args:
        weeks: 展望幾週（預設 8，上限 13）。
    Returns:
        資金展望報告（含 🚨/⚠️/✅ 缺口判定）。
    """
    return _ft.cash_outlook(weeks=weeks)


SKILL_TOOLS = [cash_position, cash_flow_monthly, payment_pressure, cash_outlook]

# 全部純讀 → 背景 dispatcher 的排程任務可用（safe_tools 的第二條路徑）。
for _fn in SKILL_TOOLS:
    _fn.background_safe = True
