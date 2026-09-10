"""飛越 ERP(Oracle 10.2 / JHDB)全 schema 擷取 + 邏輯落點量測 — 走 erp_oracle_client(SSH+10g sqlplus)。

這是「反推重寫 ERP」評估裡的『共同無悔第一步』的 ②③（見記憶 project_erp_rebuild_abc_assessment）：
  ② 全 schema 擷取：把「只摸清 2 張表」擴成完整地圖（表/欄/註解/主外鍵/views）。
  ③ 量測邏輯落點：數 all_source(PL/SQL) / trigger / view —— 這個數字直接決定「靠 DB 反推
     能還原多少業務邏輯」，也就是方案B(漸進替換)的反推天花板。DB 端 PL/SQL 多＝可反推多；
     近乎為零＝業務規則幾乎全在 Oracle Forms 的 .fmb/.fmx，DB 反推拿不到。

與 agent_core/oracle_probe.py 的差別（**別混用**）：
  - oracle_probe.py 走 python-oracledb(DSN)：mac 上的 client 連不到 10g 飛越（thin 需 12.1+、
    無 mac Instant Client 連 10g），且只抽 表/欄/樣本，沒有註解/主外鍵/views/邏輯落點。
  - 本模組走 erp_oracle_client(SSH → 主機自帶 10g sqlplus)，是**實際連得到飛越**的路，且補齊
    上述缺口。查詢全部是資料字典 SELECT（all_tables/all_tab_columns/all_constraints/
    all_views/all_source/all_triggers），純唯讀。

安全：完全沿用 erp_oracle_client 的唯讀三道防線 + guard_select_only + row cap + is_enabled()
閘門。RED_ERP_ORACLE_ENABLED 預設 0 —— **主機清毒 + SSH 就緒前，本探勘物理上跑不起來**。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from pathlib import Path
from typing import Callable

from agent_core import erp_oracle_client as _erp
from agent_core.erp_oracle_client import (
    ErpOracleError,
    clean_text_col,
    fetch_table,
    ssh_sqlplus_stream,
)
from agent_core.logging_and_paths import DATA_DIR, _atomic_write_text

Fetch = Callable[..., dict]

_OUTPUT_DIR = os.path.join(DATA_DIR, "erp_schema_probe")

# Oracle schema/owner 名：字母開頭、英數 _ $ #，≤30。owner 會內嵌進資料字典 SELECT，
# 故先驗證防注入（帳號/表值不同，這裡是 schema 名，用比 validate_identifier 更嚴的白名單）。
_OWNER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,29}$")

# 大字典查詢的列上限（fetch_table 的 limit 直接當 cap、不再夾）：一個 schema 通常
# 數百表 / 數千欄，給足裕度；真超過會標 truncated，重跑時縮 owner 範圍即可。
_CAP_TABLES = 5000
_CAP_COLUMNS = 60000
_CAP_CONSTRAINTS = 40000
_CAP_VIEWS = 5000
_CAP_SMALL = 500

# 已知屬於飛越 APP schema 的核心表（見 docs/erp_schema_map.md）—— 用來自動偵測 owner。
_KNOWN_TABLES = ("BQ_SE_ORDITEM", "BQ_SE_ITEMSCHE")


def validate_owner(owner: str) -> str:
    """驗證 schema owner（大寫化），不合法 raise ErpOracleError。owner 會內嵌進 SQL，故防注入。"""
    v = (owner or "").strip().upper()
    if not _OWNER_RE.match(v):
        raise ErpOracleError(f"schema owner 格式不合法「{owner}」（字母開頭、英數 _ $ #，≤30）。")
    return v


# ── SQL 產生器（純字串，皆為單條資料字典 SELECT；每條都過得了 guard_select_only）──────
def sql_detect_owner() -> str:
    """找已知核心表所在的 owner（回 owner, 命中表數；最可能的 APP schema 在最前）。"""
    names = ",".join(f"'{t}'" for t in _KNOWN_TABLES)
    return (
        "SELECT owner||CHR(9)||COUNT(*) "
        f"FROM all_tables WHERE table_name IN ({names}) "
        "GROUP BY owner ORDER BY COUNT(*) DESC"
    )


def sql_owners() -> str:
    """列出這個帳號看得到的所有 owner + 表數（給人挑 --owner）。"""
    return (
        "SELECT owner||CHR(9)||COUNT(*) "
        "FROM all_tables GROUP BY owner ORDER BY COUNT(*) DESC"
    )


def sql_tables(owner: str) -> str:
    """表清單：表名 / 估列數(num_rows) / 表註解。"""
    o = validate_owner(owner)
    return (
        "SELECT t.table_name||CHR(9)||NVL(TO_CHAR(t.num_rows),'?')||CHR(9)||"
        f"{clean_text_col('c.comments')} "
        "FROM all_tables t "
        "LEFT JOIN all_tab_comments c ON c.owner=t.owner AND c.table_name=t.table_name "
        f"WHERE t.owner='{o}' ORDER BY t.table_name"
    )


def sql_columns(owner: str) -> str:
    """欄位清單（跨全表一次撈）：表名 / 序 / 欄名 / 型別 / 長度 / 可空 / 欄註解。"""
    o = validate_owner(owner)
    return (
        "SELECT c.table_name||CHR(9)||c.column_id||CHR(9)||c.column_name||CHR(9)||"
        "c.data_type||CHR(9)||NVL(TO_CHAR(c.data_length),'')||CHR(9)||c.nullable||CHR(9)||"
        f"{clean_text_col('cc.comments')} "
        "FROM all_tab_columns c "
        "LEFT JOIN all_col_comments cc ON cc.owner=c.owner "
        "AND cc.table_name=c.table_name AND cc.column_name=c.column_name "
        f"WHERE c.owner='{o}' ORDER BY c.table_name, c.column_id"
    )


def sql_constraints(owner: str) -> str:
    """主鍵 / 外鍵：表名 / 型別(P=主鍵,R=外鍵) / 欄位 / 參照的被參照約束名。"""
    o = validate_owner(owner)
    return (
        "SELECT ac.table_name||CHR(9)||ac.constraint_type||CHR(9)||acc.column_name||CHR(9)||"
        "NVL(ac.r_constraint_name,'') "
        "FROM all_constraints ac "
        "JOIN all_cons_columns acc ON acc.owner=ac.owner "
        "AND acc.constraint_name=ac.constraint_name "
        f"WHERE ac.owner='{o}' AND ac.constraint_type IN ('P','R') "
        "ORDER BY ac.table_name, ac.constraint_type, acc.position"
    )


def sql_views(owner: str) -> str:
    """view 清單（view 常藏報表 / 衍生邏輯，是可反推的一部分）。"""
    o = validate_owner(owner)
    return f"SELECT view_name FROM all_views WHERE owner='{o}' ORDER BY view_name"


def sql_logic_inventory(owner: str) -> str:
    """邏輯落點：all_source 按型別數（物件數 / 原始碼行數）。這是方案B 反推天花板的關鍵數字。"""
    o = validate_owner(owner)
    return (
        "SELECT NVL(type,'?')||CHR(9)||COUNT(DISTINCT name)||CHR(9)||COUNT(*) "
        f"FROM all_source WHERE owner='{o}' GROUP BY type ORDER BY COUNT(*) DESC"
    )


def sql_trigger_inventory(owner: str) -> str:
    """trigger 數 + 掛在幾張表上（trigger 常藏寫入時的驗證/過帳邏輯）。"""
    o = validate_owner(owner)
    return (
        "SELECT COUNT(*)||CHR(9)||COUNT(DISTINCT table_name) "
        f"FROM all_triggers WHERE owner='{o}'"
    )


def sql_sequence_count(owner: str) -> str:
    o = validate_owner(owner)
    return f"SELECT COUNT(*) FROM all_sequences WHERE sequence_owner='{o}'"


# ── PL/SQL 原始碼全文擷取（方案B 反推的真正材料，非只有 COUNT）────────────────
# all_source 一行原始碼 = 一 row（有 line 欄），text 尾端含 CHR(10)。**故意不套
# clean_text_col**（它連 CHR(9) 一起剝、會毀掉縮排）：只把換行(CHR10/13)轉空白
# 避免打散 stream 的「一物理行=一列」，保留 tab 縮排（CHR(1) 分隔不與 tab 撞），
# 重組時按 line 排序 join('\n') 即還原。RTRIM 去掉換行轉出的行尾空白。
_SRC_TEXT = "RTRIM(REPLACE(REPLACE(text,CHR(10),' '),CHR(13),''))"
# all_source 一個 schema 動輒上萬行（SC00 ~13,846）> fetch_table 的 5000 cap，
# 必須走全量 stream（不設 ROWNUM cap）。分隔用 CHR(1)（見上）。
_SRC_SEP = "\x01"


def sql_source_stream(owner: str) -> str:
    """all_source 全量串流 SELECT：type / name / line / 該行原始碼（CHR(1) 分隔）。"""
    o = validate_owner(owner)
    return (
        f"SELECT type||CHR(1)||name||CHR(1)||line||CHR(1)||{_SRC_TEXT} "
        f"FROM all_source WHERE owner='{o}' ORDER BY type, name, line"
    )


def extract_source(
    owner: str, *, stream: Callable[[str], object] = ssh_sqlplus_stream
) -> dict[tuple[str, str], str]:
    """串流抓 all_source、按 (type, name) 重組 PL/SQL body。回 {(type, name): body_text}。

    不吃進整表記憶體（走 stream 逐行）；一物件的行按 line 排序後 join('\\n')。
    stream 斷線（SSH Broken pipe，實測 ERP 大串流會偶發）由 ssh_sqlplus_stream
    raise ErpOracleError，呼叫端據此重試/縮範圍——**不吞**，半份 body 比整份缺更危險。
    """
    o = validate_owner(owner)
    by_obj: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for raw in stream(sql_source_stream(o)):
        line = str(raw).rstrip("\n")
        if not line.strip():
            continue
        parts = line.split(_SRC_SEP)
        if len(parts) < 4:
            continue  # 分隔異常的行跳過（不讓半行污染 body）
        typ, name, lineno = parts[0].strip(), parts[1].strip(), parts[2].strip()
        text = _SRC_SEP.join(parts[3:])  # text 本身理論上不含 CHR(1)，保險用 join 復原
        if not typ or not name:
            continue
        by_obj.setdefault((typ, name), []).append((_int(lineno), text))
    return {
        key: "\n".join(t for _, t in sorted(rows, key=lambda x: x[0]))
        for key, rows in by_obj.items()
    }


def _safe_filename(name: str) -> str:
    """物件名→安全檔名（Oracle 名可含 $ # 等；擋路徑穿越/怪字元）。"""
    cleaned = re.sub(r"[^A-Za-z0-9_$#.-]", "_", (name or "").strip())
    return cleaned or "_unnamed"


# all_source 的 type → 副檔名/子目錄（package body 與 spec 分開存）。
_SOURCE_TYPE_DIR = {
    "PACKAGE": "packages",
    "PACKAGE BODY": "package_bodies",
    "PROCEDURE": "procedures",
    "FUNCTION": "functions",
    "TYPE": "types",
    "TYPE BODY": "type_bodies",
    "TRIGGER": "triggers",
    "JAVA SOURCE": "java",
}


def write_source_files(
    bodies: dict[tuple[str, str], str], owner: str, *, out_dir: str | None = None
) -> dict[str, int]:
    """把重組好的 PL/SQL body 寫成每物件一個 .sql（依 type 分子目錄）。

    回各 type 的檔數統計。目錄 `<out_dir>/source/<owner>/<type_dir>/<name>.sql`。
    """
    o = validate_owner(owner)
    base = Path(out_dir or _OUTPUT_DIR) / "source" / o
    counts: dict[str, int] = {}
    for (typ, name), body in bodies.items():
        sub = _SOURCE_TYPE_DIR.get(typ.upper(), _safe_filename(typ.lower()))
        d = base / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{_safe_filename(name)}.sql").write_text(body + "\n", encoding="utf-8")
        counts[typ] = counts.get(typ, 0) + 1
    return counts


def probe_source(
    owner: str | None = None,
    *,
    fetch: Fetch = fetch_table,
    stream: Callable[[str], object] = ssh_sqlplus_stream,
    out_dir: str | None = None,
) -> dict:
    """全量擷取 PL/SQL 原始碼並落檔。回 {owner, objects, files_by_type, total_files}。

    owner=None 時自動偵測飛越核心表所在 schema（同 probe）。這是 probe()（結構/
    COUNT）之外的「深度」擷取，**單獨跑**（stream 大量、斷線風險高，別和 probe 綁死）。
    """
    if owner is None:
        owner = detect_owner(fetch=fetch)
        if not owner:
            raise ErpOracleError(
                "偵測不到飛越核心表(BQ_SE_*)的 schema owner；用 list_owners 看再指定 owner。"
            )
    owner = validate_owner(owner)
    bodies = extract_source(owner, stream=stream)
    counts = write_source_files(bodies, owner, out_dir=out_dir)
    return {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "owner": owner,
        "objects": len(bodies),
        "files_by_type": counts,
        "total_files": sum(counts.values()),
    }


# ── 執行 + 組裝 ────────────────────────────────────────────────────────────
def _int(s: str, default: int = 0) -> int:
    try:
        return int(str(s).strip() or default)
    except (ValueError, TypeError):
        return default


def detect_owner(fetch: Fetch = fetch_table) -> str | None:
    """自動偵測飛越 APP schema 的 owner；查不到回 None。"""
    res = fetch(sql_detect_owner(), ["OWNER", "HITS"], limit=_CAP_SMALL)
    rows = res.get("rows") or []
    return rows[0][0].strip() if rows and rows[0] and rows[0][0].strip() else None


def list_owners(fetch: Fetch = fetch_table) -> list[tuple[str, int]]:
    res = fetch(sql_owners(), ["OWNER", "TABLES"], limit=_CAP_SMALL)
    return [(r[0].strip(), _int(r[1])) for r in (res.get("rows") or []) if r and r[0].strip()]


def probe(owner: str | None = None, *, fetch: Fetch = fetch_table) -> dict:
    """對一個 owner 跑完整 schema + 邏輯落點探勘，回 JSON-able report dict。

    owner=None 時自動偵測飛越核心表所在 schema；偵測不到會 raise（附 --list-owners 提示）。
    """
    if owner is None:
        owner = detect_owner(fetch=fetch)
        if not owner:
            raise ErpOracleError(
                "偵測不到飛越核心表(BQ_SE_*)的 schema owner。"
                "用 --list-owners 看有哪些 owner，再用 --owner 指定。"
            )
    owner = validate_owner(owner)

    # 表 + 註解
    t_res = fetch(sql_tables(owner), ["TABLE", "ROWS", "COMMENT"], limit=_CAP_TABLES)
    tables: dict[str, dict] = {}
    order: list[str] = []
    for r in t_res.get("rows") or []:
        name = (r[0] or "").strip()
        if not name:
            continue
        order.append(name)
        tables[name] = {
            "name": name,
            "est_rows": _int(r[1]) if len(r) > 1 and str(r[1]).strip().isdigit() else None,
            "comment": (r[2].strip() if len(r) > 2 and r[2] else ""),
            "columns": [], "pk": [], "fk": [],
        }

    # 欄位 + 欄註解
    c_res = fetch(sql_columns(owner), ["TABLE", "ID", "COL", "TYPE", "LEN", "NULL", "COMMENT"],
                  limit=_CAP_COLUMNS)
    for r in c_res.get("rows") or []:
        r = list(r) + [""] * (7 - len(r))
        tbl = (r[0] or "").strip()
        if tbl not in tables:
            continue
        tables[tbl]["columns"].append({
            "id": _int(r[1]), "name": (r[2] or "").strip(), "type": (r[3] or "").strip(),
            "length": (r[4] or "").strip(), "nullable": (r[5] or "").strip().upper() == "Y",
            "comment": (r[6] or "").strip(),
        })

    # 主外鍵
    k_res = fetch(sql_constraints(owner), ["TABLE", "TYPE", "COL", "REF"], limit=_CAP_CONSTRAINTS)
    for r in k_res.get("rows") or []:
        r = list(r) + [""] * (4 - len(r))
        tbl, ctype, col, ref = (r[0] or "").strip(), (r[1] or "").strip(), (r[2] or "").strip(), (r[3] or "").strip()
        if tbl not in tables:
            continue
        if ctype == "P":
            tables[tbl]["pk"].append(col)
        elif ctype == "R":
            tables[tbl]["fk"].append({"column": col, "references": ref})

    # views
    v_res = fetch(sql_views(owner), ["VIEW"], limit=_CAP_VIEWS)
    views = [(r[0] or "").strip() for r in (v_res.get("rows") or []) if r and (r[0] or "").strip()]

    # 邏輯落點
    l_res = fetch(sql_logic_inventory(owner), ["TYPE", "OBJECTS", "LINES"], limit=_CAP_SMALL)
    by_type = [{"type": (r[0] or "").strip(), "objects": _int(r[1]), "lines": _int(r[2])}
               for r in (l_res.get("rows") or []) if r and (r[0] or "").strip()]
    tg = fetch(sql_trigger_inventory(owner), ["N", "TABLES"], limit=_CAP_SMALL).get("rows") or [["0", "0"]]
    seq = fetch(sql_sequence_count(owner), ["N"], limit=_CAP_SMALL).get("rows") or [["0"]]

    logic = {
        "by_type": by_type,
        "total_objects": sum(x["objects"] for x in by_type),
        "total_lines": sum(x["lines"] for x in by_type),
        "triggers": _int(tg[0][0]) if tg and tg[0] else 0,
        "trigger_tables": _int(tg[0][1]) if tg and tg[0] and len(tg[0]) > 1 else 0,
        "sequences": _int(seq[0][0]) if seq and seq[0] else 0,
        "views": len(views),
    }

    return {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "owner": owner,
        "truncated": {
            "tables": bool(t_res.get("truncated")),
            "columns": bool(c_res.get("truncated")),
            "constraints": bool(k_res.get("truncated")),
        },
        "summary": {
            "tables": len(order),
            "columns": sum(len(tables[t]["columns"]) for t in order),
            "views": len(views),
            "logic_objects": logic["total_objects"],
            "logic_lines": logic["total_lines"],
            "triggers": logic["triggers"],
        },
        "logic": logic,
        "tables": [tables[t] for t in order],
        "view_names": views,
    }


# ── 邏輯落點判讀（方案B 反推天花板）──────────────────────────────────────────
def logic_verdict(logic: dict) -> str:
    """把 all_source/trigger 計數翻成一句「B 反推可行性」判讀。"""
    lines = logic.get("total_lines", 0)
    triggers = logic.get("triggers", 0)
    if lines == 0 and triggers == 0:
        return ("DB 端幾乎沒有 PL/SQL 邏輯 → 業務規則多半在 Oracle Forms(.fmb/.fmx)，"
                "DB 反推拿不到；方案B 的核心遷移只能靠廠商文件 + 現場訪談 + 影子運行重建，反推天花板低。")
    if lines < 2000:
        return (f"DB 端有少量 PL/SQL({lines} 行 / {triggers} 個 trigger)，但大半邏輯應仍在 Forms；"
                "方案B 反推有部分材料、不完整。")
    return (f"DB 端有可觀 PL/SQL({lines} 行、{logic.get('total_objects', 0)} 個物件、{triggers} 個 trigger) → "
            "相當比例的業務邏輯在資料庫內，方案B 反推有實質材料，值得逐 package 擷取。")


# ── Markdown 輸出 ──────────────────────────────────────────────────────────
def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows]
    return "\n".join([head, sep, *body])


def render_markdown(report: dict) -> str:
    owner = report["owner"]
    s = report["summary"]
    logic = report["logic"]
    out: list[str] = []
    out.append(f"# 飛越 ERP 全 schema 擷取 — schema `{owner}`")
    out.append(f"> 由 `agent_core/erp_schema_probe` 於 {report['generated_at']} 自動產生"
               "（唯讀，走 erp_oracle_client SSH+10g sqlplus）。")
    trunc = [k for k, v in report.get("truncated", {}).items() if v]
    if trunc:
        out.append(f"> ⚠️ 下列查詢達列上限被截斷：{', '.join(trunc)} —— 縮小 owner 範圍或調高 cap 重跑。")
    out.append("")
    out.append(f"**盤點**：{s['tables']} 表 / {s['columns']} 欄 / {s['views']} views；"
               f"邏輯 {s['logic_objects']} 物件 · {s['logic_lines']} 行 · {s['triggers']} triggers。")
    out.append("")

    # 邏輯落點 — 放最前面，因為它決定方案B 可行性
    out.append("## 邏輯落點量測（決定方案B 反推天花板）")
    out.append("")
    out.append(f"**判讀**：{logic_verdict(logic)}")
    out.append("")
    if logic["by_type"]:
        out.append(_md_table(["物件型別", "物件數", "原始碼行數"],
                             [[x["type"], x["objects"], x["lines"]] for x in logic["by_type"]]))
    else:
        out.append("_all_source 查無資料（此帳號可能無權讀，或該 schema 無 PL/SQL）。_")
    out.append("")
    out.append(f"- Triggers：**{logic['triggers']}**（掛在 {logic['trigger_tables']} 張表）")
    out.append(f"- Views：**{logic['views']}**　Sequences：**{logic['sequences']}**")
    out.append("")

    # 表清單
    out.append(f"## 表清單（{s['tables']} 張）")
    out.append("")
    out.append(_md_table(
        ["表", "估列數", "說明"],
        [[t["name"], ("?" if t["est_rows"] is None else t["est_rows"]), t["comment"] or "—"]
         for t in report["tables"]]))
    out.append("")

    # 每表欄位
    out.append("## 欄位明細")
    for t in report["tables"]:
        pk = "、".join(t["pk"]) if t["pk"] else "—"
        out.append("")
        out.append(f"### `{t['name']}`　<sub>主鍵：{pk}</sub>")
        if t["fk"]:
            fks = "；".join(f"{fk['column']}→{fk['references']}" for fk in t["fk"])
            out.append(f"<sub>外鍵：{fks}</sub>")
        out.append("")
        if t["columns"]:
            out.append(_md_table(
                ["#", "欄位", "型別", "長度", "空", "說明"],
                [[c["id"], c["name"], c["type"], c["length"] or "", "Y" if c["nullable"] else "N",
                  c["comment"] or "—"] for c in t["columns"]]))
        else:
            out.append("_（無欄位資料）_")
    out.append("")

    if report["view_names"]:
        out.append(f"## Views（{len(report['view_names'])}）")
        out.append("")
        out.append("\n".join(f"- `{v}`" for v in report["view_names"]))
        out.append("")
    return "\n".join(out)


def write_report(report: dict, *, out_dir: str | None = None) -> tuple[str, str]:
    """把 report 寫成 <owner>.md + <owner>.json（含時間戳一份 + latest 一份）。回 (md_path, json_path)。"""
    base = out_dir or _OUTPUT_DIR
    Path(base).mkdir(parents=True, exist_ok=True)
    owner = report["owner"]
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    md_path = os.path.join(base, f"erp-schema-{owner}-{ts}.md")
    json_path = os.path.join(base, f"erp-schema-{owner}-{ts}.json")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report) + "\n")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    # latest 便於固定路徑取用。原子寫：latest 是被固定路徑消費的（probe 後
    # mirror 決策會讀），torn 的 latest.json 比沒有更糟 —— 時間戳檔撕了一眼
    # 看得出來，latest 撕了會被當成正常輸出讀進去。
    for name, payload in ((f"erp-schema-{owner}-latest.md", render_markdown(report) + "\n"),
                          (f"erp-schema-{owner}-latest.json",
                           json.dumps(report, ensure_ascii=False, indent=2) + "\n")):
        try:
            _atomic_write_text(os.path.join(base, name), payload)
        except OSError:
            pass
    return md_path, json_path


def is_enabled() -> bool:
    """轉呼 erp_oracle_client.is_enabled()，CLI 用來在碰主機前擋掉未啟用的情況。"""
    return _erp.is_enabled()
