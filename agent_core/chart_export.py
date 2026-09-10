"""圖表產生 — 把小紅整理好的資料畫成圖（甘特圖 / 長條圖 / 折線圖…）並直接傳給大王。

動機：大王在 Telegram 說「把每天的生產做成甘特圖」「畫個效率圖給我」時，小紅以前
只能走 run_python_code。但那個沙箱是「算數 + print 文字」用途，刻意禁掉 `import`、
`.savefig`、`open(` —— 永遠存不出圖片檔，所以圖一直畫不出來，還要大王連打兩道
危險確認（run_python_code 是 DANGEROUS 級）。

這個模組比照 doc_export.export_report 的範式，補上整段：

  1. 一個彈性的「圖表規格」(chart spec)：type + 標題 + 各型別專屬資料欄位。
  2. 一個 matplotlib renderer（內部直接 savefig，沙箱禁詞不適用 —— 因為這是
     小紅自己的程式碼、不是 LLM 產的任意 code）。支援繁體中文（嵌系統 CJK 字型）。
  3. generate_chart() tool：畫圖 → 存 var/data/exports/ → 主動用 telegram_send_photo
     送到大王 Telegram（inline 預覽，best-effort）→ 回 ToolResult.artifacts。

因為 generate_chart 不是任意 code exec（吃的是結構化資料、不執行使用者程式碼），
它落在 SAFE tier —— **不需要 +確認 / +雙確認**，跟 export_report 一樣免摩擦。

輸入刻意做得寬鬆（LLM 友善），spec_json 可以用各種型別別名（"甘特圖" / "gantt" /
"排程圖" 都認），資料欄位也接受多種寫法。

matplotlib 在 renderer 內 lazy import —— 匯入本模組很便宜，缺套件只在實際畫圖時
報清楚的錯。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from agent_core.logging_and_paths import EXPORTS_DIR, logger
from agent_core.tool_result import ErrorCode, ToolResult

# macOS 上可嵌的繁中字型（依序 fallback；shell_python_web 的沙箱也用 Arial Unicode MS）。
# 不嵌中文字型 → 圖上中文全變空格/方框，連數字有時都消失（見 reportlab CJK 教訓）。
_CJK_FONTS = [
    "Arial Unicode MS", "PingFang TC", "PingFang SC", "Heiti TC",
    "STHeiti", "Songti SC", "Hiragino Sans GB", "Apple LiGothic",
]

# 配色沿用 doc_export 的房子風格基調，外加一條好看的分類色盤（tab10 太飽和時用）。
_MUTED = "#7F8C8D"

# ── 型別別名（LLM 可能用各種說法）─────────────────────────────────────
_TYPE_ALIASES = {
    # 甘特 / 排程
    "gantt": "gantt", "甘特": "gantt", "甘特圖": "gantt", "排程圖": "gantt",
    "時程圖": "gantt", "進度圖": "gantt", "schedule": "gantt", "timeline": "gantt",
    # 長條（群組是預設）
    "bar": "grouped_bar", "長條圖": "grouped_bar", "柱狀圖": "grouped_bar",
    "column": "grouped_bar", "bar_chart": "grouped_bar",
    "grouped_bar": "grouped_bar", "grouped": "grouped_bar",
    "群組長條圖": "grouped_bar", "群組柱狀圖": "grouped_bar", "clustered": "grouped_bar",
    # 堆疊長條
    "stacked_bar": "stacked_bar", "stacked": "stacked_bar", "堆疊": "stacked_bar",
    "堆疊長條圖": "stacked_bar", "堆疊柱狀圖": "stacked_bar", "stacked_column": "stacked_bar",
    # 折線
    "line": "line", "折線圖": "line", "趨勢圖": "line", "line_chart": "line",
    "曲線圖": "line",
    # 圓餅
    "pie": "pie", "圓餅圖": "pie", "派圖": "pie", "餅圖": "pie", "pie_chart": "pie",
    # 水平長條（適合「各項目排名 / 進度 / 達成率」——分類名稱長時比直條好讀）
    "barh": "barh", "橫條圖": "barh", "水平長條圖": "barh", "水平長條": "barh",
    "horizontal_bar": "barh", "進度條": "barh", "達成率圖": "barh", "排名圖": "barh",
}

# 甘特圖日期解析支援的格式（無年份的會落在 1900 年，僅作相對排程顯示用）。
_DATE_FMTS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
    "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M",
    "%m-%d", "%m/%d", "%m.%d",
)

_SECONDARY_TOKENS = {"secondary", "second", "right", "副", "右", "次", "2", "二"}


# ────────────────────────────────────────────────────────────────────
# Spec 解析 helpers
# ────────────────────────────────────────────────────────────────────
def _normalize_type(raw: Any) -> str | None:
    if not raw:
        return None
    key = str(raw).strip().lower()
    return _TYPE_ALIASES.get(key)


def _parse_axis_value(raw: Any) -> tuple[datetime | None, float | None]:
    """把甘特圖的 start/end 值解析成 (datetime, None) 或 (None, float)。
    都解析不出來回 (None, None)。"""
    if isinstance(raw, bool):  # bool 是 int 子類，先排除
        return None, None
    if isinstance(raw, (int, float)):
        return None, float(raw)
    s = str(raw or "").strip()
    if not s:
        return None, None
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt), None
        except ValueError:
            continue
    try:
        return None, float(s)
    except ValueError:
        return None, None


def _coerce_floats(values: Any, *, where: str) -> list[float]:
    """把一串值轉成 float list；遇到非數字丟帶上下文的 ValueError。"""
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{where} 必須是數字陣列，收到 {type(values).__name__}")
    out: list[float] = []
    for i, v in enumerate(values):
        if isinstance(v, bool) or v is None:
            raise ValueError(f"{where}[{i}] 不是數字：{v!r}")
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            raise ValueError(f"{where}[{i}] 不是數字：{v!r}")
    return out


def _normalize_series(spec: dict) -> list[dict]:
    """把 spec 的 series 統一成 [{name, values, color, secondary, kind}]。

    接受兩種寫法：
      • 完整：'series': [{'name':'迪卡儂','values':[...], 'axis':'secondary'}]
      • 簡寫：'values':[...]（單一序列，無名）
    """
    series_raw = spec.get("series")
    if series_raw is None:
        # 簡寫：頂層直接給 values
        if "values" in spec:
            series_raw = [{"name": spec.get("name") or "", "values": spec["values"]}]
        else:
            return []
    if not isinstance(series_raw, list):
        raise ValueError("series 必須是陣列")
    out = []
    for i, s in enumerate(series_raw):
        if not isinstance(s, dict):
            raise ValueError(f"series[{i}] 必須是物件（含 name / values）")
        name = str(s.get("name") or s.get("label") or f"序列{i + 1}")
        values = _coerce_floats(
            s.get("values") if s.get("values") is not None else s.get("data"),
            where=f"series[{i}]({name}).values",
        )
        axis = str(s.get("axis") or s.get("y_axis") or "primary").strip().lower()
        secondary = axis in _SECONDARY_TOKENS
        kind = str(s.get("kind") or s.get("type") or "").strip().lower()
        kind = "line" if kind in ("line", "折線", "線") else ("bar" if kind in ("bar", "長條", "柱") else "")
        # annotations：每個資料點的標籤字串（barh 用來標「58% (7,020/12,000)」）；
        # colors：每個資料點的顏色（barh 用來依達成率紅/橘/綠分級）。長度需與 values 齊。
        annotations = s.get("annotations")
        if annotations is not None and (not isinstance(annotations, list)
                                        or len(annotations) != len(values)):
            raise ValueError(f"series[{i}]({name}).annotations 長度需與 values 相同")
        colors = s.get("colors")
        if colors is not None and (not isinstance(colors, list)
                                   or len(colors) != len(values)):
            raise ValueError(f"series[{i}]({name}).colors 長度需與 values 相同")
        out.append({
            "name": name, "values": values, "color": s.get("color"),
            "secondary": secondary, "kind": kind,
            "annotations": annotations, "colors": colors,
        })
    return out


# ────────────────────────────────────────────────────────────────────
# matplotlib 設定 + renderer
# ────────────────────────────────────────────────────────────────────
def _setup_cjk_font(plt) -> None:
    existing = list(plt.rcParams.get("font.sans-serif", []))
    # 把可用的 CJK 字型排到最前面（matplotlib 會依序找第一個系統有的）
    plt.rcParams["font.sans-serif"] = _CJK_FONTS + [f for f in existing if f not in _CJK_FONTS]
    plt.rcParams["axes.unicode_minus"] = False  # 避免負號變成豆腐


def _draw_gantt(fig, ax, spec, plt) -> None:
    import matplotlib.dates as mdates

    tasks = spec.get("tasks") or spec.get("rows") or spec.get("bars") or []
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("甘特圖需要 'tasks' 陣列，每項含 label / start / end")

    parsed = []  # (label, start_dt_or_num, end_dt_or_num, annotation, color)
    use_dates: bool | None = None
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            raise ValueError(f"tasks[{i}] 必須是物件（含 label / start / end）")
        label = str(t.get("label") or t.get("name") or t.get("task") or f"項目{i + 1}")
        s_dt, s_num = _parse_axis_value(t.get("start") if t.get("start") is not None else t.get("begin"))
        e_dt, e_num = _parse_axis_value(t.get("end") if t.get("end") is not None else t.get("finish"))
        s_is_date = s_dt is not None
        e_is_date = e_dt is not None
        if (s_dt is None and s_num is None) or (e_dt is None and e_num is None):
            raise ValueError(f"tasks[{i}]({label}) 的 start/end 解析失敗："
                             f"start={t.get('start')!r} end={t.get('end')!r}")
        if s_is_date != e_is_date:
            raise ValueError(f"tasks[{i}]({label}) 的 start 與 end 型別不一致（一個是日期一個是數字）")
        if use_dates is None:
            use_dates = s_is_date
        elif use_dates != s_is_date:
            raise ValueError("所有 task 的 start/end 必須一致都是日期或都是數字，不能混用")
        annotation = t.get("annotation") or t.get("note") or t.get("text") or ""
        parsed.append((label, s_dt or s_num, e_dt or e_num, str(annotation), t.get("color")))

    n = len(parsed)
    # y 由上往下（第一個 task 在最上面）
    y_positions = list(range(n - 1, -1, -1))

    if use_dates:
        starts = [mdates.date2num(p[1]) for p in parsed]
        ends = [mdates.date2num(p[2]) for p in parsed]
        min_w = 0.5  # 半天，讓單日任務也看得見
    else:
        starts = [float(p[1]) for p in parsed]
        ends = [float(p[2]) for p in parsed]
        span = (max(ends) - min(starts)) or 1.0
        min_w = span * 0.005

    widths = [max(e - s, min_w) for s, e in zip(starts, ends)]

    cmap = plt.get_cmap("tab10")
    for idx, (pos, (label, _s, _e, annotation, color)) in enumerate(zip(y_positions, parsed)):
        bar_color = color or cmap(idx % 10)
        ax.barh(pos, widths[idx], left=starts[idx], height=0.55,
                color=bar_color, edgecolor="white", alpha=0.92, zorder=3)
        if annotation:
            ax.text(starts[idx] + widths[idx], pos, f"  {annotation}",
                    va="center", ha="left", fontsize=9, color="#2C3E50", zorder=4)

    ax.set_yticks(y_positions)
    ax.set_yticklabels([p[0] for p in parsed])
    ax.set_xlabel(spec.get("xlabel") or ("日期" if use_dates else ""))
    if spec.get("ylabel"):
        ax.set_ylabel(spec["ylabel"])
    ax.grid(axis="x", linestyle="--", alpha=0.4, zorder=0)
    # 留點右邊空間給註記文字
    ax.margins(x=0.12)
    if use_dates:
        ax.xaxis_date()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        fig.autofmt_xdate(rotation=0)


def _draw_xy(fig, ax, spec, ctype, plt):
    """畫 bar / stacked_bar / grouped_bar / line（共用 x 軸分類邏輯，支援雙軸 + 組合）。"""
    import numpy as np

    categories = (spec.get("categories") or spec.get("x")
                  or spec.get("labels") or spec.get("xticks"))
    series = _normalize_series(spec)
    if not series:
        raise ValueError(f"{ctype} 需要 'series'（或簡寫 'values'）資料")
    n_pts = len(series[0]["values"])
    if categories is None:
        categories = [str(i + 1) for i in range(n_pts)]
    if not isinstance(categories, (list, tuple)):
        raise ValueError("categories / x 必須是陣列")
    categories = [str(c) for c in categories]
    for s in series:
        if len(s["values"]) != len(categories):
            raise ValueError(f"序列「{s['name']}」有 {len(s['values'])} 個值，"
                             f"但 categories 有 {len(categories)} 個 —— 數量要一致")

    x = np.arange(len(categories))
    stacked = (ctype == "stacked_bar")
    default_kind = "line" if ctype == "line" else "bar"
    cmap = plt.get_cmap("tab10")

    # 是否需要副軸？
    need_secondary = any(s["secondary"] for s in series)
    ax2 = ax.twinx() if need_secondary else None

    legend_handles, legend_labels = [], []

    def _axis_for(s):
        return ax2 if (s["secondary"] and ax2 is not None) else ax

    # 先把每個軸上的 bar 序列分群（為了 grouped 偏移 / stacked 疊加）
    for target in (ax, ax2):
        if target is None:
            continue
        bar_series = [s for s in series if _axis_for(s) is target
                      and (s["kind"] or default_kind) == "bar"]
        line_series = [s for s in series if _axis_for(s) is target
                       and (s["kind"] or default_kind) == "line"]

        # bars
        if stacked:
            bottom = np.zeros(len(x))
            for j, s in enumerate(bar_series):
                vals = np.array(s["values"])
                color = s["color"] or cmap(series.index(s) % 10)
                h = target.bar(x, vals, bottom=bottom, width=0.6,
                               label=s["name"], color=color, zorder=3)
                bottom += vals
                legend_handles.append(h)
                legend_labels.append(s["name"])
        else:
            nb = max(len(bar_series), 1)
            total_w = 0.8
            w = total_w / nb
            for j, s in enumerate(bar_series):
                offset = (j - (nb - 1) / 2.0) * w
                # 單序列可給 series.colors 逐條上色（例：每日產量把產能高峰那天標紅）
                color = s["colors"] if s["colors"] else (s["color"] or cmap(series.index(s) % 10))
                h = target.bar(x + offset, s["values"], width=w * 0.95,
                               label=s["name"], color=color, zorder=3)
                legend_handles.append(h)
                legend_labels.append(s["name"])

        # lines（畫在分類中心）
        for s in line_series:
            color = s["color"] or cmap(series.index(s) % 10)
            (h,) = target.plot(x, s["values"], marker="o", linewidth=2.2,
                               label=s["name"], color=color, zorder=4)
            legend_handles.append(h)
            legend_labels.append(s["name"])

    ax.set_xticks(x)
    rot = 45 if max((len(c) for c in categories), default=0) > 4 or len(categories) > 10 else 0
    ax.set_xticklabels(categories, rotation=rot, ha="right" if rot else "center")
    ax.set_xlabel(spec.get("xlabel") or "")
    ax.set_ylabel(spec.get("ylabel") or "")
    ax.grid(axis="y", linestyle="--", alpha=0.4, zorder=0)
    if ax2 is not None:
        ax2.set_ylabel(spec.get("ylabel2") or spec.get("secondary_ylabel") or "")
        ax2.grid(False)

    if len(legend_handles) > 1 or (series and series[0]["name"] and not series[0]["name"].startswith("序列")):
        ax.legend(legend_handles, legend_labels, loc="best", fontsize=9, framealpha=0.9)


def _draw_pie(fig, ax, spec, plt):
    labels = spec.get("labels") or spec.get("categories") or spec.get("x")
    values = spec.get("values")
    if values is None:
        series = _normalize_series(spec)
        if series:
            values = series[0]["values"]
            if labels is None:
                labels = spec.get("categories")
    if values is None:
        raise ValueError("圓餅圖需要 'values'（與對應的 'labels'）")
    values = _coerce_floats(values, where="pie.values")
    if labels is None:
        labels = [str(i + 1) for i in range(len(values))]
    labels = [str(x) for x in labels]
    if len(labels) != len(values):
        raise ValueError(f"labels({len(labels)}) 與 values({len(values)}) 數量不一致")
    cmap = plt.get_cmap("tab10")
    ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90,
           colors=[cmap(i % 10) for i in range(len(values))],
           textprops={"fontsize": 10}, wedgeprops={"edgecolor": "white"})
    ax.axis("equal")


def _draw_barh(fig, ax, spec, plt):
    """水平長條 —— 適合各項目排名/進度/達成率（分類名稱長時比直條好讀）。

    支援每條自訂色（series.colors，例：達成率紅/橘/綠分級）、條尾標籤
    （series.annotations，例「58% (7,020/12,000)」，沒給就自動帶數值+value_suffix）、
    以及一條垂直參考線（reference_line/reference_label，例 100% 目標）。
    """
    import numpy as np

    categories = spec.get("categories") or spec.get("labels") or spec.get("y")
    series = _normalize_series(spec)
    if not series:
        raise ValueError("橫條圖需要 'series'（或簡寫 'values'）資料")
    if categories is None:
        categories = [str(i + 1) for i in range(len(series[0]["values"]))]
    if not isinstance(categories, (list, tuple)):
        raise ValueError("categories / labels 必須是陣列")
    categories = [str(c) for c in categories]
    for s in series:
        if len(s["values"]) != len(categories):
            raise ValueError(f"序列「{s['name']}」有 {len(s['values'])} 個值，"
                             f"但 categories 有 {len(categories)} 個 —— 數量要一致")

    y = np.arange(len(categories))
    nb = len(series)
    total_h = 0.8
    h = total_h / nb
    cmap = plt.get_cmap("tab10")
    suffix = spec.get("value_suffix", "")
    show_values = spec.get("value_labels", True)  # barh 預設標數值

    for j, s in enumerate(series):
        offset = (j - (nb - 1) / 2.0) * h
        bar_colors = s["colors"] if s["colors"] else (s["color"] or cmap(j % 10))
        bars = ax.barh(y + offset, s["values"], height=h * 0.9,
                       label=s["name"], color=bar_colors, edgecolor="white", zorder=3)
        labels = s["annotations"]
        if labels is None and show_values:
            labels = [f"{v:g}{suffix}" for v in s["values"]]
        if labels:
            for rect, lab in zip(bars, labels):
                ax.text(rect.get_width(), rect.get_y() + rect.get_height() / 2,
                        f"  {lab}", va="center", ha="left", fontsize=9,
                        color="#2C3E50", zorder=4)

    ax.set_yticks(y)
    ax.set_yticklabels(categories)
    ax.invert_yaxis()                       # 第一個項目排最上面
    ax.set_xlabel(spec.get("xlabel") or "")
    if spec.get("ylabel"):
        ax.set_ylabel(spec["ylabel"])
    ax.grid(axis="x", linestyle="--", alpha=0.4, zorder=0)
    ax.margins(x=0.16)                       # 留空間給條尾標籤

    ref = spec.get("reference_line")
    if ref is not None:
        try:
            refv = float(ref)
        except (TypeError, ValueError):
            refv = None
        if refv is not None:
            ax.axvline(refv, linestyle="--", color="#E74C3C", linewidth=1.4, zorder=2)
            if spec.get("reference_label"):
                ax.text(refv, 1.01, str(spec["reference_label"]),
                        transform=ax.get_xaxis_transform(),
                        color="#E74C3C", fontsize=9, ha="center", va="bottom")

    if nb > 1:
        ax.legend(loc="best", fontsize=9, framealpha=0.9)


def _figsize(ctype: str, spec: dict) -> tuple[float, float]:
    if ctype == "barh":
        cats = spec.get("categories") or spec.get("labels") or spec.get("y")
        n = len(cats) if isinstance(cats, (list, tuple)) else 6
        return 10.0, max(2.8, min(0.62 * n + 1.8, 16.0))
    if ctype == "gantt":
        n = len(spec.get("tasks") or spec.get("rows") or spec.get("bars") or [])
        return 11.0, max(2.8, min(0.62 * n + 1.6, 16.0))
    if ctype == "pie":
        return 7.5, 6.5
    # bar / line：寬度隨分類數
    cats = spec.get("categories") or spec.get("x") or spec.get("labels") or []
    n = len(cats) if isinstance(cats, (list, tuple)) else 8
    return max(8.0, min(0.7 * n + 2.5, 24.0)), 5.8


def _render_chart(spec: dict, path: str) -> None:
    """把 chart spec 畫成圖檔存到 path。重的 matplotlib 在這裡 lazy import。"""
    import matplotlib
    matplotlib.use("Agg")  # 無 GUI / daemon 環境必備
    import matplotlib.pyplot as plt

    _setup_cjk_font(plt)
    ctype = spec["type"]
    fig, ax = plt.subplots(figsize=_figsize(ctype, spec))

    try:
        if ctype == "gantt":
            _draw_gantt(fig, ax, spec, plt)
        elif ctype in ("bar", "grouped_bar", "stacked_bar", "line"):
            _draw_xy(fig, ax, spec, ctype, plt)
        elif ctype == "barh":
            _draw_barh(fig, ax, spec, plt)
        elif ctype == "pie":
            _draw_pie(fig, ax, spec, plt)
        else:  # pragma: no cover - 型別已在上游正規化
            raise ValueError(f"未支援的圖表型別：{ctype}")

        title = spec.get("title")
        if title:
            fig.suptitle(str(title), fontsize=15, fontweight="bold")
        subtitle = spec.get("subtitle")
        if subtitle:
            ax.set_title(str(subtitle), fontsize=10, color=_MUTED, pad=8)

        fig.tight_layout(rect=(0, 0, 1, 0.96) if title else None)
        fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    finally:
        plt.close(fig)


# ────────────────────────────────────────────────────────────────────
# Telegram 交付（best-effort）
# ────────────────────────────────────────────────────────────────────
def _deliver_chart(path: str, title: str, chat_id: str) -> list[str]:
    try:
        from agent_core.telegram import telegram_send_photo
    except Exception as e:  # pragma: no cover - 載入失敗極少見
        return [f"⚠️ 無法載入 Telegram 傳圖工具：{e}（圖已存到 {EXPORTS_DIR}）"]
    caption = f"小紅畫的{title}"[:900]
    try:
        res = telegram_send_photo(path, caption=caption, chat_id=chat_id)
        ok = getattr(res, "ok", True)
        if ok:
            return [f"📤 已傳到 Telegram：✅ {os.path.basename(path)}"]
        return [f"📤 傳送未成功：❌ {os.path.basename(path)}\n   {res}"]
    except Exception as e:
        return [f"⚠️ 傳送失敗：{type(e).__name__}: {e}（圖已存到 {EXPORTS_DIR}）"]


def _prune_old_exports(keep_days: int | None = None) -> None:
    """刪掉 EXPORTS_DIR 內超過 keep_days 天沒更動的舊圖/報表，避免無限累積。

    keep_days 由 RED_EXPORTS_KEEP_DAYS 控制（預設 14）；<=0 表示停用清理。
    只動 EXPORTS_DIR 下的產出格式（png/xlsx/docx/pdf），best-effort（任何錯誤都吞掉，
    清理失敗絕不可擋住畫圖本身）。在 generate_chart 寫檔前順手呼叫。
    """
    import time
    if keep_days is None:
        try:
            keep_days = int(os.environ.get("RED_EXPORTS_KEEP_DAYS", "14"))
        except (TypeError, ValueError):
            keep_days = 14
    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    try:
        entries = os.listdir(EXPORTS_DIR)
    except OSError:
        return
    for fn in entries:
        if not fn.lower().endswith((".png", ".xlsx", ".docx", ".pdf")):
            continue
        p = os.path.join(EXPORTS_DIR, fn)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


# ────────────────────────────────────────────────────────────────────
# Tool entry point
# ────────────────────────────────────────────────────────────────────
def generate_chart(spec_json: str, filename: str = "", deliver: bool = True,
                   chat_id: str = "") -> ToolResult:
    """把資料畫成圖表（甘特圖 / 長條圖 / 堆疊圖 / 折線圖 / 圓餅圖）並直接傳到大王 Telegram。

    大王說「把每天的生產做成甘特圖」「畫個效率堆疊圖」「畫趨勢線給我」時就用這個 ——
    它會真的產出 PNG 圖檔並送到 Telegram（inline 預覽），而不是只回文字，也**不需要
    任何 +確認 / +雙確認**（這是安全的結構化畫圖工具，不是任意程式碼執行）。

    Args:
        spec_json: 圖表規格（JSON 字串）。共用欄位：
            - "type"：圖表型別。甘特圖="gantt"/"甘特圖"；長條="bar"/"柱狀圖"（多序列自動
              並排）；堆疊長條="stacked_bar"/"堆疊圖"；折線="line"/"折線圖"；圓餅="pie"；
              水平長條="barh"/"橫條圖"（適合排名/進度/達成率，分類名稱長時好讀；可給
              series.colors 每條分色、series.annotations 標條尾、reference_line 畫目標線）。
            - "title"：主標題（選填）。"subtitle"：副標題（選填）。
            - "xlabel" / "ylabel"：軸標題（選填）。

          甘特圖（gantt）—— 以橫條呈現每個項目的起訖：
            '{"type":"甘特圖","title":"6月生產線甘特圖",
              "tasks":[
                {"label":"迪卡儂","start":"2026-06-01","end":"2026-06-14","annotation":"累計 12000 雙"},
                {"label":"Lurchi","start":"2026-06-03","end":"2026-06-11"}
              ]}'
            start/end 可用日期（YYYY-MM-DD、M/D）或純數字；annotation 會標在橫條尾端。

          長條 / 堆疊 / 折線 —— 用 categories（X 軸）+ series（多條資料）：
            '{"type":"堆疊長條圖","title":"每日各品牌產量",
              "categories":["6/1","6/2","6/3"],
              "series":[
                {"name":"迪卡儂","values":[120,150,90]},
                {"name":"Lurchi","values":[80,60,110]},
                {"name":"效率%","values":[88,91,75],"kind":"line","axis":"secondary"}
              ]}'
            單一序列可簡寫成頂層 "values":[...]。某序列設 "axis":"secondary" 會畫在右側
            副軸（搭配 "ylabel2"），"kind":"line"/"bar" 可做雙軸組合圖（柱+線）。

          圓餅（pie）：'{"type":"pie","title":"客戶占比","labels":["迪卡儂","Lurchi"],"values":[60,40]}'

        filename: 檔名（不含副檔名）；留空自動用標題 + 時間戳。
        deliver: 是否自動傳到 Telegram（預設 True）。在 Telegram 對話裡叫小紅畫就保持
                 True；只想存檔不傳設 False。
        chat_id: 指定 Telegram chat；留空用大王預設。

    Returns:
        ToolResult.success — summary 列出產出與傳送結果；artifacts 是 PNG 絕對路徑。
    """
    try:
        spec = json.loads(spec_json) if isinstance(spec_json, str) else spec_json
    except Exception as e:
        return ToolResult.failure(
            f"spec_json 不是合法 JSON：{e}",
            error_code=ErrorCode.INVALID_INPUT, recoverable=False,
            suggested_fix='給含 "type" 的物件，例：{"type":"甘特圖","tasks":[...]}')
    if not isinstance(spec, dict):
        return ToolResult.failure(
            "spec_json 解析後不是物件（需要含 type 的 dict）。",
            error_code=ErrorCode.INVALID_INPUT, recoverable=False)

    ctype = _normalize_type(spec.get("type"))
    if ctype is None:
        return ToolResult.failure(
            f"不認得的圖表型別：{spec.get('type')!r}",
            error_code=ErrorCode.INVALID_INPUT, recoverable=False,
            suggested_fix="支援 gantt(甘特圖) / bar(長條圖) / stacked_bar(堆疊圖) / "
                          "line(折線圖) / pie(圓餅圖)")
    spec["type"] = ctype

    # 檔名 + 輸出目錄
    from agent_core.doc_export import _sanitize_filename  # 復用既有 sanitizer
    base = _sanitize_filename(filename) or _sanitize_filename(spec.get("title") or ctype)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        os.makedirs(EXPORTS_DIR, exist_ok=True)
    except OSError as e:
        return ToolResult.failure(f"建立輸出目錄失敗：{e}", error_code=ErrorCode.INTERNAL)
    _prune_old_exports()  # 順手清掉超過保留天數的舊圖/報表（best-effort）
    out_path = os.path.join(EXPORTS_DIR, f"{base}_{stamp}.png")

    try:
        _render_chart(spec, out_path)
    except ImportError as e:
        return ToolResult.failure(
            f"缺 matplotlib（{e}）—— 無法畫圖。",
            error_code=ErrorCode.INTERNAL, recoverable=False,
            suggested_fix="pip install matplotlib")
    except ValueError as e:
        # 資料格式問題 → 可修正，給清楚的訊息讓 LLM 重試
        return ToolResult.failure(
            f"圖表資料有問題：{e}",
            error_code=ErrorCode.INVALID_INPUT, recoverable=True)
    except Exception as e:
        logger.exception("generate_chart 畫 %s 失敗", ctype)
        return ToolResult.failure(
            f"畫圖失敗：{type(e).__name__}: {e}",
            error_code=ErrorCode.INTERNAL)

    size = os.path.getsize(out_path)
    lines = [f"✅ 已畫出圖表：{os.path.basename(out_path)}（{size:,} bytes）"]
    if deliver:
        lines.append("")
        lines.extend(_deliver_chart(out_path, spec.get("title") or "圖表", chat_id))
    else:
        lines.append(f"（未自動傳送；圖在 {EXPORTS_DIR}）")

    return ToolResult.success("\n".join(lines),
                              data={"path": out_path, "type": ctype},
                              artifacts=[out_path])
