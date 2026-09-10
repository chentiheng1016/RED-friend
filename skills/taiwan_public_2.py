"""T5 第二批台灣 connector：空氣品質 / 股票 / 氣象警報 / 郵遞區號。

  1. aqi_now(city, station)  — 環保署空氣品質即時（要 api_key）
  2. tw_stock_quote(code)    — 台股即時報價（via yfinance，無 auth）
  3. tw_weather_alert()      — 中央氣象署警特報（要 auth token）
  4. tw_postal_code(addr)    — 地址 → 3 碼郵遞區號（本地 table，無 auth）

政府 API 2024 起都統一要申請 appID/授權碼（免費）。我們把 key 存 macOS
鑰匙圈，key 名分別是：
  - 'moenv-api-key'  ← 環境部 data.moenv.gov.tw
  - 'cwa-api-key'    ← 中央氣象署 opendata.cwa.gov.tw
沒設定的 tool 會清楚回指引，不會打錯 API。
"""
import json
import re
from datetime import datetime

import requests


def _get_secret(key: str) -> str:
    """從 macOS 鑰匙圈取 key，沒 keyring 或沒存都回空。"""
    try:
        import keyring
        return keyring.get_password("xiaohong-agent", key) or ""
    except Exception:
        return ""


def _setup_hint(secret_name: str, url: str) -> str:
    return (
        f"❌ 需要 API key。\n"
        f"  1. 去 {url} 申請（免費）\n"
        f"  2. 存進 macOS 鑰匙圈：\n"
        f"     python3 -c \"import keyring; keyring.set_password("
        f"'xiaohong-agent', '{secret_name}', 'YOUR_KEY')\"\n"
        f"  3. 重試小紅即可"
    )


# ────────────────────────────────────────────────────────────────────
# 1. 空氣品質（環境部）
# ────────────────────────────────────────────────────────────────────
_MOENV_AQI_URL = "https://data.moenv.gov.tw/api/v2/aqx_p_432"


def aqi_now(city: str = "", station: str = "") -> str:
    """查環境部即時空氣品質（AQI / PM2.5 / O3）。

    ⚠️ 需申請 api_key（免費）：https://data.moenv.gov.tw/
        keyring: 'xiaohong-agent' / 'moenv-api-key'

    Args:
        city: 縣市名，例如「臺北市」。空字串 = 全台概況。
        station: 測站名（比 city 更精確），例如「中山」、「前鎮」。
    """
    api_key = _get_secret("moenv-api-key")
    if not api_key:
        return _setup_hint("moenv-api-key", "https://data.moenv.gov.tw/")

    params = {"limit": 200, "api_key": api_key, "sort": "ImportDate desc", "format": "JSON"}
    try:
        r = requests.get(_MOENV_AQI_URL, params=params, timeout=15)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        return f"❌ 查 AQI 失敗：{type(e).__name__}: {e}"

    records = j.get("records") or []
    if not records:
        return f"⚠️ MOENV 沒回資料：{str(j)[:200]}"

    filtered = records
    if station:
        s_lo = station.strip()
        filtered = [r for r in filtered if s_lo in r.get("sitename", "")]
    elif city:
        c_lo = city.strip().replace("台", "臺")
        filtered = [r for r in filtered if c_lo in r.get("county", "")]
    if not filtered:
        return f"🔍 找不到 city='{city}' station='{station}'"

    lines = [f"🌫 空氣品質即時（環境部 {datetime.now().strftime('%H:%M')}）"]
    for rec in filtered[:15]:
        aqi = rec.get("aqi", "-")
        lines.append(
            f"  {_aqi_icon(aqi)} {rec.get('sitename','?'):6s}"
            f"（{rec.get('county','?')}）  AQI={aqi:3s} [{rec.get('status','-')}]  "
            f"PM2.5={rec.get('pm2.5','-')}  O3={rec.get('o3','-')}"
        )
    if len(filtered) > 15:
        lines.append(f"  ...（還有 {len(filtered) - 15} 個測站）")
    return "\n".join(lines)


def _aqi_icon(aqi: str) -> str:
    try:
        v = int(aqi)
        if v <= 50:
            return "🟢"
        if v <= 100:
            return "🟡"
        if v <= 150:
            return "🟠"
        if v <= 200:
            return "🔴"
        if v <= 300:
            return "🟣"
        return "🟤"
    except (ValueError, TypeError):
        return "⚪"


# ────────────────────────────────────────────────────────────────────
# 2. 台股報價 — yfinance，無 auth
# ────────────────────────────────────────────────────────────────────
def tw_stock_quote(code: str) -> str:
    """查台股即時報價（via yfinance / Yahoo Finance）。無需 API key。

    Args:
        code: 4 位數股票代號，例如「2330」（台積電）、「2317」（鴻海）、
              「0050」（元大台灣 50）。自動試 .TW / .TWO。
    """
    code = (code or "").strip()
    if not re.fullmatch(r"\d{4,6}", code):
        return f"❌ code 格式錯誤（要 4-6 位純數字）：{code!r}"

    try:
        import yfinance as yf
    except ImportError:
        return "❌ yfinance 未裝：pip install yfinance"

    info = None
    symbol = None
    for suffix in (".TW", ".TWO"):
        try:
            t = yf.Ticker(code + suffix)
            info = t.info
            if info and info.get("regularMarketPrice"):
                symbol = code + suffix
                break
        except Exception:
            continue
    if not info or not symbol:
        return f"🔍 查不到台股 {code}（Yahoo 偶爾 block，稍後再試）"

    name = info.get("longName") or info.get("shortName") or code
    price = info.get("regularMarketPrice")
    prev = info.get("regularMarketPreviousClose")
    change = price - prev if (price and prev) else None
    change_pct = (change / prev * 100) if (change and prev) else None
    vol = info.get("regularMarketVolume")
    high52 = info.get("fiftyTwoWeekHigh")
    low52 = info.get("fiftyTwoWeekLow")
    pe = info.get("trailingPE")
    div_yield = info.get("dividendYield")  # yfinance 新版回 percent，不用再 *100
    mcap = info.get("marketCap")

    arrow = "🔺" if (change and change > 0) else ("🔻" if change and change < 0 else "─")

    lines = [f"📈 {name}（{symbol}）"]
    if change is not None:
        lines.append(f"  現價: {price}  {arrow}{change:+.2f}（{change_pct:+.2f}%）")
    else:
        lines.append(f"  現價: {price}")
    if vol:
        lines.append(f"  成交量: {vol:,} 股")
    if high52 and low52:
        lines.append(f"  52 週: {low52} ~ {high52}")
    if pe:
        lines.append(f"  本益比: {pe:.2f}")
    if div_yield:
        # yfinance 1.x 開始直接回 percent（例如 1.17 代表 1.17%）
        # 若值 < 1（早期格式 0.0117）再 *100
        pct = div_yield if div_yield >= 1 else div_yield * 100
        lines.append(f"  殖利率: {pct:.2f}%")
    if mcap:
        lines.append(f"  市值: {mcap/1e8:.1f} 億")
    lines.append(f"  資料時間: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 3. 中央氣象署警特報（要 authorization token，CWA 官方開放）
# ────────────────────────────────────────────────────────────────────
_CWA_WARNINGS_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/W-C0033-001"


def tw_weather_alert() -> str:
    """查中央氣象署當下生效中的警特報（颱風、豪雨、低溫、濃霧等）。

    ⚠️ 需 Authorization token（免費）：https://opendata.cwa.gov.tw/user/authkey
        keyring: 'xiaohong-agent' / 'cwa-api-key'

    Returns:
        各警報項目 + 適用地區 + 發布時間。沒警報就回「目前無警報」。
    """
    token = _get_secret("cwa-api-key")
    if not token:
        return _setup_hint("cwa-api-key", "https://opendata.cwa.gov.tw/user/authkey")

    try:
        r = requests.get(_CWA_WARNINGS_URL, params={"Authorization": token}, timeout=15)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        return f"❌ 抓氣象警報失敗：{type(e).__name__}: {e}"

    # CWA API 結構：records.location[].hazardConditions.hazards[]
    records = (j.get("records") or {})
    locations = records.get("location") or []
    active_alerts = []
    for loc in locations:
        hazards = ((loc.get("hazardConditions") or {}).get("hazards") or [])
        for h in hazards:
            info = h.get("info") or {}
            phenomena = info.get("phenomena") or "-"
            significance = info.get("significance") or ""
            end_time = info.get("validTime", {}).get("endTime", "")
            if phenomena and phenomena != "-":
                active_alerts.append({
                    "loc": loc.get("locationName", "?"),
                    "phenomena": phenomena,
                    "significance": significance,
                    "end": end_time,
                })

    if not active_alerts:
        return "☀️ 目前沒有任何氣象警報生效"

    # 按警報類型 group
    lines = [f"⚠️ 中央氣象署當前警特報（{len(active_alerts)} 則）"]
    by_type = {}
    for a in active_alerts:
        key = f"{a['phenomena']} {a['significance']}".strip()
        by_type.setdefault(key, []).append(a["loc"])
    for alert_type, locs in by_type.items():
        lines.append(f"\n🔴 {alert_type}")
        lines.append(f"   適用: {', '.join(locs[:10])}"
                     + (f"（+{len(locs)-10}）" if len(locs) > 10 else ""))
        # 從第一個地點取 end time（通常整體警報有效期相同）
        end = next((a["end"] for a in active_alerts if a["phenomena"] == alert_type.split()[0]), "")
        if end:
            lines.append(f"   至: {end}")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 4. 郵遞區號（本地 table，無 auth）
# ────────────────────────────────────────────────────────────────────
_POSTAL_3CODE = {
    # 台北市各區
    "中正區": "100", "大同區": "103", "中山區": "104", "松山區": "105",
    "大安區": "106", "萬華區": "108", "信義區": "110", "士林區": "111",
    "北投區": "112", "內湖區": "114", "南港區": "115", "文山區": "116",
    # 新北市（常用）
    "板橋區": "220", "三重區": "241", "中和區": "235", "永和區": "234",
    "新莊區": "242", "新店區": "231", "土城區": "236", "蘆洲區": "247",
    "汐止區": "221", "樹林區": "238", "淡水區": "251", "林口區": "244",
    "三峽區": "237", "鶯歌區": "239", "泰山區": "243",
    # 桃園市
    "桃園區": "330", "中壢區": "320", "平鎮區": "324", "八德區": "334",
    "龜山區": "333", "蘆竹區": "338", "大溪區": "335", "楊梅區": "326",
    # 台中市
    "北屯區": "406", "西屯區": "407", "南屯區": "408",
    "臺中市北區": "404", "臺中市東區": "401",
    "西區": "403", "南區": "402", "豐原區": "420",
    "大里區": "412", "太平區": "411", "烏日區": "414",
    # 高雄市
    "苓雅區": "802", "前鎮區": "806", "三民區": "807", "左營區": "813",
    "楠梓區": "811", "鳳山區": "830", "小港區": "812", "鼓山區": "804",
    "新興區": "800", "前金區": "801", "鹽埕區": "803", "岡山區": "820",
    # 台南市
    "臺南市東區": "701", "臺南市北區": "704",
    "中西區": "700", "安南區": "709",
    "安平區": "708", "永康區": "710", "仁德區": "717", "歸仁區": "711",
    # 新竹（市 + 縣）
    "新竹市": "300", "竹北市": "302", "竹東鎮": "310",
    # 嘉義
    "嘉義市": "600",
}
_AMBIGUOUS_POSTAL_DISTRICTS = {"東區", "北區"}


def tw_postal_code(address: str) -> str:
    """從地址推測前 3 碼郵遞區號。

    涵蓋六都主要行政區 + 新竹 + 嘉義。完整 7 碼請至中華郵政。

    Args:
        address: 地址字串，例如「台北市大安區復興南路一段 30 號」。
    """
    addr = (address or "").strip().replace("台", "臺")
    if not addr:
        return "❌ address 不能空"

    matches = []
    for district, code in _POSTAL_3CODE.items():
        if district in addr:
            matches.append((district, code))

    if not matches:
        ambiguous = sorted(d for d in _AMBIGUOUS_POSTAL_DISTRICTS if d in addr)
        if ambiguous:
            return (f"🔍 '{address}' 包含重名行政區（{', '.join(ambiguous)}）。\n"
                    "請加上縣市，例如「臺中市東區」或「臺南市東區」。")
        return (f"🔍 '{address}' 推測不出郵遞區號。\n"
                "完整查詢請至中華郵政：https://postcode.post.gov.tw/")

    # 最長的 match 最準（例如「中山區」優先於「中山」）
    matches.sort(key=lambda x: -len(x[0]))
    district, code = matches[0]
    return f"📮 {address}\n  → 前 3 碼郵遞區號：{code}（{district}）"


SKILL_TOOLS = [aqi_now, tw_stock_quote, tw_weather_alert, tw_postal_code]

for fn in SKILL_TOOLS:
    fn.background_safe = True  # 全部純讀
