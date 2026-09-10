"""部門識別規則 + 品牌偵測 — 給 internal_emails 用的分類 look-up table.

sender 命中 = primary signal（強）。sender 不命中但 subject 關鍵字命中 =
secondary signal（弱）。sender 同時命中多部門時（例：twpurchase 既是
採購又是船務），用 subject 關鍵字當 tiebreaker；全都沒中就回傳第一個
match 當 primary + 其他當 secondary 存起來。

BRAND_KEYWORDS 是正交維度：同一封信可以 `dept=採購, brand=Decathlon`。
"""
from typing import Tuple, List, Dict


DEPT_RULES: Dict[str, Dict[str, List[str]]] = {
    "老闆": {
        # 大王（Owner）本人寄出的信。所有部門溝通都會經過他，
        # 這個標籤代表「這封是老闆的角度/決策」。
        "senders": ["owner@company.example"],
        "subject_keywords": [],
        "sender_patterns": [],
    },
    "業務": {
        "senders": ["twsales@company.example", "sales@company.example"],
        "subject_keywords": ["業務", "報價", "詢價", "quotation", "quote", "customer", "sales"],
        "sender_patterns": [],
    },
    "樣品室": {
        "senders": ["sampleroom@company.example", "sampledev@company.example", "staff-p@company.example"],
        "subject_keywords": ["樣品", "sample", "SP-"],
        "sender_patterns": [],
    },
    "生產管理": {
        "senders": ["production-mgr@company.example", "gm@company.example", "pm@company.example"],
        "subject_keywords": ["生產進度", "交期"],  # 「出貨」易撞船務/採購，挑掉
        "sender_patterns": [],
    },
    "採購": {
        "senders": ["twpurchase@company.example", "twpurchase2@company.example",
                    "vnpurchase@company.example", "vnpurchase2@company.example"],
        "subject_keywords": ["PO-", "採購", "purchase", "出貨單"],
        "sender_patterns": [],
    },
    "倉庫": {
        "senders": ["warehouse-mgr@company.example", "warehouse@company.example"],
        "subject_keywords": ["出入庫", "庫存"],
        "sender_patterns": [],
    },
    "船務": {
        "senders": ["shipping@company.example", "twpurchase@company.example",
                    "twpurchase2@company.example", "twsales@company.example"],
        "subject_keywords": ["櫃號", "ETD", "ETA", "shipping"],
        "sender_patterns": [],
    },
    "會計": {
        "senders": ["accounting-vn@company.example", "twaccounting@company.example", "cashier@company.example"],
        "subject_keywords": ["invoice", "發票", "付款"],
        # 外部金融 / 電信自動通知也算會計知識（帳單、扣款、匯款通知）
        "sender_patterns": ["firstbank", "fxcore", "cht_ebpp", "cht.com.tw",
                            "mega-bank", "ctbcbank", "cathaybk", "esun.com.tw"],
    },
}

# 已知寄件者身分註記 — 給 LLM prompt 當人物背景，防止只看姓名/語言腦補身分。
# 2026-07-08 事故：ponder 把倉庫主管井戶良枝（warehouse-mgr@，日文名字）當成
# 「日本客戶」發了要求賠償的急件警報。
KNOWN_SENDER_NOTES: Dict[str, str] = {
    "warehouse-mgr@company.example": "井戶良枝 — 越南廠倉庫主管（自家同事；日文名字，別誤判成日本客戶）",
    "production-mgr@company.example": "越南工廠經理 — 生產管理主管（自家同事）",
    "staff-p@company.example": "樣品室主管（自家同事）",
    "accounting-vn@company.example": "越南會計主管（自家同事）",
    "sampledev@company.example": "台灣總公司員工 — 處理客人的樣品單（樣品開發）",
    "twpurchase2@company.example": "UserA — 台灣採購（自家同事；Telegram 黃色 bot 使用者）",
    "factory-vn@company.example": "越南總機 — 收發窗口，由越南廠經理管理（自家同事）",
}


def llm_internal_context() -> str:
    """回傳塞進 LLM prompt 的內部人物背景區塊（繁中多行字串）。

    Gemini 摘要/推理信件時只看得到寄件者顯示名與內文，會用表面線索
    （姓名語言、語氣）猜身分。這裡把「@company.example＝自家人」的硬規則、
    DEPT_RULES 的部門對照、KNOWN_SENDER_NOTES 的個別註記一次給足，
    讓身分判斷有可信錨點、不靠腦補。
    """
    lines = [
        "【內部人物背景（可信事實，權重高於信件內容的表面線索）】",
        "- @company.example 是自家公司 JAIFUNG 的網域（台灣總部＋越南富春廠）。"
        "此網域的寄件者一律是內部同事，不是客戶。",
    ]
    for dept, rules in DEPT_RULES.items():
        senders = rules.get("senders", [])
        if senders:
            lines.append(f"- {dept}：{'、'.join(senders)}")
    for email, note in KNOWN_SENDER_NOTES.items():
        lines.append(f"- {email}：{note}")
    return "\n".join(lines)


# 客戶品牌 — 正交維度，independent of dept
BRAND_KEYWORDS: List[str] = [
    "PAX", "Richter", "Lurchi", "supremo", "blaklader",
    "ejendals", "newwave", "kamik", "reima", "nisshinrubber", "decathlon",
]

# 排除規則 — 不進 lake
EXCLUDE_SENDERS_SUBSTR: List[str] = [
    "hr@", "payroll@", "salary@", "noreply",
    "no-reply", "donotreply", "mailer-daemon",
]
EXCLUDE_SUBJECT_SUBSTR: List[str] = [
    # "對帳單" 移除 — 銀行對帳單算會計知識，讓 DEPT_RULES["會計"]["sender_patterns"] 吸收
    "薪資", "薪水", "勞健保",
]


def _norm(s: str) -> str:
    return (s or "").lower().strip()


def classify_dept(sender: str, subject: str) -> Tuple[str, List[str]]:
    """回傳 (primary_dept, all_depts).

    primary_dept：最可信的單一部門，空字串代表不屬於任何已知部門
    all_depts：所有有匹配的部門（primary 會排第一個）

    匹配優先序：exact sender > sender substring pattern > subject keyword
    tiebreaker：多個 sender 都中 → subject keyword 決定 primary
    """
    sender_low = _norm(sender)
    subject_low = _norm(subject)

    # 抽出 sender 的純 email 地址（去掉 "Name <addr>" 的 Name 部分）
    if "<" in sender_low and ">" in sender_low:
        sender_low = sender_low[sender_low.find("<") + 1:sender_low.find(">")].strip()

    sender_matches: List[str] = []
    keyword_matches: List[str] = []

    for dept, rules in DEPT_RULES.items():
        senders = rules.get("senders", [])
        patterns = rules.get("sender_patterns", [])
        kws = rules.get("subject_keywords", [])
        # exact sender 比對（case-insensitive）
        if any(sender_low == _norm(s) for s in senders):
            sender_matches.append(dept)
            continue
        # sender substring pattern（case-insensitive；用來抓 firstbank / cht_ebpp 這類外部自動通知）
        if any(p.lower() in sender_low for p in patterns):
            sender_matches.append(dept)
            continue
        if any(kw.lower() in subject_low for kw in kws):
            keyword_matches.append(dept)

    all_depts = sender_matches + keyword_matches
    if not all_depts:
        return "", []

    if len(sender_matches) <= 1:
        return all_depts[0], all_depts

    # Sender 同時中多個部門 → 用 subject keyword 當 tiebreaker
    for dept in sender_matches:
        kws = DEPT_RULES[dept].get("subject_keywords", [])
        if any(kw.lower() in subject_low for kw in kws):
            others = [d for d in all_depts if d != dept]
            return dept, [dept] + others

    return sender_matches[0], all_depts


def classify_direction(sender: str) -> str:
    """internal = 自家員工（@company.example）寄出；external = 對方寄進來。"""
    sender_low = _norm(sender)
    if "<" in sender_low and ">" in sender_low:
        sender_low = sender_low[sender_low.find("<") + 1:sender_low.find(">")].strip()
    return "internal" if sender_low.endswith("@company.example") else "external"


def detect_brands(subject: str, body: str = "") -> List[str]:
    """偵測這封信涉及哪些客戶品牌（subject + body 都掃）。"""
    haystack = _norm(subject + "\n" + body)
    return [b for b in BRAND_KEYWORDS if b.lower() in haystack]


def should_exclude(sender: str, subject: str) -> bool:
    """回 True 表示這封信不進 lake（HR / 薪資 / 對帳單 等）。"""
    sender_low = _norm(sender)
    subject_low = _norm(subject)
    if any(x in sender_low for x in EXCLUDE_SENDERS_SUBSTR):
        return True
    if any(x in subject_low for x in EXCLUDE_SUBJECT_SUBSTR):
        return True
    return False
