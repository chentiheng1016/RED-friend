"""Email analytics + data-lake tools.

Extracted from agent.py:
    auto-sender patterns, _is_likely_automated)
    _lake_append)
    email_lake_stats, email_lake_rebuild)

email_lake_rebuild calls _classify_email_for_lake which still lives in
agent.py (it's part of the bigger classification subsystem); resolved
via lazy import.

Downstream deps (kept working via agent.py re-export):
  - agent_daemon.A._EMAIL_LAKE_DIR / A._lake_load_df / A._lake_append
  - agent.py's _classify_email_for_lake uses _extract_email_addr
  - prioritized_inbox / quote extractors also use _extract_email_addr
"""
import os
import re
import time
from datetime import datetime, timedelta

from agent_core.email_classify import _classify_email_for_lake
from agent_core.email_utils import _extract_email_addr
from agent_core.google_auth import get_service
from agent_core.logging_and_paths import logger, EMAIL_LAKE_DIR


# ---------- Shared email helpers ----------

def _fmt_duration(ms: float) -> str:
    s = ms / 1000.0
    if s < 60:
        return f"{s:.0f} 秒"
    m = s / 60.0
    if m < 60:
        return f"{m:.0f} 分"
    h = m / 60.0
    if h < 48:
        return f"{h:.1f} 小時"
    return f"{h/24:.1f} 天"


_AUTO_SENDER_DOMAIN_PATTERNS = re.compile(
    r"(bank|billing|bill|statement|notice|notification|newsletter|alert|receipt|"
    r"billrc|ebpp|mailsystem|support|service|admin|system|invoice|"
    r"cathaybk|firstbank|ctbcbank|cathaylife|chunghwa|cht\.com\.tw|gss\.com\.tw)",
    re.I,
)
_AUTO_SUBJECT_PATTERNS = re.compile(
    r"(訂閱通知|對帳單|繳費|扣款|結果通知|自動通知|Unsubscribe|receipt|invoice|"
    r"Billing|Your statement|noreply|do not reply)",
    re.I,
)


def _is_likely_automated(addr: str, subject: str) -> bool:
    if _AUTO_SENDER_DOMAIN_PATTERNS.search(addr):
        return True
    if _AUTO_SUBJECT_PATTERNS.search(subject or ""):
        return True
    return False


# ---------- analyze_email_reply_times (pure local) ----------

def analyze_email_reply_times(days: int = 30, top_n: int = 10, include_automated: bool = False, exclude_own_domain: bool = False):
    """分析過去 N 天 Gmail 對外信件的回信速度（純本機運算，不上雲）。
    - days：分析幾天（1-365）。
    - top_n：列表顯示幾筆。
    - include_automated：預設 False 會自動過濾銀行對帳單/繳費通知/訂閱通知等「不用回」的信。
    - exclude_own_domain：預設 False。若設 True，會額外排除你公司 domain 內部的信（只看外部客戶）。
    回傳：最慢回的寄件者 top N + 還沒回的信（>24h）+ 整體回信速度統計。"""
    try:
        days = max(1, min(int(days), 365))
        top_n = max(3, min(int(top_n), 30))
        service = get_service("gmail", "v1")
        me = (service.users().getProfile(userId="me").execute() or {}).get("emailAddress", "").lower()
        if not me:
            return "錯誤：抓不到你自己的 Gmail 地址。"
        my_domain = me.split("@", 1)[1] if "@" in me else ""
        print(f"[系統日誌] 📊 分析過去 {days} 天回信速度（me={me}, 自動過濾={'關' if include_automated else '開'}）…")
    except Exception as e:
        return f"初始化失敗：{e}"

    try:
        threads = []
        req = service.users().threads().list(userId="me", q=f"newer_than:{days}d", maxResults=500)
        while req is not None and len(threads) < 1000:
            resp = req.execute()
            threads.extend(resp.get("threads") or [])
            req = service.users().threads().list_next(req, resp)
    except Exception as e:
        return f"取 thread 失敗：{e}"
    if not threads:
        return f"過去 {days} 天沒有任何 email thread。"

    pair_deltas = []
    pending = []
    now_ms = int(time.time() * 1000)

    noreply_pat = re.compile(r"(noreply|no-reply|donotreply|do-not-reply|bounce|mailer-daemon|postmaster)", re.I)

    def _header(msg, name):
        for h in (msg.get("payload", {}).get("headers") or []):
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
        return ""

    for t in threads:
        try:
            full = service.users().threads().get(userId="me", id=t["id"], format="metadata",
                                                   metadataHeaders=["From", "Subject", "Date", "List-Unsubscribe"]).execute()
            msgs = full.get("messages") or []
        except Exception:
            continue
        if len(msgs) < 1:
            continue

        msgs.sort(key=lambda m: int(m.get("internalDate", 0)))

        last_external_ts = None
        last_external_from = None
        last_external_subject = None
        for m in msgs:
            from_h = _header(m, "From")
            sender_addr = _extract_email_addr(from_h)
            subject = _header(m, "Subject")
            ts = int(m.get("internalDate", 0))
            is_list = bool(_header(m, "List-Unsubscribe"))
            is_noreply = bool(noreply_pat.search(sender_addr)) if sender_addr else False

            if sender_addr == me:
                if last_external_ts is not None:
                    delta = ts - last_external_ts
                    if delta >= 0:
                        pair_deltas.append((last_external_from, delta, last_external_subject))
                    last_external_ts = None
            else:
                if is_list or is_noreply or not sender_addr:
                    continue
                if (not include_automated) and _is_likely_automated(sender_addr, subject):
                    continue
                if exclude_own_domain and my_domain and sender_addr.endswith("@" + my_domain):
                    continue
                last_external_ts = ts
                last_external_from = sender_addr
                last_external_subject = subject

        if last_external_ts is not None:
            waited_ms = now_ms - last_external_ts
            if waited_ms > 24 * 3600 * 1000:
                pending.append((last_external_from, last_external_ts, last_external_subject, waited_ms))

    from collections import defaultdict
    bucket = defaultdict(list)
    for addr, ms, subj in pair_deltas:
        bucket[addr].append((ms, subj))

    summary = []
    for addr, items in bucket.items():
        deltas = [it[0] for it in items]
        deltas.sort()
        n = len(deltas)
        median = deltas[n // 2]
        avg = sum(deltas) / n
        slowest_ms, slowest_subj = max(items, key=lambda x: x[0])
        summary.append({
            "addr": addr, "count": n,
            "median_ms": median, "avg_ms": avg,
            "slowest_ms": slowest_ms, "slowest_subj": slowest_subj,
        })

    slow = sorted([s for s in summary if s["count"] >= 2],
                  key=lambda s: s["median_ms"], reverse=True)[:top_n]
    if not slow:
        slow = sorted(summary, key=lambda s: s["median_ms"], reverse=True)[:top_n]

    pending.sort(key=lambda x: x[3], reverse=True)

    all_deltas = [d for _, d, _ in pair_deltas]
    if all_deltas:
        all_deltas_sorted = sorted(all_deltas)
        median_all = all_deltas_sorted[len(all_deltas_sorted) // 2]
        avg_all = sum(all_deltas) / len(all_deltas)
        fastest = min(all_deltas)
        slowest_overall = max(all_deltas)
    else:
        median_all = avg_all = fastest = slowest_overall = 0

    lines = []
    lines.append(f"📊 過去 {days} 天 Email 回信分析（{len(threads)} 個 thread / {len(pair_deltas)} 次回信）")
    lines.append("")
    lines.append("【整體】")
    lines.append(f"  中位數回信時間：{_fmt_duration(median_all)}")
    lines.append(f"  平均回信時間：  {_fmt_duration(avg_all)}")
    lines.append(f"  最快一次：{_fmt_duration(fastest)}  最慢一次：{_fmt_duration(slowest_overall)}")
    lines.append("")
    lines.append(f"【🐌 最慢回的 {len(slow)} 個寄件者（中位數，互動 ≥2 次優先）】")
    if slow:
        for i, s in enumerate(slow, 1):
            lines.append(f"  {i}. {s['addr']}  {s['count']} 次  中位數 {_fmt_duration(s['median_ms'])}  "
                         f"(最慢：{_fmt_duration(s['slowest_ms'])}「{(s['slowest_subj'] or '')[:40]}」)")
    else:
        lines.append("  （沒有資料）")
    lines.append("")
    lines.append(f"【⏰ 還沒回的外來信（> 24 小時）{len(pending)} 封，按等候時間排】")
    if pending:
        for i, (addr, ts, subj, waited) in enumerate(pending[:top_n], 1):
            date_str = datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M")
            lines.append(f"  {i}. 等 {_fmt_duration(waited)}  {addr}  [{date_str}]  「{(subj or '')[:50]}」")
    else:
        lines.append("  🎉 沒有過期未回的外來信")
    return "\n".join(lines)


# ---------- Email Data Lake ----------

_EMAIL_LAKE_DIR = EMAIL_LAKE_DIR
_EMAIL_LAKE_PARQUET = os.path.join(_EMAIL_LAKE_DIR, "emails_master.parquet")
_EMAIL_LAKE_STATE_FILE = os.path.join(_EMAIL_LAKE_DIR, "last_sync.json")


def _lake_load_df():
    """讀現有 parquet 為 DataFrame，不存在就回空的。"""
    try:
        import pandas as pd
    except ImportError:
        return None
    if not os.path.exists(_EMAIL_LAKE_PARQUET):
        return pd.DataFrame()
    try:
        return pd.read_parquet(_EMAIL_LAKE_PARQUET)
    except Exception as e:
        logger.warning("lake parquet 讀取失敗: %s", e)
        return pd.DataFrame()


def _lake_append(rows: list):
    """把 rows（list of dict）append 到 Parquet。會自動 dedup by message_id。"""
    if not rows:
        return 0
    try:
        import pandas as pd
    except ImportError:
        return 0
    os.makedirs(_EMAIL_LAKE_DIR, exist_ok=True)
    new_df = pd.DataFrame(rows)
    old_df = _lake_load_df()
    if old_df is not None and len(old_df):
        # Schema-drift defence: classify_email evolves over time and the
        # parquet on disk may carry columns the new rows don't (or vice
        # versa). Two pitfalls and how this code avoids them:
        #
        #   1. Column order can flip between runs because pandas concat's
        #      "union of columns" order depends on which side appears
        #      first. Codify the order via column_order + reindex so the
        #      parquet schema is stable for downstream consumers.
        #
        #   2. dtype preservation across all-NA columns. We drop entirely-empty
        #      columns from each side BEFORE concat so pandas never infers a
        #      dtype from an all-NA column — that's the FutureWarning path, and
        #      once pandas flips it a typed-but-empty column would silently
        #      degrade to object and break typed downstream filters. We then
        #      reindex back to the full union and RESTORE each now-empty
        #      column's original typed dtype. The restore is exactly what an
        #      earlier 'just dropna' attempt was missing (Codex review on PR
        #      #32): dropna alone loses the typing; dropna + restore keeps it
        #      and no longer depends on the deprecated concat behaviour.
        column_order = list(dict.fromkeys(list(old_df.columns) + list(new_df.columns)))
        merged = pd.concat(
            [old_df.dropna(axis=1, how="all"), new_df.dropna(axis=1, how="all")],
            ignore_index=True,
        )
        merged = merged.reindex(columns=column_order)
        merged = merged.drop_duplicates(subset=["message_id"], keep="last")
        # Restore typed dtypes for columns that ended up entirely empty so a
        # legacy 'typed but never populated' column doesn't become object.
        # Columns that carry real values keep concat's natural upcast.
        for col in merged.columns:
            if not merged[col].isna().all():
                continue
            for src in (old_df, new_df):
                if col in src.columns and str(src[col].dtype) != "object":
                    try:
                        merged[col] = merged[col].astype(src[col].dtype)
                    except (ValueError, TypeError):
                        pass
                    break
    else:
        merged = new_df
    merged.to_parquet(_EMAIL_LAKE_PARQUET, index=False)
    return len(merged)


def query_email_lake(dept: str = "", doc_type: str = "", entity: str = "",
                       days: int = 30, limit: int = 20):
    """查 Email Data Lake。所有參數 optional。
    - dept: 部門（業務/採購/生產/品管/樣品/物流/財務/人資/行政/it_system/external）
    - doc_type: 文件類型（customer_po/quote_out/invoice 等 38 種）
    - entity: 客戶/供應商名稱（模糊匹配）
    - days: 只看最近 N 天，0 = 不限
    - limit: 最多回幾筆"""
    df = _lake_load_df()
    if df is None:
        return "錯誤：缺 pandas 或 pyarrow"
    if df.empty:
        return "Email Data Lake 還沒建立。請跑 email_lake_rebuild(days_back=30) 初次建立，或等 daemon 自動累積。"
    # This is an LLM-callable tool: days/limit can arrive as a non-int string
    # ("30天") or None. Coerce defensively and fall back to the documented
    # defaults rather than ValueError-ing the whole query.
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    # Guard every column access: a schema-drifted/legacy parquet missing one of
    # these columns must degrade gracefully (skip that filter) rather than
    # KeyError the whole query — the same drift the render loop below hardens
    # against via _cell(). A requested filter on an absent column is simply
    # skipped (the data can't carry it anyway).
    mask = df.index == df.index
    if dept.strip() and "dept" in df.columns:
        mask &= df["dept"] == dept.strip()
    if doc_type.strip() and "doc_type" in df.columns:
        mask &= df["doc_type"] == doc_type.strip()
    if entity.strip() and "entity" in df.columns:
        mask &= df["entity"].astype(str).str.contains(entity, case=False, na=False)
    if days > 0 and "date" in df.columns:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        mask &= df["date"].astype(str) >= cutoff
    sub = df[mask]
    if "date" in sub.columns:
        sub = sub.sort_values("date", ascending=False)
    sub = sub.head(limit)
    if sub.empty:
        return f"0 筆符合（dept={dept!r} doc_type={doc_type!r} entity={entity!r} days={days}）"
    import pandas as pd

    def _cell(value) -> str:
        # NaN/None → "" so missing parquet cells don't render as the literal
        # string "nan", and so subject[:60] can't blow up on a float NaN.
        return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)

    lines = [f"🔍 找到 {len(sub)} 筆（共 {len(df)} 筆，過濾後 {mask.sum()}）"]
    for _, r in sub.iterrows():
        amount = r.get("amount")
        amt = f"{amount} {_cell(r.get('currency'))}".strip() if pd.notna(amount) and amount else ""
        lines.append(
            f"  [{_cell(r.get('date'))}] [{_cell(r.get('dept'))}/{_cell(r.get('doc_type'))}] "
            f"{_cell(r.get('entity'))} — {_cell(r.get('summary'))}"
        )
        if amt:
            lines.append(f"      💰 {amt}")
        lines.append(
            f"      from: {_cell(r.get('sender'))}  "
            f"subject: {_cell(r.get('subject'))[:60]}  id={_cell(r.get('message_id'))}"
        )
    return "\n".join(lines)


def email_lake_stats():
    """Email Data Lake 總覽：總筆數 + dept/doc_type 分佈 + 最近活躍實體。"""
    df = _lake_load_df()
    if df is None:
        return "錯誤：缺 pandas / pyarrow"
    if df.empty:
        return "Lake 空的。跑 email_lake_rebuild() 或等 daemon 自動累積。"
    from collections import Counter
    lines = [f"📊 Email Data Lake：共 {len(df)} 筆"]
    if "date" in df.columns and not df["date"].empty:
        dmin = df["date"].astype(str).min()
        dmax = df["date"].astype(str).max()
        lines.append(f"  時間範圍：{dmin} ~ {dmax}")
    lines.append("\n部門分佈 top 5：")
    for d, n in (Counter(df["dept"].dropna().astype(str)) if "dept" in df.columns else Counter()).most_common(5):
        lines.append(f"  {d:12s} {n}")
    lines.append("\n文件類型分佈 top 8：")
    for t, n in (Counter(df["doc_type"].dropna().astype(str)) if "doc_type" in df.columns else Counter()).most_common(8):
        lines.append(f"  {t:25s} {n}")
    lines.append("\n最常出現實體 top 10：")
    for e, n in Counter(df["entity"].dropna().astype(str)).most_common(10):
        if e and e != "":
            lines.append(f"  {e:30s} {n}")
    return "\n".join(lines)


def email_lake_rebuild(days_back: int = 30, max_emails: int = 200):
    """批次處理過去 N 天的信進 lake（dedup by message_id）。
    - days_back：往前幾天（上限 1825=5 年）
    - max_emails：單次最多處理幾封（避免一次燒太多 Gemini 配額）
    每 20 封存一次 Parquet，中途 Ctrl+C 不會前功盡棄。"""
    try:
        days_back = max(1, min(int(days_back), 1825))
        max_emails = max(1, min(int(max_emails), 2000))
        service = get_service("gmail", "v1")
    except Exception as e:
        return f"初始化失敗：{e}"

    df = _lake_load_df()
    existing_ids = set(df["message_id"].tolist()) if df is not None and not df.empty else set()

    q = f"newer_than:{days_back}d"
    all_ids = []
    try:
        req = service.users().messages().list(userId="me", q=q, maxResults=500)
        while req is not None and len(all_ids) < max_emails * 3:
            resp = req.execute()
            for m in resp.get("messages", []) or []:
                mid = m["id"]
                if mid not in existing_ids:
                    all_ids.append(mid)
                if len(all_ids) >= max_emails:
                    break
            if len(all_ids) >= max_emails:
                break
            req = service.users().messages().list_next(req, resp)
    except Exception as e:
        return f"列信失敗：{e}"

    if not all_ids:
        return f"過去 {days_back} 天沒有未入 lake 的新信（已處理 {len(existing_ids)} 筆）。"
    todo = all_ids[:max_emails]
    print(f"[email lake] 開始處理 {len(todo)} 封（跳過 {len(existing_ids)} 筆已在 lake）")

    stats = {"ok": 0, "skipped": 0, "fail": 0}
    batch = []
    for i, mid in enumerate(todo, 1):
        result = _classify_email_for_lake(mid)
        if result is None:
            stats["fail"] += 1
        elif result.get("skipped"):
            stats["skipped"] += 1
        else:
            batch.append(result)
            stats["ok"] += 1

        if i % 10 == 0 or i == len(todo):
            print(f"[email lake] 進度 {i}/{len(todo)}  OK={stats['ok']} 跳過={stats['skipped']} 失敗={stats['fail']}")

        if len(batch) >= 20 or i == len(todo):
            if batch:
                total = _lake_append(batch)
                print(f"[email lake] 已寫入，Parquet 目前共 {total} 筆")
                batch = []

    return (f"✅ Lake 建完：處理 {len(todo)} 封\n"
            f"   入 lake：{stats['ok']}  敏感過濾：{stats['skipped']}  失敗：{stats['fail']}\n"
            f"   Parquet：{_EMAIL_LAKE_PARQUET}")
