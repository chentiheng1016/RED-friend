"""Alert thresholds — 把「狀況不對」的時刻自動標出來。

有了 dashboard_trends 算 today vs baseline，這層在上面加「**自動判斷
是否該叫人**」邏輯：

  - daemon 連續 N 次 exit 非 0 → ALERT
  - 今日 cost 超過 X 元（或 2× 7-day avg）→ ALERT
  - 任務失敗率 > 30% → ALERT
  - error log 比 7 天平均多 300% → ALERT
  - email_ingest 已超過 6 小時沒成功跑 → ALERT
  - ChromaDB 條目數比昨天少 5% → ALERT（疑似資料損毀）
  - 共用 Chroma server heartbeat 無回應 → ALERT（全 fleet RAG/記憶停擺）
  - Gemini API 錯誤率 > 20% → ALERT（503 風暴 / 配額 / 逾時）
  - ERP 鏡像跑批時長超過門檻（跑得成但一路變長）→ ALERT
  - ERP 主機 RDP 巡檢旗標（非白名單成功登入 / 真實帳號被密集猜密碼）→ ALERT

`check_alerts()` 回傳 list of dict，每個 dict 含：
  {
    "id": "cost_high",       # 穩定識別碼
    "level": "warn"|"crit",  # warn = 注意，crit = 立刻處理
    "title": "今日成本異常高",
    "detail": "今日 US$2.30 是 7-day avg US$0.40 的 5.7×",
    "metric": {"today": 2.30, "baseline": 0.40, "ratio": 5.75},
    "advice": "查 cost_by_tool 看哪個工具暴衝；可能是 prompt 失控 / loop",
  }

`system_alerts()` 是 LLM-facing wrapper：把 alerts 渲染成可讀文字。
適合 schedule task 每 15 分鐘跑一次：有 alert 就 push_briefing_telegram。
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta

from agent_core.logging_and_paths import logger
from agent_core.daemon_launchd_state import (
    is_benign_stopped_daemon,
    is_known_daemon_recovering,
    short_launchd_label,
)


# ────────────────────────────────────────────────────────────────────
# 成本門檻的單位基準 — 直接取自 cost_tracker 的牌價表
# ────────────────────────────────────────────────────────────────────
# 推導失敗時的保守 fallback = 2026-08-05 改制當下的 3.6-flash input 牌價。
# 只在牌價表讀不到／壞掉時才會用到 —— 寧可用一組略舊的數字，也不要讓成本紅線
# 整條消失（或變成 0 而狂告警）。
_FLASH_INPUT_RATE_FALLBACK = 1.50


def _flash_input_rate() -> float:
    """1M 顆 gemini-3.6-flash input token 的價（目前 USD 1.50）。

    成本門檻一律表述成「幾個這種單位」而不是寫死金額 —— 這正是 2026-08-05 那次
    假告警的根因：`_PRICING`（cost_tracker）在 #353 從新台幣改成美元，門檻卻還
    留在台幣刻度上，兩份各自寫死的數字沒有任何東西保證它們同步。當天 redeploy
    後的 process 拿新 USD 門檻（$37）去加總還是台幣口徑的帳本列（US$77.83，
    換算回美元只有 $2.40）就報了紅。

    改成從同一張牌價表推導之後，牌價換幣別、換單位、換級距，門檻都會自動跟著走，
    「單位對不上」這個 bug 類別在結構上消失。
    """
    try:
        from agent_core.cost_tracker import _PRICING
        rate = float(_PRICING["gemini-3.6-flash"][0])
    except Exception as exc:  # noqa: BLE001 - 門檻算不出來不能讓整份健檢死掉
        logger.warning("成本門檻無法從牌價表推導，改用 fallback 刻度：%s", exc)
        return _FLASH_INPUT_RATE_FALLBACK
    # 牌價表被改壞（0 或負）時不要把紅線一起帶壞 → 退回改制當下的刻度
    return rate if rate > 0 else _FLASH_INPUT_RATE_FALLBACK


# ────────────────────────────────────────────────────────────────────
# Default thresholds — 大王 可在這裡或透過 env var 改
# ────────────────────────────────────────────────────────────────────
_DEFAULTS = {
    # 成本絕對門檻 — 以 _flash_input_rate() 為單位（見上）。倍數是 2026-08-05
    # 從當時的實際水位訂的，刻意讓觸發點跟改制前的 25.0 / 37.0 / 3.0 幾乎不變，
    # 只是換成一個不會跟牌價走鐘的表述方式。當時實際日燒 ≈ US$11。
    "cost_today_warn_usd": 17 * _flash_input_rate(),   # ≈US$25.5：重日但可能正常
    "cost_today_crit_usd": 25 * _flash_input_rate(),   # ≈US$37.5：明顯異常暴衝
    "cost_recent_window_min": 30.0,    # recent burn window for active cost incidents
    "cost_recent_crit_usd": 2 * _flash_input_rate(),   # ≈US$3：夜跑 embedding
                                       # 正常 ~US$0.5–1/30min，US$3 = 失控迴圈級
    "cost_today_ratio_to_avg_warn": 2.5,  # 今日 > 2.5× 7d-avg → warn
    "cost_today_ratio_to_avg_crit": 5.0,  # > 5× → crit
    # 月度上限預警 — 對應 Google AI Studio 的 monthly spend cap（撞到回 429）。
    # cap 預設 0 = 關閉（沒設就不檢查）；用 RED_ALERT_COST_MONTHLY_CAP_USD 設你
    # 在 AI Studio 設的上限金額，接近時就 Telegram 預警，不必等小紅報 429。
    "cost_monthly_cap_usd": 0.0,
    "cost_monthly_warn_pct": 80.0,     # 月用量 ≥ cap 的 80% → warn
    "cost_monthly_crit_pct": 95.0,     # ≥ 95% → crit（快撞牆）
    "runs_err_pct_warn": 30.0,         # 任務失敗率 > 30% warn
    "runs_err_pct_crit": 60.0,         # > 60% crit
    # 比例超標**還要**有這麼多筆絕對失敗才報。runs/index.jsonl 只記
    # wrap_sensitive_tool 級的敏感工具，實測真實流量每日中位數 3 筆 —— 在這種
    # 量級上，比例是個統計上沒有意義的數字（2/5 就 40%）。3 筆真失敗值得看，
    # 1 筆不值得。見 project_dashboard_alerts_fp_audit。
    "runs_err_min_count": 3,
    "errors_today_count_warn": 100,    # 今日 ERROR 行 > 100 條
    "errors_today_count_crit": 500,
    "errors_today_ratio_to_avg_warn": 3.0,  # > 3× 7d-avg
    "errors_today_ratio_to_avg_crit": 10.0,
    # ratio 判準的基線下限：7d 平均低於這個數就不做倍數比較。以前是
    # `avg7 = trend["7d_avg"] or 1`，平均 0 時被換成 1 → 10 條錯誤就 crit「暴衝」，
    # 基線越乾淨越容易炸。10 條/天是「這個 repo 有在動」的合理下限。
    "errors_ratio_min_baseline": 10.0,
    # 1.5h = 4 個 daemon cycle (15 分鐘/cycle * 4 = 60min + 緩衝 30min)
    # 改用 heartbeat file（每跑必寫，含空跑）所以可以收緊很多
    # 之前 6h 看 parquet mtime — 空跑不更新 → 看起來 stale 但其實 daemon 健康
    "email_ingest_stale_hours": 1.5,
    "chroma_drop_pct_warn": 5.0,       # 比昨天少 > 5%
    # 外部 API（Gemini）錯誤率紅線 — 讀 cost.jsonl(成功) + api_errors.jsonl(失敗)
    "api_error_window_hours": 6.0,
    # embedding 路徑自己的失敗紅線（跟上面的生成路徑是兩套配額、兩套失敗模式）。
    #
    # 🚨 判準是**絕對筆數**，不是比例。#414 初版訂「≥200 批且失敗率 ≥30%」，事後
    # 去量歷史基線才發現那是一條**永遠不會響的鈴**：
    #
    #     2026-07-27 → 08-15，4 份 rag_sync log（每晚 7–15h、數百萬次 embed）
    #       「giving up」（重試用盡）      0 次
    #       「aborting retries」（硬配額） 0 次
    #       「attempt 1/5; sleep」（重試）  1 次 ← 重試一次就成功了
    #
    # 三週零筆。在零基線上要求「6 小時內 60 批以上永久失敗」等於不存在的門檻。
    # 這跟普查 C 是同一個形狀的錯（在每日 3 筆的量級上談比例沒有意義），只是更
    # 極端 —— **只要出現重試用盡的批次，本身就已經前所未見**。
    #
    # 所以：絕對筆數當主閘（≥5 批就 warn），比例只用來把「整條掛掉」升級成 crit。
    # 沒有 min_calls —— 那個閘在零基線下的唯一作用是讓紅線更難響。
    "embed_error_window_hours": 6.0,
    "embed_error_min_count": 5,
    "embed_error_rate_crit_pct": 50.0,
    "api_error_min_calls": 20.0,       # 樣本不足不報（避免少量呼叫的偽陽）
    "api_error_rate_warn_pct": 20.0,
    "api_error_rate_crit_pct": 50.0,
}


def _env_override(key: str, default: float) -> float:
    """允許 env var RED_ALERT_<KEY>=value 蓋掉 default。走 env_float（葉模組唯一解析者，
    會拒 nan/±inf）——否則 bare float() 吃得下 RED_ALERT_..._CRIT_USD=nan，靜默讓那條
    crit 紅線失效，或 -inf 狂告警（健檢 Low）。"""
    from agent_core.env_utils import env_float
    return env_float(f"RED_ALERT_{key.upper()}", default)


def _t(key: str) -> float:
    return _env_override(key, _DEFAULTS[key])


def _observed_age_h(since_ts: float) -> float:
    """距離 `since_ts` 的「實際觀測時數」—— 扣掉機器睡著 / 沒在監控的空窗。

    這台是 MacBook：闔上蓋子睡 10 小時，所有 age-based 判準會同時超標、醒來噴一
    整排告警，而系統從頭到尾都好的（2026-07-07 實際發生過）。門檻本身沒錯（ERP
    鏡像真的 26 小時沒刷就是有事），錯的是把「沒人在線的時間」也算進去。

    awake_clock 壞掉或沒有歷史時退回原始 age ＝ 維持加這層之前的行為（寧可誤報
    也不漏報）。見 agent_core/awake_clock.py。
    """
    # 「現在」一律取本模組的 datetime.now() —— 本檔多處測試用
    # `mock.patch.object(dashboard_alerts, "datetime", _Clock)` 造假時鐘，
    # 直接讓 awake_clock 去讀 time.time() 會繞過那層、算出天文數字的 age。
    now_ts = datetime.now().timestamp()
    try:
        from agent_core.awake_clock import observed_age_hours
        return observed_age_hours(since_ts, now=now_ts)
    except Exception:  # noqa: BLE001 - 觀測簿記壞掉不該讓告警本身消音
        return max(0.0, (now_ts - since_ts) / 3600.0)


# ────────────────────────────────────────────────────────────────────
# Individual checks — 每個都回 list[alert] 或空 list
# ────────────────────────────────────────────────────────────────────
def _check_cost() -> list[dict]:
    from agent_core.dashboard_trends import cost_trend
    trend = cost_trend()
    if not trend:
        return []
    today = trend["today_usd"]
    avg7 = trend["7d_avg_usd"]
    recent_window_min = _t("cost_recent_window_min")
    recent_usd = _recent_cost_usd(recent_window_min)
    recent_crit = _t("cost_recent_crit_usd")
    alerts = []
    # Absolute thresholds — 單一穩定 id "cost_today"，嚴重度用 level 表達。
    # （健檢 Medium：以前 warn/crit 拆成 cost_today_warn / cost_today_high 兩個
    #  id，惡化時 alert_pusher 會先推舊 id 的「✅ 已恢復」再推新 id，warn→crit
    #  的 _escalated 升級繞流永不生效。同檔 email_ingest_stale / cost_monthly_cap
    #  是正確樣板。）
    if today >= _t("cost_today_crit_usd"):
        level = "crit" if recent_usd >= recent_crit else "warn"
        active_note = (
            f"最近 {recent_window_min:.0f} 分鐘 US${recent_usd:.4f}"
            f" {'≥' if level == 'crit' else '<'} active 上限 US${recent_crit:.2f}"
        )
        alerts.append({
            "id": "cost_today",
            "level": level,
            "title": "今日成本超過上限" if level == "crit" else "今日成本已超標（目前未持續暴衝）",
            "detail": (
                f"今日 US${today:.4f} ≥ crit 上限 US${_t('cost_today_crit_usd'):.2f}；"
                f"{active_note}"
            ),
            "metric": {
                "today": today,
                "threshold": _t("cost_today_crit_usd"),
                "recent_usd": recent_usd,
                "recent_window_min": recent_window_min,
                "recent_crit": recent_crit,
            },
            "advice": (
                "立刻查 cost_by_tool() 看哪個工具暴衝；可能是 prompt 失控 / 死循環"
                if level == "crit"
                else "成本已超標但近期燒錢已降下來；保持 guard 開啟並觀察到隔日歸零"
            ),
        })
    elif today >= _t("cost_today_warn_usd"):
        alerts.append({
            "id": "cost_today",
            "level": "warn",
            "title": "今日成本偏高",
            "detail": f"今日 US${today:.4f} ≥ warn 上限 US${_t('cost_today_warn_usd'):.2f}",
            "metric": {"today": today, "threshold": _t("cost_today_warn_usd")},
            "advice": "看 cost_by_tool() 列出 top consumer；考慮拆分長 prompt",
        })
    # Ratio-to-avg — 同樣收斂成單一 id "cost_ratio"（同上述 warn/crit 拆 id bug）
    #
    # 2026-08-14 假警報普查 D：原本懷疑「#388 把 embedding token 估算調高 1.8×
    # → 之後 7 天分母混紀元、ratio 必然虛高」。**實測推翻了這個前提**：拿 16 天
    # cost.jsonl 分帳，embedding 只佔日金額 2–10%（08-02~04 那 41–74% 是背填期），
    # 把舊紀元 embedding 補 1.8× 後 ratio 只從 0.50× 動到 0.48× —— 對 2.5× 的
    # warn 門檻差了一個數量級，單獨不可能觸發假警報。所以**不**排除跨紀元的天數
    # （_avg 本來就已經擋掉 pricing_cutover 之前的日子）。
    # 真正缺的是「看得出誰在燒」：embedding 佔比是背填/重建日的指紋，直接寫進
    # detail，收到告警的人第一眼就能分辨異常暴衝 vs 計畫中的背填。
    if avg7 > 0.01 and today > 0:
        ratio = today / avg7
        emb = float(trend.get("today_embedding_usd") or 0.0)
        emb_note = (
            f"；其中 embedding US${emb:.4f}（{emb / today * 100:.0f}%）"
            if emb > 0 else ""
        )
        detail = f"今日 US${today:.4f} = {ratio:.1f}× 7-day avg US${avg7:.4f}{emb_note}"
        metric = {"today": today, "baseline": avg7, "ratio": ratio,
                  "today_embedding_usd": emb}
        if ratio >= _t("cost_today_ratio_to_avg_crit"):
            alerts.append({
                "id": "cost_ratio",
                "level": "crit",
                "title": "今日成本相對 7-day 平均暴衝",
                "detail": detail,
                "metric": metric,
                "advice": (
                    "看是不是新加的 tool 或 prompt 在 loop；embedding 佔比高"
                    "通常是 RAG 背填/重建（對照 cost_by_tool 確認）"
                ),
            })
        elif ratio >= _t("cost_today_ratio_to_avg_warn"):
            alerts.append({
                "id": "cost_ratio",
                "level": "warn",
                "title": "今日成本明顯高於最近平均",
                "detail": detail,
                "metric": metric,
                "advice": "對比昨日 cost_by_tool 看差別；非異常就忽略",
            })
    return alerts


def _recent_cost_usd(minutes: float) -> float:
    """Gemini spend in the last N minutes; used to tell active burn from history."""
    try:
        from agent_core.cost_tracker import _load_entries
    except Exception:
        return 0.0
    cutoff = datetime.now() - timedelta(minutes=max(1.0, minutes))
    total = 0.0
    for entry in _load_entries(hours=max(1, int(minutes / 60) + 1)):
        try:
            ts = datetime.fromisoformat(str(entry.get("ts", "")))
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        try:
            total += float(entry.get("cost_usd") or entry.get("usd") or 0.0)
        except (ValueError, TypeError):
            continue
    return total


def _check_monthly_cap() -> list[dict]:
    """月度 spend cap 預警 — 接近 AI Studio 月上限就提早叫人。

    只有設了 cap（RED_ALERT_COST_MONTHLY_CAP_USD > 0）才會檢查；沒設就靜默。
    走 dashboard_alerts → alert_pusher → Telegram/Gmail 推播，**不經 Gemini**，
    所以即使已經 100% 撞牆回 429，這則預警仍送得出去。
    """
    cap = _t("cost_monthly_cap_usd")
    if cap <= 0:
        return []
    from agent_core.cost_tracker import month_to_date_usd
    spent = month_to_date_usd()
    pct = (spent / cap) * 100 if cap > 0 else 0.0
    warn_pct = _t("cost_monthly_warn_pct")
    crit_pct = _t("cost_monthly_crit_pct")
    if pct < warn_pct:
        return []
    level = "crit" if pct >= crit_pct else "warn"
    title = (
        "Gemini 月度上限即將撞牆" if level == "crit"
        else "Gemini 月度用量接近上限"
    )
    return [{
        "id": "cost_monthly_cap",  # 穩定 id → alert_pusher 6h throttle + 恢復自動清
        "level": level,
        "title": title,
        "detail": (
            f"本月已花 US${spent:.2f} / cap US${cap:.2f}（{pct:.0f}%）。"
            f"撞到 cap 後所有 Gemini 呼叫會回 429 RESOURCE_EXHAUSTED，"
            f"小紅與所有 daemon 都會停擺。"
        ),
        "metric": {"spent": spent, "cap": cap, "pct": pct},
        "advice": (
            "到 https://ai.studio/spend 調高月上限或確認 billing；"
            "或先開 dry-run / 暫停 ponder 等背景 daemon 降速。"
        ),
    }]


def _check_runs_errors() -> list[dict]:
    """今日任務失敗率 —— 判準是「絕對筆數 ≥ N **且** 比例超標」。

    為什麼不是只看比例（2026-08-14 假警報普查 C）：runs/index.jsonl 只記
    wrap_sensitive_tool 級的敏感工具，拿清乾淨後的 live index 實測，**每日中位數
    3 筆、24 天裡只有 6 天達得到舊的樣本閘 5**。在這種量級上比例是統計上沒有意義
    的數字 —— `2/5 = 40%` 就 warn、`3/5 = 60%` 就 crit，真陽性基礎率極低卻對雜訊
    極敏感（已誤報 07-11 / 07-15 / 08-14 三次）。

    舊的 `today_total < 5` 樣本閘刻意拿掉：它會把「今天只跑了 3 支、3 支全掛」這種
    **真的該看**的日子擋在門外。改用絕對筆數當主閘，比例退居第二條件。
    """
    from agent_core.dashboard_trends import runs_trend
    trend = runs_trend()
    if not trend:
        return []
    today = trend["today"]
    today_total = today.get("total", 0)
    err_pct = trend["today_err_pct"]
    # `err` 缺席時（舊 shape / 其他 backend）從比例回推。
    err_count = today.get("err")
    if err_count is None:
        err_count = int(round(today_total * err_pct / 100.0))
    if err_count < _t("runs_err_min_count"):
        return []
    alerts = []
    # 單一穩定 id "runs_err_pct"，warn/crit 用 level 表達（同 _check_cost 的
    # warn→crit 升級 bug 修法）。
    if err_pct >= _t("runs_err_pct_crit"):
        alerts.append({
            "id": "runs_err_pct",
            "level": "crit",
            "title": "今日任務失敗率異常高",
            "detail": (f"{err_count} 筆失敗 / {today_total} 次 = {err_pct:.1f}%"
                       f"（crit 上限 {_t('runs_err_pct_crit'):.0f}%）"),
            "metric": {"err_pct": err_pct, "total": today_total, "errors": err_count},
            "advice": "看 list_runs(status='error') 找最近 5 個 fail trace；可能是 API quota / 認證 / 環境問題",
        })
    elif err_pct >= _t("runs_err_pct_warn"):
        alerts.append({
            "id": "runs_err_pct",
            "level": "warn",
            "title": "今日任務失敗率偏高",
            "detail": (f"{err_count} 筆失敗 / {today_total} 次 = {err_pct:.1f}%"
                       f"（warn 上限 {_t('runs_err_pct_warn'):.0f}%）"),
            "metric": {"err_pct": err_pct, "total": today_total, "errors": err_count},
            "advice": "看是哪幾個 tool 重複失敗",
        })
    return alerts


def _check_error_log() -> list[dict]:
    from agent_core.dashboard_trends import errors_trend
    trend = errors_trend()
    if not trend:
        return []
    today = trend["today"]
    # 🚨 這裡以前寫 `trend["7d_avg"] or 1`：7 天平均是 0（＝乾淨的一週）時被換成
    # 1，於是「10 條錯誤 / 1 = 10× → crit 暴衝」。基線越乾淨這條越容易炸，正好跟
    # 它想表達的意思相反。ratio 判準需要**真實基線**才有意義，沒有就別做 ratio
    # ——絕對值那段照跑，爆量還是抓得到。
    avg7 = trend["7d_avg"]
    alerts = []
    # Absolute — 單一穩定 id "errors_log"，warn/crit 用 level 表達（同
    # _check_cost 的 warn→crit 升級 bug 修法）。
    if today >= _t("errors_today_count_crit"):
        alerts.append({
            "id": "errors_log",
            "level": "crit",
            "title": "今日 log 錯誤行數爆量",
            "detail": f"{today} 條 ≥ crit {_t('errors_today_count_crit'):.0f}",
            "metric": {"today": today, "7d_avg": avg7},
            "advice": "tail var/logs 找最頻繁的 error type；可能是 daemon 卡 retry 死循環",
        })
    elif today >= _t("errors_today_count_warn"):
        alerts.append({
            "id": "errors_log",
            "level": "warn",
            "title": "今日 log 錯誤行數偏多",
            "detail": f"{today} 條 ≥ warn {_t('errors_today_count_warn'):.0f}",
            "metric": {"today": today, "7d_avg": avg7},
            "advice": "看 system_status 'errors' 段最近 5 條",
        })
    # Ratio —— 基線要夠厚才比得出「相對暴衝」。7d 平均低於 min_baseline 時（近乎
    # 沒有錯誤的一週）任何個位數波動都會變成幾十倍，那不是暴衝、只是分母太小。
    if avg7 >= _t("errors_ratio_min_baseline") and today / avg7 >= _t("errors_today_ratio_to_avg_crit"):
        alerts.append({
            "id": "errors_ratio_crit",
            "level": "crit",
            "title": "今日 log 錯誤暴衝（相對近期）",
            "detail": f"{today} / {avg7:.1f}/天 = {today / avg7:.1f}× ≥ {_t('errors_today_ratio_to_avg_crit'):.0f}×",
            "metric": {"today": today, "7d_avg": avg7},
            "advice": "今天有事 — 馬上看 logs",
        })
    return alerts


def _check_daemon_health() -> list[dict]:
    """launchctl list 看 com.xiaohong.* daemon。
    任何 daemon last_exit 非 0 → warn；連續多個 → crit。
    （沒有歷史紀錄無法判斷「連續 N 次」，現只用 single-shot signal）
    """
    try:
        out = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []
    failed = []
    for line in out.stdout.splitlines():
        if "com.xiaohong" not in line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid_str = parts[0].strip()
        try:
            exit_code = int(parts[1])
        except ValueError:
            continue
        if exit_code == 0:
            continue
        # launchctl reports the *last* exit code even after the daemon has
        # been respawned. If a numeric pid is present the daemon is
        # currently running again — the non-zero exit is a historical
        # marker (e.g. watchdog os._exit(99) before auto-restart) and the
        # alert isn't actionable. Only flag when launchctl shows pid="-"
        # (daemon actually down) AND exit non-zero (down because of fail).
        if pid_str and pid_str != "-":
            continue
        label = short_launchd_label(parts[2])
        if is_known_daemon_recovering(label):
            continue
        if is_benign_stopped_daemon(label, exit_code):
            continue
        failed.append((label, exit_code))
    if not failed:
        return []
    # 一律回「每台 daemon 一條、id 穩定 = daemon_fail_<name>」。
    # （健檢 Medium：以前第 3 台失敗時整批換成單一 daemon_multi_fail id →
    #  個別 id 從清單消失，alert_pusher 對還在失敗的 daemon 推「✅ 已恢復」
    #  誤報；跌回 2 台時又反向誤報一次。改成 id 永遠 per-daemon、規模用
    #  level 表達：≥3 台同時失敗視為系統性問題升 crit，alert_pusher 的
    #  _escalated 會繞過 6h 節流。）
    fleet_wide = len(failed) >= 3
    fleet_note = (
        f"；同時共 {len(failed)} 個 daemon 失敗（疑似系統性問題）"
        if fleet_wide else
        ("（其他 daemon 都 OK）" if len(failed) == 1 else f"（共 {len(failed)} 個 daemon 失敗）")
    )
    return [{
        "id": f"daemon_fail_{name}",
        "level": "crit" if fleet_wide else "warn",
        "title": f"daemon 上次失敗：{name}",
        "detail": f"exit {code}{fleet_note}",
        "metric": {"name": name, "exit": code, "failed_count": len(failed)},
        "advice": (
            f"tail var/logs/daemon-{name}.log 看 stack trace"
            + ("；多台同倒先查系統性原因（斷網/斷電喚醒/quota）" if fleet_wide else "")
        ),
    } for name, code in failed]


def _check_email_ingest_stale() -> list[dict]:
    """email_ingest daemon 應該定期跑（launchd StartInterval=900）。

    優先看 heartbeat file（每跑必寫，含空跑）：
      → daemon 真的死了才會 stale，不會誤判「沒新信但 daemon 還健康」

    Heartbeat 不存在則 fall back 到 parquet mtime（向後相容舊環境）。
    """
    from agent_core.logging_and_paths import STATE_DIR, INTERNAL_LAKE_DIR
    threshold_h = _t("email_ingest_stale_hours")

    # 1. 優先看 heartbeat（精準 — daemon 跑完一定寫）
    heartbeat = os.path.join(STATE_DIR, "email_ingest_heartbeat.json")
    if os.path.isfile(heartbeat):
        mtime = os.path.getmtime(heartbeat)
        age_h = _observed_age_h(mtime)   # 扣掉睡眠 —— 機器沒醒著時 daemon 本來就不會跑
        if age_h <= threshold_h:
            return []
        level = "crit" if age_h > threshold_h * 4 else "warn"
        return [{
            "id": "email_ingest_stale",
            "level": level,
            "title": "Email ingest daemon 沒 heartbeat",
            "detail": (f"heartbeat 已 {age_h:.1f} 小時沒寫入"
                       f"（threshold {threshold_h}h，daemon 應每 15 分鐘跑一次）"),
            "metric": {"age_h": age_h, "threshold_h": threshold_h,
                       "source": "heartbeat"},
            "advice": ("daemon 可能死了或排程沒生效。看 daemon-email_ingest.log "
                       "+ launchctl list com.xiaohong.email_ingest"),
        }]

    # 2. Fallback：沒 heartbeat 就看 parquet mtime（舊行為）
    parquet = os.path.join(INTERNAL_LAKE_DIR, "emails.parquet")
    if not os.path.isfile(parquet):
        return []
    mtime = os.path.getmtime(parquet)
    age_h = _observed_age_h(mtime)
    # parquet mtime 在空跑時不更新 — 用較寬的 threshold（threshold * 4）
    # 才不會誤觸 alert（daemon 健康但沒新信時 parquet 可能 8h 沒動）
    fallback_threshold = threshold_h * 4
    if age_h <= fallback_threshold:
        return []
    level = "crit" if age_h > fallback_threshold * 2 else "warn"
    return [{
        "id": "email_ingest_stale",
        "level": level,
        "title": "Email lake 太久沒更新（無 heartbeat fallback 模式）",
        "detail": (f"emails.parquet 已 {age_h:.1f} 小時沒寫入"
                   f"（fallback threshold {fallback_threshold}h；建議 daemon 升級寫 heartbeat）"),
        "metric": {"age_h": age_h, "threshold_h": fallback_threshold,
                   "source": "parquet_mtime_fallback"},
        "advice": "看 daemon-email_ingest.log 是不是卡 OAuth 過期 / 連線失敗",
    }]


def _check_daemon_stalls() -> list[dict]:
    """長駐 daemon「還活著但卡死」偵測 — 委派給 agent_core.daemon_watchdog。

    與 _check_daemon_health 互補：那邊看「launchctl 顯示 pid='-' 且 exit 非 0」
    （真的掛了）；這邊看「pid 還在但任務心跳 / log mtime 停滯」（活著但 wedge）。
    走 alert_pusher → Telegram，**不經 Gemini**，自帶 dedup / 6h 節流 / 恢復通知。

    telegram bot 由 daemon_watchdog daemon 每 ~2 分鐘自動 kickstart 救回（若
    RED_WATCHDOG_TG_AUTORESTART 開），通常還沒撞到這層 5 分鐘掃描就已恢復；
    這層主要涵蓋「自動重啟關閉 / 連續失敗」與「rag_sync 卡死（不自動重啟）」。
    """
    try:
        from agent_core.daemon_watchdog import detect_stalls
        findings = detect_stalls()
    except Exception:
        return []
    out: list[dict] = []
    for f in findings:
        mins = f.get("age_s", 0) / 60.0
        thr_min = f.get("threshold_s", 0) / 60.0
        if f.get("kind") == "telegram":
            label = f.get("label", "telegram")
            task = (f.get("task") or "")[:40]
            out.append({
                "id": f"daemon_stall_{label}",
                "level": "crit",
                "title": f"Telegram bot 卡死：{label}",
                "detail": (
                    f"process 還活著（pid {f.get('pid')}）但任務 heartbeat 已 "
                    f"{mins:.0f} 分鐘沒前進（門檻 {thr_min:.0f} 分鐘）"
                    f"{('；任務「' + task + '」') if task else ''}"
                ),
                "metric": {"age_min": mins, "threshold_min": thr_min,
                           "pid": f.get("pid"), "label": label},
                "advice": (
                    "看門狗會自動 SIGTERM + launchctl kickstart -k 救回（需 "
                    "RED_WATCHDOG_TG_AUTORESTART=1）；若關閉或重啟一直失敗，"
                    f"手動：launchctl kickstart -k gui/$(id -u)/{label}"
                ),
            })
        elif f.get("kind") == "rag":
            # log_path 是 detect_stalls 取到的「最新」那份輸出 log — 排程 daemon
            # 是 daemon-rag_sync.log，手動補跑是 rag_sync_manual*.log。
            log_name = os.path.basename(f.get("log_path") or "") or "daemon-rag_sync.log"
            out.append({
                "id": "daemon_stall_rag_sync",
                "level": "warn",
                "title": "RAG sync 卡死",
                "detail": (
                    f"rag_sync 正在跑但最新輸出 log（{log_name}）已 {mins:.0f} 分鐘"
                    f"沒寫入（門檻 {thr_min:.0f} 分鐘；判定已涵蓋手動補跑的 "
                    f"rag_sync_manual*.log），疑似 wedge"
                ),
                "metric": {"age_min": mins, "threshold_min": thr_min,
                           "log": log_name},
                "advice": (
                    f"tail var/logs/{log_name} 看卡在哪（大檔 OCR / embedding "
                    "429 backoff？）。要重啟須手動（7–15h 夜跑，重啟靠 content-hash "
                    "dedup 當天續做）：launchctl kickstart -k gui/$(id -u)/com.xiaohong.rag_sync_daily"
                ),
            })
    return out


def _check_chroma_mode_consistency() -> list[dict]:
    """Chroma 共用-server 健康紅線（存取模式一致性）。

    全 fleet 必須走共用 server（RED_CHROMA_HTTP_URL=http://127.0.0.1:8000）；多
    process 直開同一份 index 會腐壞 HNSW → SIGSEGV（2026-06 事故）。這個 check
    在 alert_check daemon（plist 已注入該 env）裡每 5 分鐘跑：

      - http 模式 + server heartbeat 無回應 → crit（全 fleet RAG/記憶停擺）

    只在 http 模式動作。direct 模式（dev / CI / 離線維運）一律靜默——那是合法的
    single-process 情境；而漏帶 env 的 daemon 真去碰 chroma 時，build_chroma_client
    會自己 RuntimeError 擋下（再由 daemon health / error-log 紅線接手），不需在這裡
    重複報，也避免在沒 server 的環境誤報。走 alert_pusher → Telegram，不經 Gemini。
    """
    try:
        from agent_core.chroma_backend import preflight
        status = preflight()
    except Exception:
        return []
    if status.get("mode") != "http" or status.get("server_alive"):
        return []
    return [{
        "id": "chroma_server_down",
        "level": "crit",
        "title": "共用 Chroma server 無回應",
        "detail": (
            f"{status.get('http_url')} heartbeat 探測失敗。全 fleet 的 RAG 檢索 / "
            f"記憶讀寫都會失敗。"
        ),
        "metric": {"http_url": status.get("http_url"), "server_alive": False},
        "advice": (
            "看 com.xiaohong.chroma 是否掛了："
            "launchctl kickstart -k gui/$(id -u)/com.xiaohong.chroma；"
            "log 在 var/logs/daemon-chroma.log"
        ),
    }]


def _check_external_api_health() -> list[dict]:
    """外部 API（Gemini）錯誤率紅線。

    error_rate = 最終失敗 / (最終失敗 + 成功)，取最近 N 小時 = 真實任務失敗率
    （retry 後成功的暫時性 503 不計入，避免告警疲勞）。失敗來自
    cost_tracker.record_api_error（_gemini_generate 放棄時記），成功來自
    cost.jsonl **的生成路徑列**——embedding 已在 api_error_stats 排除（分子不可能
    有 embedding，分母混進去只會稀釋；實測 24h 稀釋 4.1×、背填夜 82×，見該函式
    docstring）。樣本 < min_calls 不報，而這個閘也因此改看生成路徑的真實樣本數。
    監看背景 fleet + RAG + 工具這條主路徑（_gemini_generate）；互動 Telegram
    chat.send_message 是另一條，暫不計入。
    """
    try:
        from agent_core.cost_tracker import api_error_stats
        stats = api_error_stats(hours=_t("api_error_window_hours"))
    except Exception:
        return []
    total = stats.get("total", 0)
    if total < _t("api_error_min_calls"):
        return []
    rate = stats.get("error_rate_pct", 0.0)
    warn = _t("api_error_rate_warn_pct")
    crit = _t("api_error_rate_crit_pct")
    if rate < warn:
        return []
    level = "crit" if rate >= crit else "warn"
    by_status = stats.get("by_status", {})
    top = ", ".join(
        f"{k}×{v}" for k, v in sorted(by_status.items(), key=lambda kv: -kv[1])[:4]
    )
    by_model = stats.get("by_model", {})
    model_top = ", ".join(
        f"{model} {item.get('errors', 0)}/{item.get('total', 0)} "
        f"({float(item.get('error_rate_pct') or 0):.0f}%)"
        for model, item in sorted(
            by_model.items(),
            key=lambda kv: (-(kv[1].get("errors", 0) or 0), kv[0]),
        )[:3]
    )
    threshold = crit if level == "crit" else warn
    model_detail = f"；主要模型：{model_top}" if model_top else ""
    # 瞬時爆發 vs 持續故障：處置完全不同，但比例本身分不出來。整批錯誤擠在
    # 5 分鐘內就明講——2026-08-14 那次 12 筆全在 2 秒內，是本機 DNS 斷線，
    # 面板卻只顯示「26% 錯誤率」，看的人會先去懷疑模型。
    span = stats.get("error_span_sec")
    burst_detail = ""
    if span is not None and span <= 300 and stats.get("errors", 0) >= 3:
        burst_detail = (
            f"。⚡ 全部集中在 {span:.0f} 秒內＝瞬時爆發（網路/DNS 斷線、"
            f"睡眠喚醒空窗的形狀），先看那個時間點的 log，不是模型本身"
        )
    return [{
        "id": "external_api_error_rate",
        "level": level,
        "title": ("Gemini API 錯誤率異常高" if level == "crit"
                  else "Gemini API 錯誤率偏高"),
        "detail": (
            f"最近 {stats.get('window_hours')}h：{stats.get('errors')} 失敗 / "
            f"{total} 次呼叫 = {rate:.0f}%（{level} 門檻 {threshold:.0f}%）。"
            f"主要狀態：{top or '—'}{model_detail}{burst_detail}"
        ),
        "metric": {"rate_pct": rate, "errors": stats.get("errors"),
                   "total": total, "by_status": by_status,
                   "by_model": by_model, "error_span_sec": span},
        "advice": (
            "503/unavailable 多 = Gemini 過載（可暫切 RED_GEMINI_MODEL 到較穩模型）；"
            "429/quota_depleted 多 = 撞配額或預付餘額燒乾（查 billing / Buy credits）；"
            "timeout 多 = 網路或大檔上傳卡住；"
            "network_unreachable 多 = **本機** DNS／網路斷線（睡眠喚醒空窗最常見），"
            "不是 Gemini 的問題，網路恢復後這批會自己滾出視窗。"
        ),
    }]


def _check_embedding_api_health() -> list[dict]:
    """embedding 路徑的錯誤率紅線（跟 `_check_external_api_health` 是兩條）。

    為什麼要獨立一條：embedding 走 `embed_content`、不經 `_gemini_generate`，是
    兩套配額、兩套失敗模式（429 突發 / DSQ 403 / 傳輸逾時）。假警報普查 E 已把
    embedding 的成功從生成路徑的分母剔掉；這條把它們配上自己的分子。

    在此之前 embedding 失敗**完全沒有結構化紀錄** —— 只 print 到 rag_sync log。
    災難級（硬配額燒乾）會中止夜跑、由 daemon 健康紅線接手；但「持續失敗但沒到
    中止門檻」那一段是純盲區，只能事後從覆蓋率（rag_gap_report）反推。

    只 warn/crit 不 page：夜跑失敗的批次隔天靠 content-hash dedup 會續做，不像
    排程寄不出信那樣有人正在等。

    判準是**絕對筆數**（≥`embed_error_min_count` 批重試用盡就 warn），比例只用來
    升 crit。理由見 _DEFAULTS 那段：歷史三週的基線是**零**，在零基線上談百分比
    只會造出永遠不會響的鈴。
    """
    try:
        from agent_core.cost_tracker import embed_error_stats
        stats = embed_error_stats(hours=_t("embed_error_window_hours"))
    except Exception:  # noqa: BLE001 — 監控面自己壞掉不該讓整批 check 掛掉
        return []
    errors = stats.get("errors", 0)
    if errors < _t("embed_error_min_count"):
        return []
    total = stats.get("total", 0)
    rate = stats.get("error_rate_pct", 0.0)
    crit = _t("embed_error_rate_crit_pct")
    level = "crit" if rate >= crit else "warn"
    by_status = stats.get("by_status", {})
    top = ", ".join(f"{k}×{v}" for k, v in
                    sorted(by_status.items(), key=lambda kv: -kv[1])[:4])
    return [{
        "id": "embedding_api_error_rate",
        "level": level,
        "title": ("Embedding 大量批次失敗" if level == "crit"
                  else "Embedding 出現重試用盡的批次"),
        "detail": (
            f"最近 {stats.get('window_hours')}h：{errors} 批重試用盡 / {total} 批 "
            f"= {rate:.0f}%。主要狀態：{top or '—'}。"
            + ("**過半批次失敗**，embedding 幾乎整條掛掉。"
               if level == "crit" else
               f"門檻是絕對筆數 ≥{_t('embed_error_min_count'):.0f} 批 —— "
               "歷史三週基線是零，出現就值得看。")
            + "只算重試用盡才放棄的批次，retry 後成功的不計入。"
        ),
        "metric": {"rate_pct": rate, "errors": errors,
                   "total": total, "by_status": by_status,
                   "min_count": _t("embed_error_min_count")},
        "advice": (
            "429/quota_depleted 多 = embedding 配額或預付餘額（跟生成路徑分開計）；"
            "timeout 多 = 批次太大或網路慢，看 RAG_EMBED_* 批量旋鈕；"
            "network_unreachable 多 = 本機斷網，不是 Gemini 的問題。"
            "失敗的批次隔天夜跑會靠 content-hash dedup 續做，"
            "但連續多天就會在 rag_gap_report 看到覆蓋率掉下去。"
        ),
    }]


def _check_erp_mirror_stale(manifest_path: str | None = None) -> list[dict]:
    """飛越 ERP 本地鏡像新鮮度：manifest 最新 ts >26h warn / >50h crit + 異常表數。

    每日 02:00 的 erp_mirror_refresh 若失敗（SSH 斷、ERP 主機關機、plist env 漏、
    機器睡眠錯過 calendar tick），小紅隔天照用舊資料答訂單/庫存/MRP 數字。manifest
    最新 ts 是端到端訊號——連「launchd 根本沒跑」都涵蓋。manifest 不存在（此機沒部署
    ERP 鏡像，如 CI / dev clone）一律靜默。
    """
    import json

    from agent_core.logging_and_paths import DATA_DIR

    path = manifest_path or os.path.join(DATA_DIR, "erp_mirror", "manifest.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            man = json.load(fh)
        # "_" 開頭是偽條目（如 _item_alias 對照同步狀態，有專屬檢查）：不算表、
        # 也不許墊高 latest（手動補跑對照成功不代表鏡像本體有刷）。
        tables = {k: v for k, v in man.items() if not k.startswith("_")}
        latest = max((v.get("ts") or "" for v in tables.values()), default="")
        bad = sum(1 for v in tables.values()
                  if v.get("status") in ("error", "count_mismatch"))
        # 扣掉睡眠：02:00 的排程在機器睡著時本來就不會觸發，那段不該計入「幾小時沒刷」。
        age_h = (_observed_age_h(
                     datetime.strptime(latest, "%Y-%m-%d %H:%M:%S").timestamp())
                 if latest else float("inf"))
    except Exception:
        return []
    alerts = []
    if age_h > 50:
        level, hint = "crit", "連兩晚沒刷成"
    elif age_h > 26:
        level, hint = "warn", "昨晚 02:00 沒刷成"
    else:
        level = ""
    if level:
        alerts.append({
            "id": "erp_mirror_stale",
            "level": level,
            "title": f"ERP 鏡像 {age_h:.0f}h 沒刷新（{hint}）",
            "detail": (f"erp_mirror manifest 最新條目 {latest or '無'}，已 {age_h:.0f} 小時；"
                       "小紅的 ERP 查詢（訂單/庫存/MRP/生產）正在用過期資料。"),
            "metric": {"age_hours": round(age_h, 1), "latest_ts": latest},
            "advice": ("看 daemon-erp_mirror_refresh.log；測 SSH 到 ERP 主機通不通；"
                       "手動補跑 launchd/scripts/erp_mirror_refresh.py。"),
        })
    if bad:
        alerts.append({
            "id": "erp_mirror_table_errors",
            "level": "warn",
            "title": f"ERP 鏡像 {bad} 張表刷新異常",
            "detail": f"manifest 有 {bad} 張表 status=error/count_mismatch（schema 漂移或 SSH 中斷）。",
            "metric": {"bad_tables": bad},
            "advice": ("看 manifest.json 哪些表 + daemon log 的 ORA- 錯誤；"
                       "疑 schema 漂移就重跑 scripts/erp_schema_probe.py 更新欄位快照。"),
        })
    return alerts


def _check_erp_mirror_slow(manifest_path: str | None = None) -> list[dict]:
    """erp_mirror_refresh 跑批時長：02:00 排程起跑 → manifest 最後落筆 > 門檻即 warn。

    `_check_erp_mirror_stale` 只抓「沒跑成」——只要跑完它就是綠的，不管跑了 1 小時
    還是 3 小時。「跑成功但一路變長」（ERP 主機變慢／SSH 降速／某張表爆量）是自家
    盲區，以前只能靠阿里雲 ECS 那條預設 CPU 告警發信到大王信箱間接察覺（2026-08-01
    的 99.79% 告警就是這麼來的，見 erp_mirror 案史）。這裡把訊號收回自己手上。

    量測：end = 表條目最新 ts（跑批最後落筆）；start = 同日 `_REFRESH_HOUR`:00
    （launchd StartCalendarInterval）。實測基線 61–95 分，看門狗 deadline 120 分。

    ⚠️ 手動補跑不是 02:00 起跑，差值會虛高 → 用 deadline 當上界濾掉：算出來超過
    deadline 的一律靜默，因為真排程輪跑到 deadline 就被 `run_with_deadline`
    os._exit(75) 砍了、不可能留下更晚的 ts。鏡像本體已 stale（>26h）時也靜默——
    root cause 交給 `erp_mirror_stale` 報，同 `_check_erp_item_alias` 慣例。
    """
    import json

    from agent_core.env_utils import env_int
    from agent_core.logging_and_paths import DATA_DIR

    path = manifest_path or os.path.join(DATA_DIR, "erp_mirror", "manifest.json")
    if not os.path.isfile(path):
        return []
    warn_min = env_int("RED_ERP_REFRESH_SLOW_WARN_MIN", 100, min_value=10, max_value=1440)
    hour = env_int("RED_ERP_REFRESH_HOUR", 2, min_value=0, max_value=23)
    deadline_min = env_int(
        "RED_ERP_REFRESH_DEADLINE_S", 7200, min_value=300, max_value=21600) / 60
    try:
        with open(path, encoding="utf-8") as fh:
            man = json.load(fh)
        # 同 _check_erp_mirror_stale：「_」開頭是偽條目，不算表也不許墊高 end。
        tables = {k: v for k, v in man.items() if not k.startswith("_")}
        stamps = []
        for v in tables.values():
            try:
                stamps.append(datetime.strptime(v.get("ts") or "", "%Y-%m-%d %H:%M:%S"))
            except (ValueError, TypeError):
                continue
        if not stamps:
            return []
        end = max(stamps)
        start = end.replace(hour=hour, minute=0, second=0, microsecond=0)
        dur_min = (end - start).total_seconds() / 60
        age_h = _observed_age_h(end.timestamp())   # 同 stale：睡眠不計入
    except Exception:
        return []
    if age_h > 26:
        return []                       # 鏡像本身就 stale：交給 erp_mirror_stale
    if dur_min <= 0 or dur_min > deadline_min:
        return []                       # 非該輪排程（手動補跑）——差值無意義
    if dur_min <= warn_min:
        return []
    # 最慢的表：只認這輪真刷過的（full_ts 落在本輪窗內；預檢跳過的 secs 是上次殘值）。
    slowest, slowest_secs = "", 0.0
    for k, v in tables.items():
        try:
            full = datetime.strptime(v.get("full_ts") or "", "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        secs = float(v.get("secs") or 0)
        if full >= start and secs > slowest_secs:
            slowest, slowest_secs = k, secs
    detail = (f"erp_mirror_refresh 從 {hour:02d}:00 跑到 {end:%H:%M}（{dur_min:.0f} 分），"
              f"超過常態門檻 {warn_min} 分；看門狗 deadline 是 {deadline_min:.0f} 分，"
              "再長就會被砍、隔天才自癒。")
    if slowest:
        detail += f" 本輪最慢的表：{slowest}（{slowest_secs:.0f}s）。"
    return [{
        "id": "erp_mirror_slow",
        "level": "warn",
        "title": f"ERP 鏡像跑批 {dur_min:.0f} 分（偏長）",
        "detail": detail,
        "metric": {"duration_min": round(dur_min, 1), "warn_min": warn_min,
                   "deadline_min": round(deadline_min), "end_ts": f"{end:%Y-%m-%d %H:%M:%S}",
                   "slowest_table": slowest, "slowest_secs": round(slowest_secs, 1)},
        "advice": ("看 daemon-erp_mirror_refresh.log 各表耗時找變慢的那張；"
                   "測 SSH 到 ERP 主機的延遲；ERP 主機同時段 CPU 看阿里雲監控。"
                   "確認是常態成長就調高 RED_ERP_REFRESH_SLOW_WARN_MIN。"),
    }]


# 單頭×明細成對表：可讀視圖靠 JOIN 兩邊才湊成一筆（v_production =
# SF_TRANS_HEADER JOIN SF_TRANS_WK 才有「日期×工單×站別×產量」）。清單成員都必須
# 在 erp_mirror.HOT_TABLES 內——tests/test_dashboard_alerts_erp_pair.py 對照，
# HOT_TABLES 增刪時這份不會靜默過期。
_ERP_PAIR_GROUPS: tuple[tuple[str, ...], ...] = (
    ("MK00.SF_TRANS_HEADER", "MK00.SF_TRANS_WK"),
    ("SC00.SE_ORD_M", "SC00.SE_ORD_ITEM"),
    ("SC00.SE_BOM_M", "SC00.SE_BOM_PART", "SC00.SE_BOM_SIZE"),
    ("SC00.PO_ORDER_M", "SC00.PO_ORDER_D"),
    ("SC00.PO_RCPT_M", "SC00.PO_RCPT_D"),
    ("SC00.PO_PROC_M", "SC00.PO_PROC_D"),
    ("SC00.PO_MRP_M", "SC00.PO_MRP_ITEM"),
    ("SC00.IV_TRANS_M", "SC00.IV_TRANS_D"),
    ("SC00.IV_STOC_M", "SC00.IV_STOC_D"),
    ("SC00.RD_BOM_M", "SC00.RD_BOM_ITEM"),
    ("GL00.AP_APPLY_M", "GL00.AP_APPLY_D"),
    ("GL00.AP_PAY_M", "GL00.AP_PAY_D"),
)


def _check_erp_mirror_pair_skew(manifest_path: str | None = None) -> list[dict]:
    """成對表版本不齊：一半 error 留舊資料、另一半刷成功 → 下游 JOIN 靜默漏整批。

    `erp_mirror_table_errors` 只報「N 張表異常」，讀的人會當成「少一張表、隔晚
    自癒」。但失敗的若是單頭/明細的其中一半，另一半同輪刷成功就讓兩表版本不齊，
    JOIN 掉的是**配不到對面的整批資料**——查詢不會報錯，只會少算或回 0。
    2026-08-15 實例：SF_TRANS_WK（明細，TRANS_QTY 在這）SSH 斷線失敗、
    SF_TRANS_HEADER 同輪成功 → 08-14 的 20 筆過站單頭配不到明細，「昨天各站
    入站產量」回 0（補刷後 667）。整整一天沒人知道數字是錯的。

    判準（兩個條件同時成立才算）：
      ① 同組有成員 status=="error" —— **只認 error**。`count_mismatch` 的資料是
         載入過的（erp_mirror.mirror 先 load 再比對數量，status 在 load 之後才
         算），只是與 COUNT 快照差幾列的 live churn，不會留舊版本、不構成不齊。
      ② 同組有成員 status=="done" 且 full_ts 觀測年齡 ≤26h（真的在最近一輪刷過，
         不是預檢跳過的殘值——預檢跳過只更新 ts、不動 full_ts）。

    兩者缺一即靜默：全組都失敗＝全是舊版本、彼此仍一致；失敗的表旁邊沒有新刷的
    兄弟＝沒有人跑贏它、也不齊不起來。失敗的表隔晚自癒（precheck 只對 done 判
    unchanged）後條件①消失，告警自動收掉。manifest 不存在（CI / dev clone）靜默。
    """
    import json

    from agent_core.logging_and_paths import DATA_DIR

    path = manifest_path or os.path.join(DATA_DIR, "erp_mirror", "manifest.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            man = json.load(fh)
    except Exception:
        return []
    skewed = []
    for group in _ERP_PAIR_GROUPS:
        failed = [k for k in group if (man.get(k) or {}).get("status") == "error"]
        if not failed:
            continue
        fresh = []
        for key in group:
            entry = man.get(key) or {}
            if entry.get("status") != "done":
                continue
            try:
                full = datetime.strptime(entry.get("full_ts") or "",
                                         "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            # 扣掉睡眠 —— 同 _check_erp_mirror_stale 的理由。
            if _observed_age_h(full.timestamp()) <= 26:
                fresh.append(key)
        if fresh:
            skewed.append({"failed": failed, "fresh": fresh})
    if not skewed:
        return []
    lines = "；".join(
        f"{'/'.join(s['failed'])} 失敗但 {'/'.join(s['fresh'])} 已刷新"
        for s in skewed)
    return [{
        "id": "erp_mirror_pair_skew",
        "level": "warn",
        "title": f"ERP 鏡像 {len(skewed)} 組成對表版本不齊（下游查詢正在少算）",
        "detail": (f"{lines}。這些表要 JOIN 才成一筆，版本不齊時 JOIN 會靜默漏掉"
                   "配不到對面的整批資料——查詢不報錯、只會少算或回 0。"),
        "metric": {"skewed_groups": len(skewed), "groups": skewed},
        "advice": ("別等隔晚自癒（中間那一整天的查詢都是錯的）：先看 daemon log 確認"
                   "失敗原因是 SSH 斷線（exit 255、無 ORA-）還是 ORA- 確定性錯誤，"
                   "再定向補刷失敗那張——"
                   "mirror(only_tables=['<OWNER.TABLE>'], force=True)，"
                   "跑之前 ps 確認沒有並行的刷新程序。"),
    }]


def _check_erp_item_alias(db_path: str | None = None,
                          manifest_path: str | None = None) -> list[dict]:
    """庫存編號（舊短碼）對照新鮮度：_item_alias 表空/缺 → warn；同步連續多晚失敗 → warn。

    erp_mirror.sync_item_alias 每日 02:00 隨 refresh_hot 同步 料號→庫存編號
    （SP_ITEM.O_ITEMNO 舊短碼）對照。它失敗只寫 daemon log、不影響 exit code
    （設計如此：加值資料不擋鏡像主流程）——連續失敗多晚沒人會發現，SF24 這類
    舊短碼查詢會悄悄退化成查無。訊號兩路：

      ① 表缺/0 列（read_only 直查 DuckDB；fleet 共用、live 檔設計上永遠只被
         read_only 開）→ 舊短碼查詢「現在就」在退化，立即 warn。
      ② manifest "_item_alias" 偽條目（refresh_hot 每晚落帳）status != done 且距
         上次成功（last_done_ts；從未成功則 first_bad_ts）>74h ≈ 連三晚沒同步成
         → warn。單晚 SSH 瞬斷自癒（見 erp_mirror 深夜斷線案史）不吵。

    鏡像本體已 stale（>26h）時一律靜默——root cause 交給 erp_mirror_stale 報，
    免同一場 SSH 死機疊兩條。DB 不存在（CI / dev clone）靜默。
    """
    import json

    from agent_core.logging_and_paths import DATA_DIR

    d = os.path.join(DATA_DIR, "erp_mirror")
    db = db_path or os.path.join(d, "erp_full.duckdb")
    man_path = manifest_path or os.path.join(d, "manifest.json")
    if not os.path.isfile(db):
        return []
    try:
        with open(man_path, encoding="utf-8") as fh:
            man = json.load(fh)
    except Exception:
        man = {}

    def _age_h(ts: str) -> float | None:
        try:
            # 扣掉睡眠 —— 同 _check_erp_mirror_stale 的理由。
            return _observed_age_h(
                datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp())
        except Exception:
            return None

    latest = max((v.get("ts") or "" for k, v in man.items()
                  if not k.startswith("_")), default="")
    mirror_age = _age_h(latest)
    if mirror_age is not None and mirror_age > 26:
        return []

    try:
        import duckdb
        con = duckdb.connect(db, read_only=True)
        try:
            try:
                rows = con.execute('SELECT COUNT(*) FROM "_item_alias"').fetchone()[0]
            except Exception:
                rows = None          # 表不存在
        finally:
            con.close()
    except Exception:
        return []                    # 打不開（鎖住/壞檔）——鏡像自身的告警會抓

    if not rows:
        return [{
            "id": "erp_item_alias_missing",
            "level": "warn",
            "title": "ERP 庫存編號對照表空缺（舊短碼查詢已退化）",
            "detail": ("鏡像 erp_full.duckdb 的 _item_alias 表"
                       f"{'不存在' if rows is None else '為 0 列'}；SF24/CL14.1 這類"
                       "舊短碼（SP_ITEM.O_ITEMNO）查詢正退化成查無，LLM 可能憑空作答。"),
            "metric": {"rows": rows},
            "advice": ("看 daemon-erp_mirror_refresh.log 搜 sync_item_alias；"
                       "SSH 通後手動補跑 launchd/scripts/erp_mirror_refresh.py。"),
        }]

    entry = man.get("_item_alias") or {}
    status = entry.get("status")
    if status and status != "done":
        anchor = entry.get("last_done_ts") or entry.get("first_bad_ts") or ""
        age_h = _age_h(anchor)
        if age_h is not None and age_h > 74:
            return [{
                "id": "erp_item_alias_stale",
                "level": "warn",
                "title": f"ERP 庫存編號對照連 {age_h / 24:.0f} 天沒同步成",
                "detail": (f"sync_item_alias 最近一次結果 status={status}，距上次成功"
                           f"已 {age_h:.0f} 小時（>74h ≈ 連三晚失敗）。舊對照還在但已"
                           "凍結：新料號解不出 SF24 這類舊短碼、對照逐日過期。"),
                "metric": {"status": status, "age_hours": round(age_h, 1),
                           "anchor_ts": anchor, "rows": rows},
                "advice": ("error=SSH/GG_1001 函式授權問題、empty=解碼輸出異常，"
                           "看 daemon-erp_mirror_refresh.log 搜 sync_item_alias；"
                           "修復後手動補跑 launchd/scripts/erp_mirror_refresh.py。"),
            }]
    return []


def _check_erp_rdp_security(report_path: str | None = None) -> list[dict]:
    """ERP 主機 RDP 安全巡檢旗標（每日 02:00 erp_mirror_refresh 順跑寫入）。

    報告由 agent_core/erp_security_patrol.py 產生：經 ERP SSH 通道統計 Security log
    近 24h 的 4625 失敗登入與 4624 LogonType10 成功登入，白名單/密集門檻在巡檢端
    （erp_mirror_refresh plist env）裁決、旗標直接寫進報告。這裡只讀旗標轉 alert →
    alert_pusher → Telegram（不經 Gemini）。報告內容在巡檢端已逐欄過 sanitize_for_llm
    （帳號名是攻擊者可控字串），這裡可以直接嵌進 detail。

    報告不存在（此機未部署 ERP 巡檢，如 CI / dev clone）或 >48h（巡檢停跑——別拿舊
    事件永久掛警，巡檢本身失敗會在 daemon log 出現）一律靜默。
    """
    import json

    from agent_core.logging_and_paths import DATA_DIR

    path = report_path or os.path.join(DATA_DIR, "erp_security", "rdp_audit_latest.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            rep = json.load(fh)
        flags = rep.get("flags") or {}
        # 扣掉睡眠：巡檢隨 02:00 的 erp_mirror_refresh 跑，機器睡著就不會產新報告；
        # 用原始 age 會在長假後把還有效的資安旗標整批靜音（這條是**漏報**方向，
        # 比其他幾條更該修）。
        age_h = _observed_age_h(
            datetime.fromisoformat(rep.get("generated_at", "")).timestamp())
    except Exception:
        return []
    if age_h > 48:
        return []
    window_h = rep.get("window_hours") or 24
    alerts = []
    unknown = flags.get("success_unknown_ip") or []
    if unknown:
        sample = "；".join(
            f"{e.get('account', '?')}@{e.get('ip', '?')}（{e.get('time', '?')}）"
            for e in unknown[:3])
        alerts.append({
            "id": "erp_rdp_logon_unknown_ip",
            "level": "crit",
            "title": "ERP 主機出現非白名單 RDP 成功登入",
            "detail": (f"近 {window_h}h 有 {len(unknown)} 筆 4624 LogonType10 成功登入來自"
                       f"白名單外 IP：{sample}。若非自己人操作，主機可能已被暴力破解攻陷。"),
            "metric": {"count": len(unknown), "entries": unknown[:5]},
            "advice": ("立刻確認是否本人/同事登入；不是就當已淪陷處理——阿里雲安全群組先收 3389、"
                       "改密碼，全清單見 var/data/erp_security/rdp_audit_latest.json。"
                       "白名單加 IP 用 RED_ERP_RDP_ALLOWED_IPS（erp_mirror_refresh plist）。"),
        })
    dense = flags.get("real_account_bruteforce") or []
    if dense:
        level = flags.get("level_bruteforce")
        sample = "、".join(f"{a.get('account', '?')}×{a.get('n', 0)}" for a in dense[:3])
        alerts.append({
            "id": "erp_rdp_real_account_bruteforce",
            "level": level if level in ("warn", "crit") else "warn",
            "title": "ERP 主機真實帳號正被密集猜密碼",
            "detail": (f"近 {window_h}h Security 4625 中有 {len(dense)} 個「真實存在」的帳號被"
                       f"密集嘗試（SubStatus=密碼錯/鎖定等）：{sample}。攻擊者已摸到正確帳號名，"
                       f"只差密碼。"),
            "metric": {"accounts": dense[:5],
                       "real_fail_total": rep.get("real_fail_total")},
            "advice": ("儘速收掉 3389 公網曝露（安全群組限源）並替被猜帳號改強密碼/開帳號鎖定；"
                       "來源 IP 見報告 top_src_ips / real_account_pairs。"),
        })
    return alerts


def _check_scheduled_tasks() -> list[dict]:
    """排程任務失敗 / 停擺偵測（daemon_tasks.json 的 last_error 與 last_run_at）。

    為什麼補這條（2026-08-06）：dispatcher 執行失敗只把 last_error 寫回檔案就
    結束，**全 repo 沒有任何東西讀它** —— 一支任務可以連續失敗好幾天，log 一片
    綠、red-status 也顯示正常。這批任務裡有一半是直接寄信給同事的（採購晨報、
    生產回報、倉庫通知、匯率推播），壞掉時第一個發現的會是收件人說「我沒收到」。

    分級：**會寄給別人的任務（notify_emails / notify_channel）算 crit**，因為
    有人正在等那封信；純給大王看的排程算 warn。停擺門檻由 scheduler.
    expected_gap_minutes 依「interval × 可觸發時段」推導 —— 09-10 視窗配 60 分
    interval 實際是一天一次，用 interval 當門檻會天天誤報。

    與 _check_daemon_health / _check_daemon_stalls 互補：那兩條看 launchd 程序
    活不活、有沒有 wedge；這條看**排程任務本身**有沒有真的跑出結果。dispatcher
    daemon 完全健康、但某支任務每輪都拋例外，只有這條抓得到。
    """
    try:
        from agent_core.scheduler import all_task_health, _load_daemon_tasks
        health = all_task_health()
        tasks = {str(t.get("name")): t
                 for t in (_load_daemon_tasks().get("tasks") or [])
                 if isinstance(t, dict)}
    except Exception:
        return []

    alerts: list[dict] = []
    for h in health:
        state = h.get("state")
        if state not in ("error", "stalled", "never"):
            continue
        name = h.get("name", "?")
        task = tasks.get(name) or {}
        # 有外部收件人 = 有人在等 → crit；否則 warn。
        outbound = bool(task.get("notify_emails") or task.get("notify_channel"))
        who = ""
        if task.get("notify_emails"):
            who = "、".join(str(a) for a in task["notify_emails"][:3])
        elif task.get("notify_channel"):
            who = f"{task['notify_channel']}/{task.get('notify_agent_color', 'red')}"

        if state == "error":
            title = f"排程任務失敗：{name}"
            detail = f"上次執行拋例外：{h.get('detail')}"
            advice = (f"看 var/logs/daemon-dispatcher.log 搜「{name}」找例外堆疊；"
                      f"修好後跟小紅說「立刻執行排程 {name}」補一輪。")
        elif state == "stalled":
            title = f"排程任務停擺：{name}"
            detail = h.get("detail", "")
            advice = ("dispatcher 沒觸發它 —— 查 enabled 是否被關、start_hour/"
                      "end_hour 視窗是否設錯（視窗外永不觸發），以及 "
                      "com.xiaohong.dispatcher 是否還在跑。")
        else:  # never
            title = f"排程任務從未執行：{name}"
            detail = h.get("detail", "")
            advice = ("建立後一直沒被觸發 —— 多半是 start_hour/end_hour 視窗設錯"
                      "或 enabled=false。")

        if outbound and who:
            detail += f"（這支會寄給 {who}，對方正在等）"
        alerts.append({
            "id": f"scheduled_task_{state}_{name}",
            "level": "crit" if outbound else "warn",
            "title": title,
            "detail": detail,
            "metric": {"task": name, "state": state,
                       "age_min": round(float(h.get("age_min", 0)), 1),
                       "threshold_min": round(float(h.get("threshold_min", 0)), 1),
                       "outbound": outbound},
            "advice": advice,
        })
    return alerts


def _check_dependency_drift() -> list[dict]:
    """venv 實裝版本與 requirements 釘版不符 —— 「改了釘版但沒 pip install」。

    2026-08-12 一天內踩到兩次（pypdf 以為升好其實沒升；ruff 因為 requirements.txt
    不含 -dev 而停在舊版，而 ruff 版本決定 lint 規則集、差一版差 4,014 個錯），
    當時沒有任何東西在檢查。首次上線即抓到既有漂移（onnxruntime 釘 1.28.0、
    實裝 1.27.0）。

    只 warn 不 crit：漂移代表「這台機器跑的不是你以為的版本」，要人來看，但不像
    排程寄不出去那樣有人正在等。沒安裝的不算漂移（選配依賴），見 deps_check。

    涵蓋主 .venv 與各專用 venv（`var/venvs/*`，靠 requirements 檔頭的
    `deps-check: separate-venv=<名>` 指出來）。兩者修法不同，所以 advice 依實際
    漂到的 venv 給對應指令 —— 對 box_ocr 叫人 `pip install -r requirements.txt`
    是錯的建議。
    """
    try:
        from agent_core.deps_check import MAIN_VENV, check_dependency_drift, fix_hint
        drift = check_dependency_drift()
    except Exception:  # noqa: BLE001 — 監控面自己壞掉不該讓整批 check 掛掉
        return []
    if not drift:
        return []
    venvs = sorted({d.get("venv") or MAIN_VENV for d in drift},
                   key=lambda v: (v != MAIN_VENV, v))
    parts = []
    for d in drift[:4]:
        venv = d.get("venv") or MAIN_VENV
        suffix = "" if venv == MAIN_VENV else f"（{venv}）"
        parts.append(f"{d['name']} {d['op']}{d['wanted']}→實裝 {d['installed']}{suffix}")
    worst = ", ".join(parts)
    fixes = "；".join(f"{'主 venv' if v == MAIN_VENV else v}：`{fix_hint(v)}`" for v in venvs)
    return [{
        "id": "dependency_drift",
        "level": "warn",
        "title": f"venv 與 requirements 不符（{len(drift)} 個套件）",
        "detail": f"{worst}{'…' if len(drift) > 4 else ''}",
        "metric": {"count": len(drift),
                   "venvs": venvs,
                   "packages": [d["name"] for d in drift[:10]]},
        "advice": (f"多半是改了釘版但忘了裝。{fixes}。裝完 make test-quiet 再 "
                   "bin/redeploy-daemons --force 讓艦隊載到新版。"),
    }]


_ALL_CHECKS = [
    _check_cost,
    _check_monthly_cap,
    _check_runs_errors,
    _check_error_log,
    _check_daemon_health,
    _check_email_ingest_stale,
    _check_daemon_stalls,
    _check_scheduled_tasks,
    _check_dependency_drift,
    _check_chroma_mode_consistency,
    _check_external_api_health,
    _check_embedding_api_health,
    _check_erp_mirror_stale,
    _check_erp_mirror_slow,
    _check_erp_mirror_pair_skew,
    _check_erp_item_alias,
    _check_erp_rdp_security,
]


def check_alerts() -> list[dict]:
    """跑全部 check，回 list of triggered alerts。失敗的 check 自動 skip。

    測試隔離 kill-switch：設 `RED_DISABLE_HEALTH_ALERTS=1` 時一律回空 list（連
    個別 check 都不跑）。用途是讓只想驗 RPC/worker plumbing 的 E2E 測試在子程序
    裡不依賴 live fleet 健康 —— 那些 check 會讀主 checkout 的 live var/
    （cost.jsonl / api_errors.jsonl 算 Gemini 錯誤率）與 launchctl，配額耗盡時
    回 crit 害測試 flaky（見 tests/test_tool_rpc_worker.py）。預設關閉。

    ⚠️ 別在正式 alert_check daemon 的環境設這個：alert_pusher 看到 alert 從
    清單消失會發「已恢復」通知並停止追蹤，等於致盲監控，不是單純靜音。
    """
    from agent_core.env_utils import env_bool
    if env_bool("RED_DISABLE_HEALTH_ALERTS"):
        return []
    # 打一顆「此刻我們在線」—— age-based 判準靠這條時間軸扣掉睡眠/停擺的空窗。
    # 放在 kill-switch 之後：告警關掉時我們本來就沒在觀測，不該記成有。
    try:
        from agent_core.awake_clock import record_tick
        record_tick()
    except Exception:  # noqa: BLE001 - 簿記失敗不影響告警
        pass
    out = []
    for fn in _ALL_CHECKS:
        try:
            out.extend(fn())
        except Exception:
            # 個別 check 出錯不影響整體
            continue
    return out


def system_alerts(min_level: str = "warn") -> str:
    """🚨 RED 警示總覽：列今天觸發的 alert，附建議動作。

    Args:
        min_level: 最低顯示級別，'warn' 或 'crit'。crit 表示只看立刻處理的。

    Returns:
        formatted alert list；無 alert 時回「目前一切正常 ✅」。
    """
    if isinstance(min_level, dict):
        min_level = min_level.get("min_level") or min_level.get("level") or "warn"
    elif isinstance(min_level, (list, tuple, set)):
        min_level = next((str(x) for x in min_level if x), "warn")
    elif min_level is None:
        min_level = "warn"
    else:
        min_level = str(min_level)
    min_level = min_level.strip().lower()

    alerts = check_alerts()
    if min_level == "crit":
        alerts = [a for a in alerts if a.get("level") == "crit"]
    if not alerts:
        return f"✅ 目前無 {min_level}-level alert（{datetime.now().strftime('%H:%M:%S')}）"

    crit_count = sum(1 for a in alerts if a.get("level") == "crit")
    warn_count = sum(1 for a in alerts if a.get("level") == "warn")

    lines = [
        f"🚨 RED 警示  @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"   crit: {crit_count}  warn: {warn_count}",
        "─" * 60,
    ]
    # crit 優先
    alerts.sort(key=lambda a: (a.get("level") != "crit", a.get("id")))
    for a in alerts:
        icon = "🔴" if a.get("level") == "crit" else "🟡"
        lines.append(f"{icon} [{a['level']}] {a['title']}")
        lines.append(f"   詳情：{a['detail']}")
        if a.get("advice"):
            lines.append(f"   建議：{a['advice']}")
        lines.append("")
    lines.append("─" * 60)
    lines.append("💡 用 `system_alerts('crit')` 只看立刻要處理的")
    lines.append("💡 thresholds 可用 `RED_ALERT_<KEY>=value` env var 覆蓋")
    return "\n".join(lines)
