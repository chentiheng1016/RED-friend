"""RAG Retrieval / Extraction evaluation framework。

Why：
  之前我改 BM25 / 加 rerank / 做 multi-hop，每次都憑感覺說「看起來更好」，
  但沒數字。下次再改不知道是進步還是回退 → 盲修 → 系統會慢慢回退。

Metrics:
  - recall@k (k=5, 10, 20)：top-k 裡有多少 expected thread_id 命中
  - MRR (Mean Reciprocal Rank)：第一個命中的 1/rank，越高越好
  - citation validity：answer 裡引用的 thread_id 真存在的比例
  - latency / cost：從 cost_tracker 拉
  - (extraction fill rate / safety block rate 另外單獨算)

Golden set:
  var/data/eval/golden_set.json
  [
    {
      "id": "q001",
      "query": "浩筌 LOT03-2024 採購單",
      "expected_thread_ids": ["18f38a2be14453a7"],   # 預期要被抓到的 thread
      "category": "exact_po",                         # 分類：便於子集統計
      "notes": "PO 編號精確匹配 + 客戶名稱"
    },
    ...
  ]

Run report:
  var/data/eval/runs/{timestamp}.json — 完整 run record
  自動跟上一次 run 比對、有退步直接 print 警告
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable

from agent_core.logging_and_paths import logger, DATA_DIR, _atomic_write_text


# 走 DATA_DIR（吃 RED_RUNTIME_DIR）而不是自己拼 REPO_ROOT/var/data，理由同
# cost_tracker._get_cost_log_path。RED_RUNTIME_DIR 沒設時解析結果不變。
_EVAL_DIR = os.path.join(DATA_DIR, "eval")
_GOLDEN_FILE = os.path.join(_EVAL_DIR, "golden_set.json")
_RUNS_DIR = os.path.join(_EVAL_DIR, "runs")


def _ensure_dirs():
    os.makedirs(_EVAL_DIR, exist_ok=True)
    os.makedirs(_RUNS_DIR, exist_ok=True)


# ────────────────────────────────────────────────────────────────────
# Golden set I/O
# ────────────────────────────────────────────────────────────────────
def load_golden_set() -> list:
    """讀 golden set。「檔案不存在」與「檔案損毀」是兩回事，不可摺疊：

    舊版兩者都靜默回 []，於是損毀（torn write / 斷電 / SIGKILL）之後的下一次
    add_to_golden_set 會 load 到空清單、append 一筆、**整份覆寫回去** ——
    人工策展的 golden set 就這樣靜默歸零，連可救援的位元組都沒了。

    現在損毀時：把原檔搬到 .corrupt-<ts> 保全證據（之後的寫入不會蓋掉它）、
    大聲警告、回 []。工具照常可用，資料可人工救回。
    """
    if not os.path.isfile(_GOLDEN_FILE):
        return []
    try:
        with open(_GOLDEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception as e:
        quarantine = f"{_GOLDEN_FILE}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        try:
            os.replace(_GOLDEN_FILE, quarantine)
            logger.error(
                "golden_set.json 損毀（%s）— 原檔已保全到 %s，請人工檢視救回；"
                "本輪視為空集，後續寫入不會蓋掉保全檔", e, quarantine)
        except OSError:
            logger.error("golden_set.json 損毀（%s）且無法搬移保全 — 先別執行 "
                         "add_to_golden_set，會蓋掉損毀原檔", e)
        return []


def save_golden_set(entries: list) -> None:
    # 原子寫（tmp + os.replace）：torn-file 家族 —— token.json 曾因非 atomic
    # 直寫被斷電/併發撞爛讓整艦隊 OAuth 假死；golden set 是人工策展資料，更輸不起。
    _ensure_dirs()
    _atomic_write_text(_GOLDEN_FILE,
                       json.dumps(entries, ensure_ascii=False, indent=2))


def add_to_golden_set(query: str, expected_thread_ids: list[str],
                     category: str = "manual", notes: str = "") -> str:
    """手動加一筆到 golden set。

    Args:
        query: 使用者 query 字串。
        expected_thread_ids: 這筆 query 應該要找到的 thread_id 清單（至少 1 個）。
        category: 分類，例如 'exact_po' / 'alias' / 'time' / 'multi_hop'。
        notes: 這筆的特殊測試點，未來 debug 用。
    """
    entries = load_golden_set()
    new_id = f"q{len(entries) + 1:03d}"
    entry = {
        "id": new_id,
        "query": query.strip(),
        "expected_thread_ids": [tid.strip() for tid in expected_thread_ids if tid.strip()],
        "category": category.strip() or "manual",
        "notes": notes.strip(),
        "added_at": datetime.now().isoformat(timespec="seconds"),
    }
    entries.append(entry)
    save_golden_set(entries)
    return f"✅ 已加入 {new_id}（現有 {len(entries)} 筆 golden queries）"


def list_golden_set(limit: int = 30, category: str = "") -> str:
    """列 golden set 裡的 query。"""
    entries = load_golden_set()
    if not entries:
        return f"(golden set 空的；路徑：{_GOLDEN_FILE})"
    if category.strip():
        entries = [e for e in entries if e.get("category") == category.strip()]
    if not entries:
        return f"沒有 category='{category}' 的 queries"
    lines = [f"📋 Golden set ({len(entries)} 筆)"]
    by_cat = defaultdict(int)
    for e in entries:
        by_cat[e.get("category", "?")] += 1
    lines.append(f"   categories: {dict(by_cat)}")
    lines.append("")
    for e in entries[:limit]:
        lines.append(f"  [{e['id']}] {e.get('category',''):12s}  {e['query'][:70]}")
        lines.append(f"           expect {len(e.get('expected_thread_ids', []))} threads, "
                     f"{e.get('notes', '')[:60]}")
    if len(entries) > limit:
        lines.append(f"  ... 還有 {len(entries) - limit} 筆未顯示")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# Metrics
# ────────────────────────────────────────────────────────────────────
_THREAD_ID_RE = re.compile(r"thread_id=([a-f0-9]{16})", re.IGNORECASE)


def extract_thread_ids_from_output(output: str) -> list[str]:
    """從 recall 的 output 字串抓 thread_id（用我們 citation format）。"""
    if not output:
        return []
    # 依出現順序去重
    seen = []
    for m in _THREAD_ID_RE.finditer(output):
        tid = m.group(1).lower()
        if tid not in seen:
            seen.append(tid)
    return seen


def recall_at_k(retrieved_ids: list[str], expected_ids: list[str], k: int) -> float:
    """top-k 裡找到幾個 expected（佔 expected 多少比例）。"""
    if not expected_ids:
        return 0.0
    top_k = set(retrieved_ids[:k])
    hit = sum(1 for eid in expected_ids if eid.lower() in top_k)
    return hit / len(expected_ids)


def mrr(retrieved_ids: list[str], expected_ids: list[str]) -> float:
    """Mean Reciprocal Rank — 第一個命中 expected 的位置倒數。"""
    expected_lower = {eid.lower() for eid in expected_ids}
    for i, tid in enumerate(retrieved_ids, 1):
        if tid.lower() in expected_lower:
            return 1.0 / i
    return 0.0


def citation_validity(answer_text: str, valid_thread_ids: set) -> tuple[int, int]:
    """從 answer 抽 thread_id citation、查多少真存在。

    Returns: (valid_count, total_citations)
    """
    cited = extract_thread_ids_from_output(answer_text)
    if not cited:
        return (0, 0)
    valid_lower = {tid.lower() for tid in valid_thread_ids}
    valid_count = sum(1 for tid in cited if tid.lower() in valid_lower)
    return (valid_count, len(cited))


# ────────────────────────────────────────────────────────────────────
# Runners — 跑一組 query 收集指標
# ────────────────────────────────────────────────────────────────────
def _load_all_thread_ids_for_validation() -> set:
    """從 chroma 拉所有 thread_id，給 citation validity 查用。"""
    try:
        from agent_core.memory import _get_memory_collection
        col = _get_memory_collection()
        if col is None:
            return set()
        data = col.get(limit=50000)
        return set((data.get("ids") or []))
    except Exception as e:
        logger.warning("load all thread_ids for validation failed: %s", e)
        return set()


def eval_method(method_name: str, method_fn: Callable,
                golden_set: list, valid_tids: set) -> dict:
    """對一個 retrieval 方法跑整個 golden set，算每題 metrics + 彙總。

    Args:
        method_name: 顯示用的名稱，例如 "recall_reranked" / "recall_hybrid_raw"。
        method_fn: function(query) -> output_string（要回傳 recall format 帶 thread_id）。
        golden_set: 已 loaded 的 list。
        valid_tids: 所有 valid thread_id 的 set。
    """
    per_query = []
    agg = {
        "recall@5_sum": 0.0,
        "recall@10_sum": 0.0,
        "recall@20_sum": 0.0,
        "mrr_sum": 0.0,
        "valid_citations": 0,
        "total_citations": 0,
        "latency_ms_sum": 0.0,
        "failures": 0,
    }

    for entry in golden_set:
        q = entry["query"]
        expected = entry.get("expected_thread_ids", [])
        t0 = time.time()
        try:
            output = method_fn(q)
        except Exception as e:
            output = f"❌ 失敗: {e}"
            agg["failures"] += 1
        elapsed = (time.time() - t0) * 1000

        retrieved = extract_thread_ids_from_output(output)
        r5 = recall_at_k(retrieved, expected, 5)
        r10 = recall_at_k(retrieved, expected, 10)
        r20 = recall_at_k(retrieved, expected, 20)
        mrr_v = mrr(retrieved, expected)
        cv_valid, cv_total = citation_validity(output, valid_tids)

        agg["recall@5_sum"] += r5
        agg["recall@10_sum"] += r10
        agg["recall@20_sum"] += r20
        agg["mrr_sum"] += mrr_v
        agg["valid_citations"] += cv_valid
        agg["total_citations"] += cv_total
        agg["latency_ms_sum"] += elapsed

        per_query.append({
            "id": entry["id"],
            "category": entry.get("category", ""),
            "query": q,
            "recall@5": round(r5, 3),
            "recall@10": round(r10, 3),
            "recall@20": round(r20, 3),
            "mrr": round(mrr_v, 3),
            "citations": f"{cv_valid}/{cv_total}",
            "retrieved_ids_sample": retrieved[:5],
            "expected_ids": expected,
            "latency_ms": round(elapsed, 1),
        })

    n = len(golden_set)
    summary = {
        "method": method_name,
        "n_queries": n,
        "failures": agg["failures"],
        "recall@5":  round(agg["recall@5_sum"] / max(1, n), 3),
        "recall@10": round(agg["recall@10_sum"] / max(1, n), 3),
        "recall@20": round(agg["recall@20_sum"] / max(1, n), 3),
        "mrr":       round(agg["mrr_sum"] / max(1, n), 3),
        "citation_validity": (
            round(agg["valid_citations"] / agg["total_citations"], 3)
            if agg["total_citations"] else 0.0
        ),
        "total_citations_in_answers": agg["total_citations"],
        "avg_latency_ms": round(agg["latency_ms_sum"] / max(1, n), 1),
    }
    return {"summary": summary, "per_query": per_query}


def run_eval(methods: str = "",
             category: str = "",
             save_run: bool = True) -> str:
    """跑 eval 套件對照數字，產出報告。

    Args:
        methods: 要測的方法列表，逗號分隔。空字串 = 預設 4 種：
                 "recall_hybrid,recall_reranked,recall_reranked_no_expand,multihop"
                 （multihop 貴，要 ~$0.003/query，小心）
        category: 只跑某個 category 的 queries。空 = 全部。
        save_run: True 則把結果存 runs/{ts}.json 方便後續比較。

    Returns:
        人類可讀報告：每個 method 的 summary + regression alerts。
    """
    _ensure_dirs()
    golden = load_golden_set()
    if category.strip():
        golden = [e for e in golden if e.get("category") == category.strip()]
    if not golden:
        return (
            f"⚠️ golden set 是空的（路徑：{_GOLDEN_FILE}）\n"
            "   先用 add_to_golden_set(...) 加幾筆，或跑 bootstrap_golden_set() 自動產一批。"
        )

    valid_tids = _load_all_thread_ids_for_validation()

    # 準備 methods map
    from agent_core.rerank import recall_reranked
    from agent_core.memory import recall as hybrid_recall

    # method_registry 要跟使用者拿到的真實 default 一致（eval 不該偷偷改參數）。
    # 如果想對比「有擴詞 vs 沒擴詞」效果，用 "_with_expand" 那個明確的變體。
    method_registry = {
        "recall_hybrid": lambda q: hybrid_recall(q, k=20, mode="hybrid"),
        "recall_reranked": lambda q: recall_reranked(q, k=20),  # 用真實 default
        "recall_reranked_with_expand": lambda q: recall_reranked(q, k=20, expand_query=True, auto_time_filter=True),
        "recall_reranked_no_expand": lambda q: recall_reranked(q, k=20, expand_query=False, auto_time_filter=True),
        "multihop": None,  # lazy
    }

    method_list = [m.strip() for m in methods.split(",") if m.strip()] if methods else [
        "recall_hybrid", "recall_reranked",
    ]

    # 如果要 multihop 才 lazy import
    if "multihop" in method_list:
        from agent_core.multihop import multihop_query
        method_registry["multihop"] = lambda q: multihop_query(q, max_hops=3)

    ts = datetime.now().isoformat(timespec="seconds")
    results = {"timestamp": ts, "n_queries": len(golden), "methods": {}}

    lines = [f"📊 RAG Eval Run @ {ts}", f"   Golden set: {len(golden)} queries", ""]

    for m_name in method_list:
        fn = method_registry.get(m_name)
        if fn is None:
            lines.append(f"⚠️  unknown method '{m_name}'，跳過")
            continue
        lines.append(f"▶️ Running {m_name}...")
        try:
            res = eval_method(m_name, fn, golden, valid_tids)
            results["methods"][m_name] = res
            s = res["summary"]
            lines.append(
                f"  {m_name:30s}  "
                f"R@5={s['recall@5']:.2f}  R@10={s['recall@10']:.2f}  R@20={s['recall@20']:.2f}  "
                f"MRR={s['mrr']:.2f}  "
                f"cite={s['citation_validity']:.2f}  "
                f"lat={s['avg_latency_ms']:.0f}ms"
            )
            if s["failures"] > 0:
                lines.append(f"    ⚠️  {s['failures']} failures")
        except Exception as e:
            lines.append(f"  ❌ {m_name} 失敗: {e}")

    # Regression detection
    if save_run:
        run_file = os.path.join(_RUNS_DIR, f"{ts.replace(':','-')}.json")
        with open(run_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        lines.append(f"\n💾 Saved: {run_file}")
        # 寫入後才找前一次：_find_previous_run 取 sorted()[1] 跳過剛寫的當次；
        # 若在寫入前呼叫，files[0] 還是上一次 → 會誤比到 N-2。
        prev_best = _find_previous_run()

        if prev_best:
            lines.append("\n📉 vs previous run:")
            regressions = []
            for m_name, res in results["methods"].items():
                prev = prev_best.get("methods", {}).get(m_name)
                if not prev:
                    continue
                s, ps = res["summary"], prev["summary"]
                for key in ["recall@5", "recall@10", "mrr", "citation_validity"]:
                    if s[key] < ps[key] - 0.02:  # 超過 2% 下降
                        regressions.append(
                            f"  🔴 {m_name}.{key}: {ps[key]:.2f} → {s[key]:.2f}"
                        )
                    elif s[key] > ps[key] + 0.02:
                        lines.append(f"  🟢 {m_name}.{key}: {ps[key]:.2f} → {s[key]:.2f} (+)")
            if regressions:
                lines.append("  --- Regressions ---")
                lines.extend(regressions)

    return "\n".join(lines)


def _find_previous_run() -> dict | None:
    """找最新一次 eval run（不是當次）。"""
    if not os.path.isdir(_RUNS_DIR):
        return None
    files = sorted(os.listdir(_RUNS_DIR), reverse=True)
    if len(files) < 2:
        return None  # 只有當次
    try:
        with open(os.path.join(_RUNS_DIR, files[1]), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def eval_history(last_n: int = 10) -> str:
    """看過去幾次 eval run 的趨勢。"""
    if not os.path.isdir(_RUNS_DIR):
        return "(沒 eval run 紀錄)"
    files = sorted(os.listdir(_RUNS_DIR), reverse=True)[:last_n]
    if not files:
        return "(沒 eval run)"

    lines = [f"📊 最近 {len(files)} 次 eval runs"]
    lines.append("─" * 90)
    lines.append(f"{'timestamp':<22s}  {'method':<28s}  {'R@5':>5s}  {'R@10':>5s}  {'MRR':>5s}  {'cite':>5s}")
    for f in files:
        try:
            with open(os.path.join(_RUNS_DIR, f), "r") as fp:
                run = json.load(fp)
        except Exception:
            continue
        ts = run.get("timestamp", f.replace(".json", ""))
        for m_name, res in (run.get("methods") or {}).items():
            s = res.get("summary", {})
            lines.append(
                f"{ts:<22s}  {m_name:<28s}  "
                f"{s.get('recall@5',0):>5.2f}  {s.get('recall@10',0):>5.2f}  "
                f"{s.get('mrr',0):>5.2f}  {s.get('citation_validity',0):>5.2f}"
            )
    return "\n".join(lines)
