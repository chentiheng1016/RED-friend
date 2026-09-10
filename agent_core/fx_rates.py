"""美金對台幣即時匯率 —— 每日排程推播的資料面（全確定性，不經 LLM）。

為什麼不用台銀牌告（skills/taiwan_public.bot_exchange_rates）：rate.bot.com.tw
已經掛上 JS proof-of-work bot 防護，CSV 端點對任何非瀏覽器 client 一律回
「Challenge Validation」頁（實測連正常 User-Agent 也擋）。那道防護就是要擋自動化，
繞過它不在選項內 —— 所以這裡改用「可正常存取的公開市場報價」，並在訊息裡把口徑
講清楚：這是**國際外匯市場即時中價**，不是銀行牌告買入/賣出。

零幻覺三道防線（〈員工零幻覺〉）：
  1. 多來源交叉核對 —— 主來源報一個數，另一個獨立來源核一次，差太多就標 ⚠️
     而不是安靜地挑一個。
  2. 合理區間閘 —— 20–50 TWD/USD 之外的值一律當該來源壞掉丟棄（防倒數、防
     回傳到別的幣別）。
  3. 全部失敗就明講失敗 —— 絕不從記憶或歷史檔湊一個數字出來充當今天的報價。

漲跌是跟 var/state 的歷史檔比對算出來的，不是估的；沒有可比的前次紀錄就不寫漲跌。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from agent_core.logging_and_paths import STATE_DIR, _atomic_write_text, logger

# 單一來源的 HTTP 逾時。四個來源全部逾時的最壞情況 = 4 × 10s，仍遠小於
# dispatcher 的 per-task deadline。
_HTTP_TIMEOUT_S = 10

# 合理區間閘：USD/TWD 有史以來沒離開過 24–36。放寬到 20–50 當「這來源壞了」的
# 判準 —— 倒數（1/32.4 = 0.031）、報成別的幣別、回傳 0 都會被這道閘擋掉。
_RATE_MIN = 20.0
_RATE_MAX = 50.0

# 兩個來源差超過這個比例就在訊息裡標 ⚠️。0.5% ≈ 0.16 TWD，正常的來源間差異
# （不同快照時點 / 不同報價商）落在 0.1–0.2%。
_CROSS_CHECK_WARN_PCT = 0.5

# 報價超過這個時數就提醒「市場休市中」。週末推播時 quoted_at 會是週五收盤，
# 不講清楚會讓人以為是當下的價。
_STALE_HOURS = 6

# 歷史檔：算漲跌用。只存極少量欄位，保留最近 N 筆。
_HISTORY_FILE = os.path.join(STATE_DIR, "usd_twd_rate_history.json")
_HISTORY_KEEP = 60

# 算「較上次」時忽略太靠近的紀錄 —— 手動試跑一次不該把 09:00 vs 14:00 的比較
# 洗成「較上次 +0.001」。
_MIN_COMPARE_GAP = timedelta(minutes=30)


@dataclass(frozen=True)
class Reading:
    """一個來源的一次報價。"""
    source: str
    rate: float
    quoted_at: datetime | None  # 來源自稱的報價時間（本地時區）；None = 來源沒給


# ────────────────────────────────────────────────────────────────────
# 各來源的解析器：payload -> (rate, quoted_at) 或 raise
# ────────────────────────────────────────────────────────────────────
def _local(dt: datetime) -> datetime:
    """轉成本機時區的 naive datetime（repo 其他地方一律用 naive local）。"""
    return dt.astimezone().replace(tzinfo=None)


def _parse_fxratesapi(payload: Any) -> tuple[float, datetime | None]:
    rate = float(payload["rates"]["TWD"])
    ts = payload.get("timestamp")
    quoted = _local(datetime.fromtimestamp(int(ts), tz=timezone.utc)) if ts else None
    return rate, quoted


def _parse_coinbase(payload: Any) -> tuple[float, datetime | None]:
    # Coinbase 不給報價時間，只給即時值。
    return float(payload["data"]["rates"]["TWD"]), None


def _parse_er_api(payload: Any) -> tuple[float, datetime | None]:
    rate = float(payload["rates"]["TWD"])
    ts = payload.get("time_last_update_unix")
    quoted = _local(datetime.fromtimestamp(int(ts), tz=timezone.utc)) if ts else None
    return rate, quoted


def _parse_rter(payload: Any) -> tuple[float, datetime | None]:
    row = payload["USDTWD"]
    rate = float(row["Exrate"])
    quoted = None
    raw = str(row.get("UTC") or "").strip()
    if raw:
        try:
            quoted = _local(
                datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            )
        except ValueError:
            quoted = None
    return rate, quoted


# 順序 = 優先序。第一個成功的當主報價，第二個成功的當交叉核對。
# 全部免 API key、全部實測可直連（2026-08 驗證）。
_SOURCES: tuple[tuple[str, str, Callable[[Any], tuple[float, datetime | None]]], ...] = (
    ("FXRatesAPI", "https://api.fxratesapi.com/latest?base=USD&currencies=TWD", _parse_fxratesapi),
    ("Coinbase", "https://api.coinbase.com/v2/exchange-rates?currency=USD", _parse_coinbase),
    ("ExchangeRate-API", "https://open.er-api.com/v6/latest/USD", _parse_er_api),
    ("rter.info", "https://tw.rter.info/capi.php", _parse_rter),
)


def _fetch_one(name: str, url: str, parser) -> tuple[Reading | None, str]:
    """抓一個來源。回 (Reading, "") 或 (None, 錯誤描述)。任何例外都收斂成錯誤字串。"""
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — 來源掛掉不該炸掉整份報價
        return None, f"{name}: {type(exc).__name__}: {str(exc)[:80]}"
    try:
        rate, quoted_at = parser(payload)
    except Exception as exc:  # noqa: BLE001 — 格式變了也一樣
        return None, f"{name}: 回傳格式非預期（{type(exc).__name__}）"
    if not (_RATE_MIN <= rate <= _RATE_MAX):
        return None, f"{name}: 報價 {rate} 不在合理區間 {_RATE_MIN}–{_RATE_MAX}"
    return Reading(source=name, rate=rate, quoted_at=quoted_at), ""


def fetch_readings(want: int = 2) -> tuple[list[Reading], list[str]]:
    """依序抓來源，湊滿 want 筆有效報價就停。回 (readings, errors)。

    want=2 = 一個主報價 + 一個交叉核對。前兩個都活著時只打兩支 API。
    """
    readings: list[Reading] = []
    errors: list[str] = []
    for name, url, parser in _SOURCES:
        reading, err = _fetch_one(name, url, parser)
        if reading is not None:
            readings.append(reading)
            if len(readings) >= max(1, want):
                break
        else:
            errors.append(err)
    return readings, errors


# ────────────────────────────────────────────────────────────────────
# 歷史（算漲跌用）
# ────────────────────────────────────────────────────────────────────
def _load_history() -> list[dict]:
    try:
        with open(_HISTORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("readings") or []
        return rows if isinstance(rows, list) else []
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 — 壞檔不該讓今天的報價發不出去
        logger.warning("usd_twd_rate_history.json 讀取失敗（%s），視為空", exc)
        return []


def _append_history(rows: list[dict], entry: dict) -> None:
    rows = (rows + [entry])[-_HISTORY_KEEP:]
    try:
        os.makedirs(os.path.dirname(_HISTORY_FILE), exist_ok=True)
        _atomic_write_text(
            _HISTORY_FILE,
            json.dumps({"version": 1, "readings": rows}, ensure_ascii=False, indent=2),
        )
    except Exception as exc:  # noqa: BLE001 — 存不了歷史只是少了漲跌，照樣要推播
        logger.warning("usd_twd_rate_history.json 寫入失敗（%s）", exc)


def _previous_reading(rows: list[dict], now: datetime) -> dict | None:
    """挑最後一筆「夠久以前」的紀錄當比較基準。"""
    for row in reversed(rows):
        try:
            at = datetime.fromisoformat(str(row.get("at")))
        except Exception:  # noqa: BLE001
            continue
        if now - at >= _MIN_COMPARE_GAP and isinstance(row.get("rate"), (int, float)):
            return row
    return None


# ────────────────────────────────────────────────────────────────────
# 對外：組出可直接推播的訊息
# ────────────────────────────────────────────────────────────────────
def _fmt_change(current: float, prev_row: dict) -> str:
    prev_rate = float(prev_row["rate"])
    delta = current - prev_rate
    pct = (delta / prev_rate * 100) if prev_rate else 0.0
    arrow = "📈" if delta > 0 else ("📉" if delta < 0 else "➖")
    sign = "+" if delta > 0 else ""
    try:
        when = datetime.fromisoformat(str(prev_row.get("at"))).strftime("%m/%d %H:%M")
    except Exception:  # noqa: BLE001
        when = "上次"
    return (
        f"{arrow} 較上次 {sign}{delta:.3f}（{sign}{pct:.2f}%）"
        f"｜{when} 為 {prev_rate:.3f}"
    )


def usd_twd_brief(now: datetime | None = None, record: bool = True) -> str:
    """組出「美金→台幣」即時匯率推播訊息（純文字，Telegram 直接可讀）。

    record=True 會把這次報價寫進歷史檔（下次推播的漲跌基準）。
    抓不到任何來源時回 ❌ 開頭的明確失敗訊息 —— 不猜、不用舊值假裝是今天的價。
    """
    now = now or datetime.now()
    readings, errors = fetch_readings(want=2)

    if not readings:
        detail = "\n".join(f"  • {e}" for e in errors) or "  • （無錯誤明細）"
        return (
            "❌ 美金對台幣匯率：今天抓不到任何報價來源，這次不推數字（不猜）。\n"
            f"🕐 {now:%Y-%m-%d %H:%M}\n各來源錯誤：\n{detail}"
        )

    primary = readings[0]
    lines = [
        "💵 美金 → 台幣 即時匯率",
        f"🕐 {now:%Y-%m-%d (%a) %H:%M}",
        "",
        f"　　1 USD = {primary.rate:.2f} TWD",
    ]

    history = _load_history()
    prev = _previous_reading(history, now)
    if prev:
        lines.append(_fmt_change(primary.rate, prev))

    lines.append("")
    if primary.quoted_at:
        age_h = (now - primary.quoted_at).total_seconds() / 3600
        stale = "　⚠️ 市場休市中，這是最後成交價" if age_h >= _STALE_HOURS else ""
        lines.append(f"報價時間：{primary.quoted_at:%m/%d %H:%M}{stale}")

    source_line = f"資料來源：{primary.source}"
    if len(readings) > 1:
        cross = readings[1]
        diff_pct = abs(cross.rate - primary.rate) / primary.rate * 100
        flag = " ⚠️ 兩來源差異偏大，數字請再確認" if diff_pct > _CROSS_CHECK_WARN_PCT else ""
        source_line += f"（交叉核對 {cross.source} {cross.rate:.2f}，差 {diff_pct:.2f}%）{flag}"
    else:
        source_line += "（只有單一來源可用，未交叉核對）"
    lines.append(source_line)

    lines.append("")
    lines.append(
        "ℹ️ 以上是國際外匯市場即時中價。銀行實際換匯以牌告為準："
        "收美金換台幣看「即期買入」（會略低），台幣買美金看「即期賣出」（會略高）。"
    )

    if record:
        _append_history(history, {
            "at": now.isoformat(timespec="seconds"),
            "rate": round(primary.rate, 4),
            "source": primary.source,
            "quoted_at": primary.quoted_at.isoformat(timespec="seconds") if primary.quoted_at else None,
        })

    return "\n".join(lines)
