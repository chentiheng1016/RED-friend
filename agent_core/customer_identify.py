"""客戶(品牌)辨識 helper——樣品單/產品照 → 這是哪個客戶的。

背景：樣品單常只有款號沒印客戶名（如 Richter 的 5010-4291 規格書），前台 LLM
沒依據就會臆測品牌。這裡提供三個確定性訊號源：
  ① 樣品單內文/檔名掃已知品牌字樣（KNOWN_BRANDS，詞彙來自 email lake brands 欄）
  ② 款號查內外 email lake parquet（誰寄過含這個款號的信 → 多數決）
  ③ 產品照庫資料夾前綴（R-=Richter、L-=Lurchi、B-=BRTK、K-=Kamik）
"""
import json
import os
import re

from agent_core.logging_and_paths import EMAIL_LAKE_DIR, INTERNAL_LAKE_DIR, logger

# token(小寫) → 正規顯示名。詞彙來自 email lake brands 欄位 + 產品照庫資料夾名。
KNOWN_BRANDS = {
    "richter": "Richter",
    "lurchi": "Lurchi",
    "kamik": "Kamik",
    "kamuk": "Kamik",  # 照片庫資料夾拼法
    "brtk": "BRTK",
    "pax": "PAX",
    "supremo": "Supremo",
    "blaklader": "Blaklader",
    "decathlon": "Decathlon",
    "deca": "Decathlon",
    "jalas": "JALAS",
    "ejendals": "Ejendals",
    "reima": "Reima",
    "newwave": "New Wave",
}

# 產品照庫資料夾前綴 → 客戶（R-JAJLCG(Husky)=Richter、L-JEK154=Lurchi…）
_FOLDER_PREFIX_CUSTOMER = {"R": "Richter", "L": "Lurchi", "B": "BRTK", "K": "Kamik"}

# 測試會 patch 這兩個常數；避免 import 較重的 email_lake 模組
_INTERNAL_PARQUET = os.path.join(INTERNAL_LAKE_DIR, "emails.parquet")
_EXTERNAL_PARQUET = os.path.join(EMAIL_LAKE_DIR, "emails_master.parquet")


def customer_of_folder(folder: str) -> str:
    """產品照庫資料夾名 → 客戶（'R-JAJLCG ( Husky )女靴' → 'Richter'）；未知前綴回 ''。"""
    m = re.match(r"^([A-Za-z])-", (folder or "").strip())
    return _FOLDER_PREFIX_CUSTOMER.get(m.group(1).upper(), "") if m else ""


# 弱品牌訊號：材質/技術品牌，別的客戶單也會提到（material_aliases.json 就把
# richtex 當通用防水膜詞）。只在完全掃不到正牌品牌字樣時才當訊號、不參與
# 多候選——2026-09-08 review：Lurchi 單提到 RICHTEX 內裡曾把客戶識別成
# 「Richter / Lurchi」，色彙 scope 比對直接落空。richtex 本身仍是 Richter
# 樣品單（logo 是圖）唯一的文字層訊號（PSS 2 案），不能整個拿掉。
_WEAK_BRAND_HINTS = {
    "richtex": "Richter",
}


def brands_in_text(text: str) -> list:
    """掃文字裡的已知品牌字樣，回正規名列表（去重、依 KNOWN_BRANDS 順序）。"""
    low = (text or "").lower()
    found = []
    for token, canon in KNOWN_BRANDS.items():
        if canon not in found and re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", low):
            found.append(canon)
    if not found:
        for token, canon in _WEAK_BRAND_HINTS.items():
            if canon not in found and re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", low):
                found.append(canon)
    return found


def _row_brands(row) -> list:
    raw = row.get("brands", "")
    if raw is None:
        return []
    try:
        items = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("[") else [raw]
    except Exception:  # noqa: BLE001
        items = [raw]
    out = []
    for b in items:
        b = str(b).strip()
        if not b:
            continue
        canon = KNOWN_BRANDS.get(b.lower(), b)
        if canon not in out:
            out.append(canon)
    return out


def identify_customer_by_style(style_numbers, max_evidence: int = 2):
    """款號查內外 email lake → (客戶, 佐證行列表)。多數決；查無回 ('', [])。"""
    styles = [str(s).strip() for s in (style_numbers or []) if s and str(s).strip()]
    if not styles:
        return "", []
    try:
        import pandas as pd
    except Exception:  # noqa: BLE001
        return "", []

    votes = {}
    evidence = []  # (brand, "date「subject」")
    for path in (_INTERNAL_PARQUET, _EXTERNAL_PARQUET):
        if not os.path.exists(path):
            continue
        try:
            df = pd.read_parquet(path)
        except Exception as e:  # noqa: BLE001
            logger.warning("customer_identify 讀 %s 失敗: %s", path, e)
            continue
        cols = [c for c in ("subject", "summary", "entities_json", "attachments") if c in df.columns]
        if not cols:
            continue
        # fillna 先於 astype：pandas 3 的 astype(str) 保留 NaN（不轉 "nan" 字串），
        # 缺欄列逐列 " ".join 會 TypeError（#323 同型雷）。
        hay = df[cols].fillna("").astype(str).agg(" ".join, axis=1)
        for st in styles:
            for _, row in df[hay.str.contains(re.escape(st), case=False, na=False)].iterrows():
                brs = _row_brands(row)
                for b in brs:
                    votes[b] = votes.get(b, 0) + 1
                if brs:
                    subj = str(row.get("subject", ""))[:60]
                    evidence.append((brs[0], f"{str(row.get('date', ''))[:10]}「{subj}」"))

    if not votes:
        return "", []
    best = max(votes, key=votes.get)
    ev = []
    for b, line in evidence:
        if b == best and line not in ev:
            ev.append(line)
        if len(ev) >= max_evidence:
            break
    return best, ev


def identify_customer(sheet_text: str, style_numbers=None) -> dict:
    """樣品單客戶辨識總入口：①內文/檔名找品牌字樣 ②款號查 email lake。

    回 {"customer": str, "source": str, "evidence": [str, ...]}；customer=="" 表示無法辨識。
    """
    found = brands_in_text(sheet_text)
    if len(found) == 1:
        return {"customer": found[0], "source": "樣品單內文", "evidence": []}
    cust, ev = identify_customer_by_style(style_numbers)
    if cust:
        return {"customer": cust, "source": "email 紀錄", "evidence": ev}
    if found:  # 內文出現多個品牌、email 又查無 → 至少把候選攤出來
        return {"customer": " / ".join(found), "source": "樣品單內文（多個候選）", "evidence": []}
    return {"customer": "", "source": "", "evidence": []}
