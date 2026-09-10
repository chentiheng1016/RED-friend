"""台灣公共 API connector（T5 第一批）— 全部公開資料、不需 API key。

對象：台灣中小企業老闆（鞋廠、貿易商、一般商業）日常會用到的外部查詢：

  1. 電子發票中獎號碼：查最近 3 期中獎號（對帳時順便看自己手上的發票中獎沒）
  2. 台銀牌告匯率：做外銷的必備（USD/EUR/JPY/CNY/HKD/AUD...）
  3. 統一編號查證：商業司公開 API，查供應商 / 客戶的統編 + 公司名
  4. 台灣行政區對照：縣市行政代碼 / 郵遞區號查詢

之後可擴：海關稅則、勞健保級距、財政部商業統計 等。

都是純 GET、無 auth、失敗 fallback to error 訊息。
"""
import json
import re
from datetime import datetime, timedelta

import requests


# ────────────────────────────────────────────────────────────────────
# 1. 電子發票中獎號碼
# ────────────────────────────────────────────────────────────────────
def einvoice_winning_numbers(period: str = "") -> str:
    """查電子發票中獎號碼（最近 3 期公開資料）。

    ⚠️ 需要財政部 appID（免費申請）：https://www.einvoice.nat.gov.tw/
        申請後存進 macOS 鑰匙圈：
        keyring.set_password('xiaohong-agent', 'einvoice-appid', 'YOUR_APPID')

    Args:
        period: YYY-MM 格式（民國年），例如 "113-04"（2024 年 3-4 月期）。
                空字串 = 回傳最近 3 期全部。
    Returns:
        每期的頭獎/二獎/三獎/四獎/五獎/六獎/特別獎/特獎 號碼清單。
    """
    # 從鑰匙圈取 appID
    try:
        import keyring
        app_id = keyring.get_password("xiaohong-agent", "einvoice-appid") or ""
    except Exception:
        app_id = ""
    if not app_id:
        return ("❌ 需先申請財政部 appID 並存鑰匙圈：\n"
                "  1. 去 https://www.einvoice.nat.gov.tw/ 註冊並建立 API 金鑰\n"
                "  2. 用 python keyring 存：\n"
                "     python3 -c \"import keyring; keyring.set_password("
                "'xiaohong-agent', 'einvoice-appid', 'YOUR_APPID')\"\n"
                "  3. 重試小紅即可")

    url = "https://api.einvoice.nat.gov.tw/PB2CAPIVAN/invapp/InvApp"
    params = {
        "version": "0.2",
        "action": "QryWinningList",
        "UUID": "agent-xiaohong",
        "appID": app_id,
    }
    try:
        r = requests.post(url, data=params, timeout=15)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        return f"❌ 查中獎號碼失敗：{type(e).__name__}: {e}"

    if j.get("code") not in (200, "200", None):
        return f"❌ API 拒絕：{j.get('msg', json.dumps(j, ensure_ascii=False)[:200])}"

    items = j.get("items") or []
    if not items:
        return f"⚠️ API 沒回資料（raw: {json.dumps(j, ensure_ascii=False)[:200]}）"

    # 過濾特定期別
    if period:
        items = [x for x in items if x.get("invoYm") == period]
        if not items:
            return f"🔍 沒有 {period} 期的資料。最近可查的：" + \
                   ", ".join(x.get("invoYm", "?") for x in (j.get("items") or [])[:5])

    lines = [f"🧾 電子發票中獎號碼（共 {len(items)} 期）："]
    for it in items:
        ym = it.get("invoYm", "?")
        lines.append(f"\n📅 {ym} 期")
        mapping = [
            ("特別獎 (1千萬)", "superPrizeNo"),
            ("特獎 (200萬)", "spcPrizeNo"),
            ("頭獎 (20萬)", "firstPrizeNo1"),
            ("頭獎 2", "firstPrizeNo2"),
            ("頭獎 3", "firstPrizeNo3"),
            ("增開六獎 (200)", "sixthPrizeNo1"),
            ("增開六獎 2", "sixthPrizeNo2"),
        ]
        for label, key in mapping:
            val = it.get(key)
            if val:
                lines.append(f"  {label:20s}  {val}")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 2. 台銀牌告匯率
# ────────────────────────────────────────────────────────────────────
_BOT_RATES_URL = "https://rate.bot.com.tw/xrt/flcsv/0/day"


def bot_exchange_rates(currency: str = "", direction: str = "sell") -> str:
    """查台灣銀行今日牌告匯率。

    Args:
        currency: 幣別代碼（大寫），例如 "USD", "EUR", "JPY", "CNY", "HKD", "AUD"。
                  空字串 = 回傳所有幣別一次。
        direction: "sell"（銀行賣出，你匯出去用的）或 "buy"（銀行買入，你收款換台幣用的）。
                   預設 "sell"。
    Returns:
        格式化的匯率表。
    """
    try:
        r = requests.get(_BOT_RATES_URL, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return f"❌ 抓台銀匯率失敗：{type(e).__name__}: {e}"

    # CSV 格式：幣別, 匯率類別, 即期買入, 即期賣出, 現金買入, 現金賣出 ...
    lines_raw = r.text.strip().split("\n")
    # 第一行是 header；每個幣別各一行
    result_rows = []
    for line in lines_raw:
        cols = line.split(",")
        if len(cols) < 14:
            continue
        code = cols[0].strip().upper()
        # 根據 direction 挑欄位
        # 欄位: 0=幣別, 3=現金買入, 12=現金賣出, 即期用第 2 (買入) / 12 (賣出)
        try:
            cash_buy = cols[2] if cols[2] != "0" else cols[3]
            cash_sell = cols[12] if cols[12] != "0" else cols[13]
        except (IndexError, ValueError):
            continue
        result_rows.append({"code": code, "cash_buy": cash_buy, "cash_sell": cash_sell})

    if not result_rows:
        return "❌ 抓不到匯率資料"

    if currency:
        target = currency.upper()
        matches = [r for r in result_rows if r["code"] == target]
        if not matches:
            codes = ", ".join(r["code"] for r in result_rows[:20])
            return f"🔍 沒有 {target} 匯率。可用幣別: {codes}"
        r0 = matches[0]
        return (f"💱 台銀 {target}/TWD 即時牌告（{direction}）\n"
                f"  買入 (你拿 {target} 換台幣): {r0['cash_buy']}\n"
                f"  賣出 (你用台幣買 {target}): {r0['cash_sell']}")

    # 無指定 → 列常用幣別
    lines = ["💱 台銀牌告匯率（即期；你拿外幣換台幣用買入，台幣買外幣用賣出）"]
    common = {"USD", "EUR", "JPY", "CNY", "HKD", "AUD", "GBP", "CHF", "KRW", "VND", "THB"}
    sorted_rows = sorted(result_rows, key=lambda x: (x["code"] not in common, x["code"]))
    for r0 in sorted_rows[:15]:
        lines.append(f"  {r0['code']:5s}  買入 {r0['cash_buy']:>10s}  賣出 {r0['cash_sell']:>10s}")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 3. 統一編號查證（經濟部商業司公開 API）
# ────────────────────────────────────────────────────────────────────
def tw_company_lookup(tax_id: str) -> str:
    """用統一編號查公司資訊（g0v ronny 的開放資料鏡像）。

    Args:
        tax_id: 8 位數統一編號（純數字）。
    Returns:
        公司名稱、地址、代表人、資本額、行業別、設立日期。
    """
    tax_id = (tax_id or "").strip()
    if not re.fullmatch(r"\d{8}", tax_id):
        return f"❌ 統編格式錯誤（應該是 8 位數字）：{tax_id!r}"

    # g0v ronny 的商業登記 API 鏡像（比經濟部自家 API 穩）
    url = f"https://company.g0v.ronny.tw/api/show/{tax_id}"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        return f"❌ 查統編失敗：{type(e).__name__}: {e}"

    data = j.get("data") or {}
    if not data:
        return (f"🔍 查無統編 {tax_id}（可能是個人 / 尚未登記 / 已解散）。\n"
                "若確定有此統編，可試經濟部官網：https://findbiz.nat.gov.tw/")

    lines = [f"🏢 統編 {tax_id}"]
    # ronny API 的欄位名
    fields = [
        ("公司名稱", "公司名稱"),
        ("英文名稱", "英文公司名稱"),
        ("狀態", "公司狀況"),
        ("資本總額", "資本總額(元)"),
        ("實收資本", "實收資本額(元)"),
        ("代表人", "代表人姓名"),
        ("地址", "公司所在地"),
        ("核准設立日期", "核准設立日期"),
        ("最近變更", "最後核准變更日期"),
    ]
    for label, key in fields:
        val = data.get(key)
        if val:
            if isinstance(val, dict):
                val = " / ".join(f"{k}: {v}" for k, v in val.items())
            lines.append(f"  {label}: {val}")

    # 所營事業也列一些（最常用的前 3 個）
    biz = data.get("所營事業資料") or []
    if biz:
        lines.append("  所營事業（前 3）:")
        for item in biz[:3]:
            if isinstance(item, list) and len(item) >= 2:
                lines.append(f"    • {item[1][:70]}")

    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 4. 行政區 / 郵遞區號查詢
# ────────────────────────────────────────────────────────────────────
_ZIP_API = "https://ezship.com.tw/api/getPostcode.php"  # 備援；主要查自己的 cache

# 簡易縣市碼對照（最常用）
_CITY_CODES = {
    "台北市": "10", "臺北市": "10", "新北市": "65",
    "桃園市": "68", "臺中市": "66", "台中市": "66",
    "臺南市": "67", "台南市": "67", "高雄市": "64",
    "基隆市": "10017", "新竹市": "10018", "嘉義市": "10020",
    "新竹縣": "10004", "苗栗縣": "10005", "彰化縣": "10007",
    "南投縣": "10008", "雲林縣": "10009", "嘉義縣": "10010",
    "屏東縣": "10013", "宜蘭縣": "10002", "花蓮縣": "10015",
    "臺東縣": "10014", "台東縣": "10014", "澎湖縣": "10016",
    "金門縣": "09020", "連江縣": "09007",
}


def tw_city_code(city_name: str) -> str:
    """查台灣縣市的行政代碼（物流、政府表單會用到）。

    Args:
        city_name: 縣市名，中文即可，例如「台北市」、「新北市」、「高雄市」。
    """
    name = (city_name or "").strip()
    code = _CITY_CODES.get(name)
    if code:
        return f"🏛 {name} → 行政代碼 {code}"
    # 部分匹配
    matches = [(k, v) for k, v in _CITY_CODES.items() if name in k or k in name]
    if matches:
        return "\n".join(f"🏛 {k} → {v}" for k, v in matches)
    return f"🔍 查無「{name}」。支援的縣市：{', '.join(list(_CITY_CODES.keys())[:8])}..."


SKILL_TOOLS = [
    einvoice_winning_numbers,
    bot_exchange_rates,
    tw_company_lookup,
    tw_city_code,
]

# 這些都是純讀 API，沒副作用 → 允許背景 dispatcher 用
einvoice_winning_numbers.background_safe = True
bot_exchange_rates.background_safe = True
tw_company_lookup.background_safe = True
tw_city_code.background_safe = True
