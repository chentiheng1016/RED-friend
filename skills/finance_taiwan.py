"""財務長工具（合併損益第一步）—— 台灣金流台帳 + 台越合併視圖。

薄殼：邏輯全在 agent_core/finance_taiwan.py（可單元測試）。sync 讀 Gmail 全文
（唯讀、不寄信不改標籤）＋寫本地台帳快取；彙總/合併視圖純讀台帳與 ERP 鏡像。

⚠️ 財務全貌屬敏感資料：這批工具**永不進** dept_tool_scope._HOME_TOOLS
（員工自由對話白名單）—— tests/test_finance_taiwan.py 有守門測試釘死。
"""
from agent_core import finance_taiwan as _ftw


def sync_taiwan_ledger(days_back: int = 120) -> str:
    """從 Gmail 同步台灣金流台帳：一銀匯入款/代繳批次/出口託收、採購水單、中華電信、富邦轉帳。

    增量同步（已入帳的信直接跳過），第一次建帳建議 days_back=365。只讀信，
    不寄信不改標籤。解析不出金額的信照筆數入帳，不猜數字。免 +確認。

    Args:
        days_back: 掃近幾天（預設 120，上限 730）。
    Returns:
        各串流掃到/新增/有金額的統計。
    """
    return _ftw.sync_taiwan_ledger(days_back=days_back)


def taiwan_cash_summary(months: int = 6) -> str:
    """台灣端逐月現金收支（email 證據彙總）：客戶匯入款、勞健保勞退、採購付款、電信費…。

    「台灣這邊每月收多少付多少」用這個。**現金收付制、只計 email 有通知的**
    —— 輸出尾端的缺口清單（薪資/房租/水電/稅）要跟著轉述，那些不在數字裡。
    關係企業內部金流（福群↔台灣、一銀→富邦薪資鏈）另列不進合計。免 +確認。

    Args:
        months: 近幾個月（預設 6，上限 24）。
    Returns:
        逐月分類收支＋覆蓋缺口說明。
    """
    return _ftw.taiwan_cash_summary(months=months)


def consolidated_pnl_view(period: str = "") -> str:
    """台越合併視圖：福群 ERP 關帳損益 × 台灣 email 台帳同月收支，並排＋美金參考。

    「集團整體賺不賺」用這個。**並排不是合併報表**：福群是權責發生制關帳數、
    台灣是現金收付估算，口徑不同不能直接相加 —— 輸出裡的口徑警語必須跟著講。
    關係企業內部金流已標示（合併應對消）。免 +確認。

    Args:
        period: 期間 YYYY-MM，留空＝福群最新關帳月。
    Returns:
        台越並排損益視圖。
    """
    return _ftw.consolidated_pnl_view(str(period or ""))


def taiwan_ledger_autosync() -> str:
    """（排程專用）每日安靜同步台帳：成功回 (無新發現)，抓取失敗 raise。

    給背景 dispatcher 的 deterministic_tool 用，對話中不要呼叫 —— 要看同步
    統計用 sync_taiwan_ledger。免 +確認。

    Returns:
        "(無新發現)"。
    """
    return _ftw.taiwan_ledger_autosync()


def taiwan_tax_from_drive(k: int = 10) -> str:
    """台灣稅單清單：從 Drive 會計資料夾（RAG 索引）撈營業稅 401/營所稅繳款書＋應納稅額。

    「台灣繳了多少稅」用這個。金額是 OCR 抽取、輸出帶檔名 —— 轉述時要說
    「以繳款書原件為準」，抽不出金額的照檔名列、不要猜。免 +確認。

    Args:
        k: 最多列幾份（預設 10，上限 20）。
    Returns:
        繳款書清單（檔名/上傳日/應納稅額）。
    """
    return _ftw.taiwan_tax_from_drive(k=k)


def freight_bills_from_drive(k: int = 10) -> str:
    """貨代/海運費歷史單：從 Drive 會計資料夾（RAG 索引）撈沛華等運費單據＋金額。

    「以前付沛華多少運費」用這個。email 只有 2026-08 起的電子發票，更早的
    在 Drive 掃描檔。金額標來源：檔名=確定性、OCR=以原件為準；抽不出照檔名
    列不要猜。免 +確認。

    Args:
        k: 最多列幾份（預設 10，上限 20）。
    Returns:
        單據清單（檔名/上傳日/金額與來源）。
    """
    return _ftw.freight_bills_from_drive(k=k)


SKILL_TOOLS = [sync_taiwan_ledger, taiwan_cash_summary, consolidated_pnl_view,
               taiwan_ledger_autosync, taiwan_tax_from_drive,
               freight_bills_from_drive]

# 全部唯讀（sync 只寫本地快取）→ 背景 dispatcher 排程可用。
for _fn in SKILL_TOOLS:
    _fn.background_safe = True
