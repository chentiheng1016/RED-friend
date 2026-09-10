"""Excel skill：讀、寫、pivot、filter、自然語言問答。

讓小紅能處理日常 xlsx 檔案 — 訂單表、報價表、應收應付等。

依賴：pandas, openpyxl（setup.sh 裝 requirements.txt 就有）。

用法範例（自然語言）：
  「幫我看一下 ~/Downloads/訂單.xlsx 有多少列」
  「把 /tmp/sales.xlsx 做一個依客戶分組的 pivot，看總金額」
  「這個 Excel 裡篩出金額 > 10000 的單」
"""
from __future__ import annotations

import json
import os
from typing import Any

import pandas as pd


def _resolve_path(path: str) -> str:
    """展開 ~ 跟相對路徑，並過 V7 path safety guard。

    阻擋：~/.ssh, ~/.aws, /etc, Keychain, *.pem, credentials.json 等。
    若被擋會 raise ValueError；上層 tool 函式用 _safe_or_err 包起來轉成
    user-readable 字串回 caller。
    """
    from agent_core.path_safety import safe_path
    return safe_path(path)


def _safe_or_err(path: str):
    """Wrapper：跑 _resolve_path 但把 ValueError 變成 (None, error_msg)。
    成功回 (resolved_path, None)。"""
    try:
        return _resolve_path(path), None
    except ValueError as e:
        return None, str(e)


def _read_df(path: str, sheet: str | None = None):
    """統一讀入 DataFrame，錯誤訊息清楚。"""
    p = _resolve_path(path)
    if not os.path.isfile(p):
        raise FileNotFoundError(f"找不到檔案：{p}")
    if not p.lower().endswith((".xlsx", ".xls", ".xlsm")):
        raise ValueError(f"不是 Excel 檔：{p}（支援 .xlsx/.xls/.xlsm）")
    try:
        if sheet:
            return pd.read_excel(p, sheet_name=sheet), p
        return pd.read_excel(p), p
    except Exception as e:
        raise RuntimeError(f"讀取失敗 ({type(e).__name__}): {e}")


def excel_sheets(path: str) -> str:
    """列出 Excel 檔案裡有哪些 sheet（工作表）。

    Args:
        path: xlsx 檔案路徑，支援 ~ 和相對路徑。
    Returns:
        工作表名稱清單字串。
    """
    p, err = _safe_or_err(path)
    if err:
        return err
    if not os.path.isfile(p):
        return f"❌ 找不到檔案：{p}"
    try:
        xl = pd.ExcelFile(p)
        lines = [f"📊 {os.path.basename(p)} 共 {len(xl.sheet_names)} 個 sheet："]
        for name in xl.sheet_names:
            try:
                # 順便顯示每個 sheet 有幾列 x 幾欄
                df = pd.read_excel(p, sheet_name=name)
                lines.append(f"  • {name}  ({len(df)} 列 × {len(df.columns)} 欄)")
            except Exception:
                lines.append(f"  • {name}  (讀取 meta 失敗)")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 開檔失敗: {type(e).__name__}: {e}"


def excel_read(path: str, sheet: str = "", rows: int = 30) -> str:
    """讀 Excel 檔回傳結構預覽（欄位名、前 N 列、基本統計）。

    Args:
        path: xlsx 檔案路徑。
        sheet: 工作表名；空字串代表第一個 sheet。
        rows: 最多顯示幾列（預設 30）。
    Returns:
        欄位摘要 + 前 N 列 + 數值欄位統計。
    """
    try:
        df, p = _read_df(path, sheet or None)
    except Exception as e:
        return f"❌ {e}"

    lines = [
        f"📊 {os.path.basename(p)}" + (f" / {sheet}" if sheet else ""),
        f"總列數: {len(df)}，總欄數: {len(df.columns)}",
        "",
        f"欄位: {', '.join(str(c) for c in df.columns)}",
        "",
        f"--- 前 {min(rows, len(df))} 列 ---",
        df.head(rows).to_string(index=False, max_cols=12),
    ]

    # 數值欄位統計
    num_cols = df.select_dtypes(include="number").columns.tolist()
    if num_cols:
        lines += [
            "",
            "--- 數值欄位統計 ---",
            df[num_cols].describe().round(2).to_string(),
        ]
    return "\n".join(lines)


def excel_pivot(path: str, index: str, values: str,
                columns: str = "", agg: str = "sum",
                sheet: str = "") -> str:
    """做 pivot table，例如按客戶看總金額、按月看毛利。

    Args:
        path: xlsx 路徑。
        index: 分組的行索引欄位（例如 "客戶" 或 "月份"）。
        values: 要彙總的數值欄位（例如 "金額" 或 "數量"）。
        columns: 可選，用來做橫軸分組（例如 "產品"）。空字串就是純 groupby。
        agg: 彙總方法：sum / mean / count / max / min / median（預設 sum）。
        sheet: 工作表名；空字串=第一個 sheet。
    Returns:
        pivot 結果（人類可讀表格）。
    """
    try:
        df, p = _read_df(path, sheet or None)
    except Exception as e:
        return f"❌ {e}"

    if index not in df.columns:
        return f"❌ index 欄位 '{index}' 不存在；可用欄位：{', '.join(str(c) for c in df.columns)}"
    if values not in df.columns:
        return f"❌ values 欄位 '{values}' 不存在；可用欄位：{', '.join(str(c) for c in df.columns)}"
    if columns and columns not in df.columns:
        return f"❌ columns 欄位 '{columns}' 不存在"

    try:
        if columns:
            result = pd.pivot_table(df, index=index, columns=columns, values=values, aggfunc=agg, fill_value=0)
        else:
            result = df.groupby(index)[values].agg(agg).to_frame(name=f"{values}_{agg}")
        # 排序（金額大的在前）
        result = result.sort_values(by=result.columns[0], ascending=False)
    except Exception as e:
        return f"❌ pivot 失敗: {type(e).__name__}: {e}"

    lines = [
        f"📊 {os.path.basename(p)} — pivot",
        f"index={index}, values={values}" + (f", columns={columns}" if columns else "") + f", agg={agg}",
        "",
        result.round(2).to_string(),
        "",
        f"共 {len(result)} 組",
    ]
    return "\n".join(lines)


def excel_filter(path: str, condition: str,
                 output_path: str = "", sheet: str = "") -> str:
    """用 pandas query 語法 filter 資料列。

    Args:
        path: 來源 xlsx。
        condition: pandas query 字串，範例：
                   - "金額 > 10000"
                   - "客戶 == 'Lurchi' and 月份 >= 4"
                   - "品名.str.contains('工作鞋')"
        output_path: 可選，過濾後另存 xlsx。空字串不存只回傳預覽。
        sheet: 工作表。
    Returns:
        過濾後的筆數 + 前 30 列預覽；有給 output_path 就也存檔。
    """
    try:
        df, p = _read_df(path, sheet or None)
    except Exception as e:
        return f"❌ {e}"

    try:
        filtered = df.query(condition, engine="python")
    except Exception as e:
        return f"❌ filter 失敗（condition='{condition}'）: {type(e).__name__}: {e}"

    lines = [
        f"📊 {os.path.basename(p)} — filter '{condition}'",
        f"符合條件：{len(filtered)} / {len(df)} 列",
        "",
        filtered.head(30).to_string(index=False, max_cols=12),
    ]
    if output_path:
        out, err = _safe_or_err(output_path)
        if err:
            lines.append(f"\n{err}")
        else:
            try:
                filtered.to_excel(out, index=False)
                lines.append(f"\n✅ 已另存：{out}")
            except Exception as e:
                lines.append(f"\n❌ 存檔失敗: {type(e).__name__}: {e}")
    return "\n".join(lines)


def excel_write(output_path: str, data_json: str, sheet: str = "Sheet1") -> str:
    """把 JSON 資料寫成 xlsx 檔。

    Args:
        output_path: 要存到哪裡（.xlsx）。
        data_json: JSON 字串，必須是 list of dict 格式。
                   範例：'[{"客戶":"A","金額":1000},{"客戶":"B","金額":2000}]'
        sheet: 工作表名。
    Returns:
        存檔結果 + 檔案大小。
    """
    out, err = _safe_or_err(output_path)
    if err:
        return err
    if not out.lower().endswith(".xlsx"):
        out += ".xlsx"
    try:
        data = json.loads(data_json)
    except Exception as e:
        return f"❌ data_json 不是合法 JSON: {e}"
    if not isinstance(data, list) or (data and not isinstance(data[0], dict)):
        return "❌ data_json 必須是 list of dict，例如 [{\"a\":1}, {\"a\":2}]"

    try:
        df = pd.DataFrame(data)
        df.to_excel(out, index=False, sheet_name=sheet)
    except Exception as e:
        return f"❌ 寫檔失敗: {type(e).__name__}: {e}"

    size = os.path.getsize(out)
    return f"✅ 已寫入 {out}（{len(df)} 列，{size:,} bytes）"


def excel_query(path: str, question: str, sheet: str = "") -> str:
    """用自然語言問 Excel 資料的問題，Gemini 會自動生 pandas code 算答案。

    適合開放式問題，例如：
      - 「前 10 大客戶總營收排名？」
      - 「每個月的平均訂單金額？」
      - 「哪個產品利潤最高？」

    Args:
        path: xlsx 檔案路徑。
        question: 問題（中文英文都可）。
        sheet: 工作表名；空字串=第一個 sheet。
    Returns:
        小紅的回答（已執行過計算）。
    """
    try:
        df, p = _read_df(path, sheet or None)
    except Exception as e:
        return f"❌ {e}"

    # 把 df schema + head(5) + question 丟 Gemini 讓它生 pandas code
    from agent_core.gemini_client import _gemini_generate, GEMINI_MODEL

    schema = ", ".join(f"{c}({df[c].dtype})" for c in df.columns)
    prompt = f"""你是 pandas 專家。DataFrame 變數叫 df，schema 如下：

{schema}

前 5 列：
{df.head(5).to_string(index=False, max_cols=12)}

總共 {len(df)} 列。

請回答以下問題，**只輸出 Python pandas 代碼**（不要用 markdown fence，就直接寫代碼）。
最後一行要用 `result = ...` 把答案存到 result 變數（可以是 Series、DataFrame 或 scalar）。

問題：{question}
"""
    try:
        resp = _gemini_generate(model=GEMINI_MODEL, contents=[prompt])
        code = (resp.text or "").strip()
        # 抓可能被 markdown 包住的 code block
        import re
        m = re.search(r"```(?:python)?\s*\n?(.+?)```", code, re.DOTALL)
        if m:
            code = m.group(1).strip()
    except Exception as e:
        return f"❌ Gemini 生碼失敗: {e}"

    # 靜態檢查：擋明顯的注入 / 逃逸嘗試
    # LLM 生成的代碼理論上只該做 pandas 操作，不該需要 import / open / exec
    #
    # ⚠️ C1 修補（review 找到的 bypass）：原本的 \bpickle\b 對 `pd.read_pickle`
    # 不命中 — `_` 是 word char，所以 read_pickle 中的 pickle 沒 word boundary。
    # 結果：`result = pd.read_pickle("/etc/passwd")` 過 deny → pickle 反序列化 = RCE。
    # 修法：明確 deny 所有 pandas I/O 方法（read_*/to_*/HDFStore/ExcelWriter 等）。
    _FORBIDDEN_PATTERNS = [
        r"\b__\w+__\b",           # dunder access (__import__, __class__, __builtins__, ...)
        r"\bimport\b",             # import statement
        r"\bopen\s*\(",            # open(
        r"\bexec\s*\(", r"\beval\s*\(", r"\bcompile\s*\(",
        r"\bgetattr\s*\(", r"\bsetattr\s*\(", r"\bdelattr\s*\(",
        r"\bglobals\s*\(", r"\blocals\s*\(", r"\bvars\s*\(",
        r"\b(os|sys|subprocess|socket|shutil|pathlib|pickle|marshal|importlib)\b",
        # pandas read_* methods — RCE via pd.read_pickle 反序列化攻擊者準備的 file
        r"\.read_\w+\b",
        # pandas write methods — 寫到 path_safety 沒檢查的位置
        r"\.to_(?:csv|excel|pickle|hdf|parquet|json|feather|orc|sql|"
        r"clipboard|xml|html|stata|latex|gbq|msgpack|sas|spss)\b",
        # C9 補丁（review 找到）：to_string(buf=...) / to_markdown(buf=...) 也會寫檔。
        # 任何 to_X 帶 buf= / path_or_buf= / excel_writer= 等 path-like kwarg 都禁。
        r"\.to_\w+\s*\([^)]*\b(?:buf|path_or_buf|excel_writer|writer|fname|filepath)\s*=",
        # pandas I/O 構造器 — C8 補丁：原本 `\b(?:HDFStore|ExcelWriter|ExcelFile)\s*\(`
        # 可被 `cls = pd.ExcelWriter; cls("/path")` 兩行 alias 繞過。改 \b...\b 鎖名稱本身。
        # C12 補丁（review round 4）：補 ExcelFormatter / CSVFormatter 等內部 writer。
        r"\b(?:HDFStore|ExcelWriter|ExcelFile|ExcelFormatter|"
        r"CSVFormatter|HTMLFormatter|StataWriter\w*|FrameFormatter)\b",
        # C12: pd.io.* 子模組無 Q&A 用途 — 整條擋（避免 pd.io.formats.excel.X 繞）
        r"\bpd\s*\.\s*io\b",
        # 直接讀 clipboard 也算 I/O 出 sandbox
        r"\.read_clipboard\b",
    ]
    import re as _re_sec
    for pattern in _FORBIDDEN_PATTERNS:
        if _re_sec.search(pattern, code):
            return (f"❌ 生成的代碼含禁用 token（{pattern}），拒絕執行。\n\n"
                    f"碼：\n{code[:500]}")

    # 沙箱 builtins：只給 pandas 操作需要的純函式，不給 import / open / eval / exec / file io
    _SAFE_BUILTIN_NAMES = (
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
        "int", "len", "list", "map", "max", "min", "range", "reversed",
        "round", "set", "slice", "sorted", "str", "sum", "tuple", "type",
        "zip", "True", "False", "None",
    )
    import builtins as _builtins
    _safe_builtins = {n: getattr(_builtins, n) for n in _SAFE_BUILTIN_NAMES
                      if hasattr(_builtins, n)}
    safe_globs = {"__builtins__": _safe_builtins, "pd": pd}
    local_ns = {"df": df}
    try:
        exec(code, safe_globs, local_ns)  # noqa: S102 - sandboxed
        result = local_ns.get("result", "（代碼沒寫 result = ...）")
    except Exception as e:
        return f"❌ 執行 Gemini 生成的代碼失敗：{type(e).__name__}: {e}\n\n碼：\n{code[:500]}"

    # 格式化輸出
    if isinstance(result, pd.DataFrame):
        out = result.head(30).to_string()
        if len(result) > 30:
            out += f"\n\n（共 {len(result)} 列，只顯示前 30）"
    elif isinstance(result, pd.Series):
        out = result.head(30).to_string()
    else:
        out = str(result)

    return f"📊 {os.path.basename(p)}\n問：{question}\n\n答：\n{out}"


def xlsx_extract_images(path: str, output_dir: str = "", sheet: str = "",
                        max_images: int = 0) -> str:
    """把 Excel 裡**貼在儲存格上的圖**抽成獨立圖檔，並回報每張圖原本在哪一列。

    用途：客人做好的追蹤表 / 樣品表把產品圖貼在格子裡，要沿用那些圖做新表時，
    先用這顆挖出來，再用 export_report 的圖片儲存格（`{"image": "<路徑>"}`）
    嵌回新表。回傳會帶「工作表!儲存格」與**該列前幾個欄位值**（款號 / 顏色）
    —— 圖檔本身看不出是哪一款，配對一律靠這個標籤，別照順序硬配。

    浮動圖與新版 Excel 的「置於儲存格」圖兩種都抽得到（回傳的 source 欄分別是
    `xlsx` / `xlsx-incell`）。⚠️ 只吃 .xlsx / .xlsm：舊的 .xls 二進位格式存不了
    這種內嵌圖，請先另存新檔。

    Args:
        path: Excel 路徑。
        output_dir: 輸出資料夾；留空 = 跟 Excel 同目錄下的 <name>_images/。
        sheet: 只抽某個工作表；留空 = 全部。
        max_images: 最多抽幾張；0 = 預設上限。
    """
    from agent_core.doc_images import extract_xlsx_images

    p, err = _safe_or_err(path)
    if err:
        return err
    if not os.path.isfile(p):
        return f"❌ 找不到：{p}"
    if not p.lower().endswith((".xlsx", ".xlsm")):
        return "❌ 只支援 .xlsx / .xlsm（舊 .xls 存不了內嵌圖，請先另存新檔）。"

    if not output_dir:
        base = os.path.splitext(os.path.basename(p))[0]
        output_dir = os.path.join(os.path.dirname(p), f"{base}_images")
    out_dir, err = _safe_or_err(output_dir)
    if err:
        return err
    try:
        items = extract_xlsx_images(
            p, out_dir, sheet=sheet,
            max_images=(max_images if max_images and max_images > 0 else None))
    except Exception as e:
        return f"❌ 抽圖失敗: {type(e).__name__}: {e}"
    if not items:
        return (f"⚠️ {os.path.basename(p)} 沒抽到貼在儲存格的圖"
                + (f"（工作表「{sheet}」）" if sheet else "")
                + "（可能圖在別的工作表、或是外部連結而非內嵌圖）。")
    lines = [f"✅ 抽出 {len(items)} 張圖 → {out_dir}"]
    for it in items:
        where = f"{it['sheet']}!{it['cell']}" if it["cell"] else it["sheet"]
        label = f"、該列：{it['label']}" if it["label"] else ""
        lines.append(f"  • {it['path']}（{where}{label}、{it['width']}×{it['height']}px）")
    lines.append('要嵌回 Excel：export_report 的該格填 {"image": "<路徑>"}；'
                 "配對用「該列」的款號，不要照順序硬配。")
    return "\n".join(lines)


SKILL_TOOLS = [excel_sheets, excel_read, excel_pivot, excel_filter, excel_write,
               excel_query, xlsx_extract_images]
