"""生產排程：從福群鞋廠生管日報算出排程狀態、落後示警、查詢、看板。

以「生管型體」為核心（生管型體=EG2305V…，跟業務訂單型體 74L 是兩套不同編碼，
要接訂單那層另需 74L↔EG 對照——見 project_erp_automation）。資料來源＝
`factory_production_report`（生管日報 xlsx），複用其 `iter_production_rows`
解析咽喉，這裡只做「排程視角」的彙整。

LLM 工具（給小紅）：
- `production_alert`            落後／即將到期示警（也給每日主動推播用）
- `query_production_schedule`  依客戶／指令／型體查排程進度
- `production_dashboard`       各客戶 在產／未完／落後 一覽看板
"""
from __future__ import annotations

import datetime
import re
import time
from typing import Any

from agent_core import factory_production_report as _fpr

_NEAR_DAYS = 7
_CACHE_TTL_S = 300  # 生管日報每日更新，5 分內重複呼叫共用同一份下載＋同一份解析結果
# bytes 快取（TTL）＋schedule 解析快取（鍵=檔 id+modifiedTime；一輪對話 3 個工具
# 各自全簿 openpyxl 解析 180 萬 cell 太貴，解析結果跟著 bytes 一起快取）。
_CACHE: dict[str, Any] = {"bytes": None, "name": "", "modified": "", "ts": 0.0,
                          "key": "", "schedule": None}


def _to_date(v: Any) -> datetime.date | None:
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%Y.%m.%d"):
            try:
                return datetime.datetime.strptime(v.strip()[:10], fmt).date()
            except ValueError:
                continue
    return None


# 業務訂單型體（客戶型體／Supremo）長相：兩碼數字 + 一字母 + 一串數字，如 74L130300300。
_SUPREMO_RE = re.compile(r"^\d{2}[A-Z]\d{5,}")


def _looks_supremo(code: str) -> bool:
    """像不像業務訂單型體（74L/63L/36L…），用來決定要不要走客戶型體→生管型體對照。"""
    return bool(_SUPREMO_RE.match(str(code or "").strip().upper()))


def _norm_style(code: str) -> str:
    """客戶型體正規化：抓開頭「數字數字+字母+數字串」核心、去顏色/備註後綴。
    例 '63L1073004-AU(S)'→'63L1073004'、'63L107300400'→'63L107300400'。"""
    m = re.match(r"(\d{2}[A-Z]\d+)", str(code or "").upper().replace(" ", ""))
    return m.group(1) if m else ""


def _resolve_supremo(code: str, schedule: list[dict[str, Any]]) -> list[str]:
    """業務訂單型體(客戶型體/Supremo) → 生管型體清單，用生管日報「客戶型體」欄對照。

    前綴比對容忍尾碼長度差（業務 '63L107300400' ↔ 報表 '63L1073004-AU'，正規化後
    取較短者為前綴比對、且核心至少 9 碼避免亂配）。回去重後的生管型體 model 字串。
    """
    q = _norm_style(code)
    if len(q) < 9:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for s in schedule:
        rnorm = _norm_style(s.get("cust_style", ""))
        if len(rnorm) < 9:
            continue
        if q.startswith(rnorm) or rnorm.startswith(q):
            m = (s.get("model") or "").strip()
            if m and m not in seen:
                seen.add(m)
                out.append(m)
    return out


def _get_report() -> tuple[bytes, str, str]:
    """下載最新生管日報 → (bytes, name, modified)，含 5 分鐘快取。

    也給 factory_production_report._load_latest_sheet 共用（同一輪對話裡
    chart_* 工具與排程工具共用同一份下載）。找不到檔丟 RuntimeError。
    """
    now = time.time()
    if _CACHE["bytes"] is not None and now - _CACHE["ts"] < _CACHE_TTL_S:
        return _CACHE["bytes"], _CACHE["name"], _CACHE["modified"]
    fid, name = _fpr._find_latest_progress_file()
    if not fid:
        raise RuntimeError("找不到任何『生產日報進度表』檔。請確認 Drive 上有該檔，或傳 file_id。")
    xlsx, meta = _fpr._download_xlsx_bytes(fid)
    key = f"{fid}:{meta.get('modifiedTime', '')}"
    if key != _CACHE["key"]:
        _CACHE["schedule"] = None  # 檔案變了 → 解析快取失效
    _CACHE.update(bytes=xlsx, name=name or str(meta.get("name", "")),
                  modified=str(meta.get("modifiedTime", ""))[:10], ts=now, key=key)
    return _CACHE["bytes"], _CACHE["name"], _CACHE["modified"]


def _get_report_bytes() -> tuple[bytes, str]:
    """下載最新生管日報 bytes（含 5 分鐘快取，免一輪多工具重複下載）。"""
    xlsx, name, _modified = _get_report()
    return xlsx, name


def _load_schedule() -> tuple[list[dict[str, Any]], str]:
    """生管日報 → 每個(客戶,指令,型體)的排程狀態。

    回 (schedule, report_name)。schedule item:
      customer, work_order(指令), model(型體),
      want_ship(date|None 希望出貨日), pack_rem(未完), pack_cum(已包裝累計),
      pairs(在表雙數)
    未完/已包裝/希望出貨日取「最後生產日」那筆（累計快照）——希望出貨日改期後
    只有最新分頁是對的，取最早分頁會拿舊交期算落後天數。
    解析結果與 bytes 同一份 TTL 快取（同一份檔重複呼叫不重解 180 萬 cell）。
    """
    xlsx, name = _get_report_bytes()
    if _CACHE["schedule"] is not None and _CACHE["bytes"] is xlsx:
        return list(_CACHE["schedule"]), name
    agg: dict[tuple, dict] = {}
    for r in _fpr.iter_production_rows(xlsx):
        wo = (r.get("work_order") or "").strip()
        if not wo:
            continue
        key = (str(r.get("customer", "")).strip(), wo, str(r.get("model", "")).strip())
        a = agg.setdefault(key, {"want": None, "_want_fb": None, "rem": None, "cum": None,
                                 "pairs": 0, "_maxday": -1, "cust_style": ""})
        if not a["cust_style"] and r.get("cust_style"):
            a["cust_style"] = str(r["cust_style"]).strip()
        if a["_want_fb"] is None and r.get("want_ship") is not None:
            # fallback：任何列的第一個非空日期。若日期只出現在沒有欠數快照的列，
            # 沒有 fallback 會從「舊日期」變「沒日期」→ 掉出逾期清單（更糟的漏報）。
            a["_want_fb"] = _to_date(r["want_ship"])
        day = r.get("prod_day", 0) or 0
        if r.get("pack_rem") is not None and day >= a["_maxday"]:
            a["rem"] = r["pack_rem"]
            a["cum"] = r.get("pack_cum")
            a["pairs"] = r.get("pairs") or 0
            # 希望出貨日與未完快照同步、取最大生產日那筆（改期後才不會用舊交期
            # 算落後）；該筆 want_ship 空值時保留既有值（部分列只在較早分頁填日期）。
            w = _to_date(r["want_ship"]) if r.get("want_ship") is not None else None
            if w is not None:
                a["want"] = w
            a["_maxday"] = day
    schedule = [
        {"customer": c, "work_order": wo, "model": m, "cust_style": a["cust_style"],
         "want_ship": a["want"] if a["want"] is not None else a["_want_fb"],
         "pack_rem": a["rem"] or 0, "pack_cum": a["cum"] or 0,
         "pairs": a["pairs"] or 0}
        for (c, wo, m), a in agg.items()
    ]
    if _CACHE["bytes"] is xlsx:
        _CACHE["schedule"] = schedule
    return list(schedule), name


_REM_SUSPECT_MIN = 10


def _rem_all_zero_warning(schedule: list[dict]) -> str:
    """欠數欄整欄解析失敗的護欄，回警示字串（空字串=無可疑）。

    版型位移讓 pack_rem 解不到時，_to_num 會把整欄靜默變 0 → 落後/到期判定
    假性全綠。「有雙數的指令夠多、未完卻全為 0」幾乎不可能是真的全做完，
    標可疑請人工複核（不拒答，數字仍照列）。
    """
    with_pairs = [s for s in schedule if (s.get("pairs") or 0) > 0]
    if len(with_pairs) >= _REM_SUSPECT_MIN and all(not (s.get("pack_rem") or 0) for s in with_pairs):
        return (f"⚠️ 資料可疑：{len(with_pairs)} 筆有雙數的指令「未完(欠數)」全為 0 —— "
                "可能是生管日報版型位移、欠數欄沒解析到（落後/到期判定會假性全綠），請人工複核原表。")
    return ""


def _classify(schedule: list[dict], *, today: datetime.date | None = None,
              near_days: int = _NEAR_DAYS) -> tuple[list[dict], list[dict]]:
    """分 overdue（應出貨日已過、未完>0）/ near（near_days 內到期、未完>0）。
    各 item 加 days_late（>0=已落後天數；<=0=距到期天數負值）。"""
    today = today or datetime.date.today()
    overdue: list[dict] = []
    near: list[dict] = []
    for s in schedule:
        rem = s["pack_rem"]
        w = s["want_ship"]
        if not w or rem <= 0:
            continue
        d = (today - w).days
        item = {**s, "days_late": d}
        if d > 0:
            overdue.append(item)
        elif d >= -near_days:
            near.append(item)
    overdue.sort(key=lambda x: -x["days_late"])
    near.sort(key=lambda x: x["days_late"])
    return overdue, near


def _by_customer(items: list[dict]) -> list[tuple[str, int, int]]:
    """彙總每客戶 (客戶, 筆數, 未完合計)，依未完降冪。"""
    agg: dict[str, list[int]] = {}
    for it in items:
        a = agg.setdefault(it["customer"], [0, 0])
        a[0] += 1
        a[1] += int(it["pack_rem"])
    return sorted(((c, n, q) for c, (n, q) in agg.items()), key=lambda x: -x[2])


def production_alert(near_days: int = 7, top: int = 15) -> str:
    """生產落後示警：哪些生產指令「應出貨日已過還沒做完」或「即將到期」。

    從福群鞋廠生管日報自動算（以生管型體／指令為單位）。被問「什麼落後了」、
    「哪些快到期」、「生產有沒有開天窗」時先呼叫這個；也用於每日主動推播。

    near_days: 「即將到期」往後幾天內算（預設 7）
    top:       落後明細最多列幾筆（預設 15；0=全列）

    回傳：markdown —— 落後各客戶筆數/未完彙總 + 最緊明細 + 即將到期清單。
    """
    try:
        schedule, name = _load_schedule()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀生管日報失敗：{type(exc).__name__}: {exc}"
    if not schedule:
        return "讀不到生產資料（生管日報還沒更新或格式變動）。"

    overdue, near = _classify(schedule, near_days=near_days)
    lines = [f"📅 生產排程示警（{name}）"]
    suspect = _rem_all_zero_warning(schedule)
    if suspect:
        lines.append(suspect)
    if not overdue and not near:
        lines.append("✅ 沒有落後、近 %d 天也沒有即將到期的未完指令。" % near_days)
        return "\n".join(lines)

    if overdue:
        tot = sum(int(i["pack_rem"]) for i in overdue)
        lines.append(f"\n🔴 落後（應出貨日已過、還有未完）：{len(overdue)} 筆 / 未完 {tot:,} 雙")
        for c, n, q in _by_customer(overdue):
            lines.append(f"   {c}：{n} 筆，未完 {q:,} 雙")
        shown = overdue if top <= 0 else overdue[:top]
        lines.append(f"   最緊{'' if top<=0 else f'（前 {len(shown)}）'}：")
        for it in shown:
            lines.append(
                f"     落後{it['days_late']:>3}天  {it['customer']:<8} {it['work_order']} "
                f"{it['model'][:18]:<18} 未完 {int(it['pack_rem'])}")
    if near:
        lines.append(f"\n🟡 即將到期（{near_days} 天內、未完）：{len(near)} 筆")
        for it in near[:top or len(near)]:
            lines.append(
                f"     {-it['days_late']}天後  {it['customer']:<8} {it['work_order']} "
                f"{it['model'][:18]:<18} 未完 {int(it['pack_rem'])}")
    return "\n".join(lines)


def query_production_schedule(customer: str = "", work_order: str = "",
                              model: str = "", overdue_only: bool = False) -> str:
    """查生產排程進度：依客戶／指令／型體過濾，看未完、已包裝、應出貨日、是否落後。

    被問「RICHTER 生產到哪了」、「JFC26366 做完沒」、「EG2305V 還差多少」、
    「LURCHI 有哪些落後」、「74L130300300 何時上線生產」時呼叫。型體欄**兩種都吃**：
    生管型體（EG2305V…）或業務訂單上的客戶型體（Supremo 74L/63L/36L…）——後者會
    自動用生管日報「客戶型體」欄對照成生管型體再查（免先問業務）。

    customer:     客戶關鍵字（LURCHI/RICHTER/DECA/JALAS…，模糊比對）
    work_order:   指令號關鍵字（JFC26366…）
    model:        生管型體（EG2305V…）或 客戶型體/Supremo 業務訂單型體（自動辨識+對照）
    overdue_only: True 只回已落後的

    回傳：markdown 清單，每筆含 客戶/指令/型體/未完/已包裝/應出貨日/落後天數。
    """
    try:
        schedule, name = _load_schedule()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀生管日報失敗：{type(exc).__name__}: {exc}"
    if not schedule:
        return "讀不到生產資料（生管日報還沒更新或格式變動）。"

    today = datetime.date.today()
    cu, wo, mo = customer.strip().upper(), work_order.strip().upper(), model.strip().upper()

    # 型體欄若是客戶型體（Supremo 業務訂單型體 74L/63L…），先用生管日報對照成生管型體。
    xwalk_note = ""
    resolved_models: set[str] | None = None
    if model and _looks_supremo(model):
        rm = _resolve_supremo(model, schedule)
        if not rm:
            return (f"客戶型體（業務訂單型體）「{model}」目前不在生管日報裡——"
                    "代表還沒排進生產／還沒開生產指令，所以還查不到上線排程。")
        resolved_models = set(rm)
        xwalk_note = (f"　🔗 客戶型體 {model} → 生管型體 "
                      + "、".join(sorted({m[:16] for m in rm})))

    hits = []
    for s in schedule:
        if cu and cu not in str(s["customer"]).upper():
            continue
        if wo and wo not in str(s["work_order"]).upper():
            continue
        if resolved_models is not None:
            if s["model"] not in resolved_models:
                continue
        elif mo and mo not in str(s["model"]).upper():
            continue
        w = s["want_ship"]
        late = (today - w).days if w else None
        if overdue_only and not (late and late > 0 and s["pack_rem"] > 0):
            continue
        hits.append({**s, "days_late": late})
    if not hits:
        scope = " / ".join(x for x in [customer, work_order, model] if x) or "（全部）"
        return f"沒找到符合的生產指令：{scope}{'（限已落後）' if overdue_only else ''}。"

    # 落後的排前面、再依應出貨日
    hits.sort(key=lambda x: (-(x["days_late"] or -9999) if x["pack_rem"] > 0 else 9999,
                             x["want_ship"] or datetime.date.max))
    tot_rem = sum(int(h["pack_rem"]) for h in hits)
    lines = [f"📋 生產排程查詢（{name}）：{len(hits)} 筆，未完合計 {tot_rem:,} 雙"]
    if xwalk_note:
        lines.append(xwalk_note)
    for h in hits[:40]:
        w = h["want_ship"]
        flag = ""
        if w and h["days_late"] and h["days_late"] > 0 and h["pack_rem"] > 0:
            flag = f" 🔴落後{h['days_late']}天"
        elif h["pack_rem"] <= 0:
            flag = " ✅已完"
        lines.append(
            f"  {h['customer']:<8} {h['work_order']} {h['model'][:18]:<18} "
            f"未完 {int(h['pack_rem']):>4} ｜已包裝 {int(h['pack_cum']):>4} "
            f"｜應出 {w or '—'}{flag}")
    if len(hits) > 40:
        lines.append(f"  …（還有 {len(hits) - 40} 筆，請縮小範圍）")
    return "\n".join(lines)


def production_dashboard() -> str:
    """生產排程看板：各客戶的 在產(未完)／已包裝／落後筆數／最緊到期 一覽。

    被問「整體生產排程如何」、「各客戶趕不趕得上」、「生產看板」時呼叫。
    回傳：markdown 每客戶一行的彙總。
    """
    try:
        schedule, name = _load_schedule()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀生管日報失敗：{type(exc).__name__}: {exc}"
    if not schedule:
        return "讀不到生產資料（生管日報還沒更新或格式變動）。"

    today = datetime.date.today()
    overdue, near = _classify(schedule)
    od_by = {c: (n, q) for c, n, q in _by_customer(overdue)}
    nr_by: dict[str, int] = {}
    for it in near:
        nr_by[it["customer"]] = nr_by.get(it["customer"], 0) + 1

    cust: dict[str, list] = {}
    for s in schedule:
        a = cust.setdefault(s["customer"], [0, 0, None])  # 未完, 已包裝, 最緊到期
        a[0] += int(s["pack_rem"])
        a[1] += int(s["pack_cum"])
        if s["pack_rem"] > 0 and s["want_ship"]:
            a[2] = min(a[2], s["want_ship"]) if a[2] else s["want_ship"]

    lines = [f"📊 生產排程看板（{name}）｜today={today}"]
    suspect = _rem_all_zero_warning(schedule)
    if suspect:
        lines.append(suspect)
    for c in sorted(cust, key=lambda x: -cust[x][0]):
        rem, cum, soonest = cust[c]
        od_n, od_q = od_by.get(c, (0, 0))
        nr_n = nr_by.get(c, 0)
        warn = ""
        if od_n:
            warn += f" 🔴落後{od_n}筆/{od_q:,}雙"
        if nr_n:
            warn += f" 🟡快到期{nr_n}筆"
        lines.append(
            f"  {c:<9} 未完 {rem:>6,}｜已包裝 {cum:>6,}｜最緊到期 {soonest or '—'}{warn}")
    return "\n".join(lines)


def production_overdue_bom(customer: str = "", top: int = 15) -> str:
    """落後生產指令逐一帶出「有沒有料表(BOM)」＋ 料表來源／料數。

    串生產排程(落後判定) × factory_bom(料表查詢)：被問「哪些落後單有/沒有料表」「落後
    這些有沒有 BOM 可查料、找供應商」「落後單缺不缺料表」時呼叫。以(客戶,型體)去重、依
    未完降冪查最多 top 個不同型體（避免逐單重複下載 Drive），其餘標「未查」。

    ⚠️ 會即時查 Drive（DECA/JALAS/LURCHI 只搜檔名很快；RICHTER 要下載確認、較慢）。
    型體多時請用 customer 縮小（如 LURCHI / RICHTER）。

    customer: 客戶關鍵字（LURCHI/RICHTER…），留空=全部落後
    top:      最多查幾個不同型體（預設 15）

    回傳：markdown — 每型體 ✅有料表(來源/料數) / ❌沒料表 / ⚠️非支援 ＋ 落後單數/未完。
    """
    try:
        schedule, name = _load_schedule()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 讀生管日報失敗：{type(exc).__name__}: {exc}"
    if not schedule:
        return "讀不到生產資料（生管日報還沒更新或格式變動）。"

    overdue, _near = _classify(schedule)
    cu = customer.strip().upper()
    if cu:
        overdue = [x for x in overdue if cu in str(x["customer"]).upper()]
    if not overdue:
        return f"沒有落後的生產指令{('（' + customer + '）') if customer else ''}。"

    # (客戶, 型體核心) 去重彙總：未完合計、落後單數、代表 model（同型體只查一次料表）
    groups: dict[tuple, dict] = {}
    for x in overdue:
        core = (x["model"] or "").split()[0] or "?"
        g = groups.setdefault((x["customer"], core),
                              {"model": x["model"], "rem": 0, "orders": 0})
        g["rem"] += int(x["pack_rem"])
        g["orders"] += 1
    ranked = sorted(groups.items(), key=lambda kv: -kv[1]["rem"])
    shown = ranked[: max(1, top)]

    from agent_core import factory_bom as _fb
    lines = [f"📋 落後生產指令 × 料表(BOM) 狀態（{name}）："
             f"{len(overdue)} 落後單 / {len(groups)} 型體"
             + (f"／客戶 {customer}" if customer else "")]
    yes = no = unsup = 0
    for (cust, core), g in shown:
        status, src, n = _fb.bom_probe(g["model"])
        if status == "yes":
            yes += 1
            tail = "✅ 有料表" + (f"（{n} 料）" if n else "") + (f" ← {src[:38]}" if src else "")
        elif status == "no":
            no += 1
            tail = "❌ 沒料表（CBD 未建/找不到）"
        else:
            unsup += 1
            tail = "⚠️ 非支援客戶（BOM 在 ERP）"
        lines.append(
            f"  {cust:<8} {core[:18]:<18} 落後 {g['orders']:>2} 單／未完 {g['rem']:>5,} ｜ {tail}")
    if len(ranked) > len(shown):
        lines.append(f"  …（還有 {len(ranked) - len(shown)} 個型體未查，請用 customer 縮小範圍）")
    lines.append(
        f"\n小計：✅{yes} 有料表、❌{no} 沒料表" + (f"、⚠️{unsup} 非支援" if unsup else "")
        + "。有料表者可用 get_model_bom／check_material_readiness 進一步查料與備料。")
    return "\n".join(lines)
