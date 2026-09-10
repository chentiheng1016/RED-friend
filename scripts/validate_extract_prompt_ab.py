"""A/B validate an EXTRACT_PROMPT change offline BEFORE it ships.

內部信 lake 的抽取 prompt 是每晚無人看管跑的 — prompt 改壞了不會當下發現，
是幾週後查 PO timeline 漏東西才察覺。所以改 prompt 的標準流程：

  1. 改 agent_core/internal_emails_extract.py 的 EXTRACT_PROMPT
  2. 跑這支腳本：抓最近 N 串內部信（Gmail 唯讀），BASELINE（改前）與
     CANDIDATE（改後，即 module 現值）對**同一批輸入**各抽一次
  3. 看報告：parse 失敗率、state 漂移/一致率、po_numbers/customers
     Jaccard、新欄位填充率 + 樣本人工抽查 — 沒退步才 merge 部署

不寫 parquet、不碰 chroma；Gemini 呼叫走 cost_tracker（caller 標
prompt_ab_validate）。

Usage:
  .venv/bin/python scripts/validate_extract_prompt_ab.py --n 100 --days-back 21

⚠️ --workers 預設 1：google.genai/grpc 原生庫非 thread-safe，多執行緒並發
打 Gemini 會 SIGTRAP（exit 133）— 正式 ingest 同樣序列跑（parallel_workers=1）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

_REPO_ROOT = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# BASELINE = 改動前的正式 prompt（凍結副本）。下次再改 prompt 時，
# 把當時的正式版貼進來當新 baseline。
BASELINE_PROMPT = """你是公司 email 檔案員。請讀完這封 email (可能是 thread 多則) 後**嚴格照下方 JSON schema** 輸出。

**必須照這個 key 名稱、這個順序輸出：**
{"summary": string, "topic_tags": [string], "state": string, "entities": {"people": [string], "products": [string], "customers": [string], "suppliers": [string], "amounts": [string], "dates_mentioned": [string], "po_numbers": [string], "actions": [string]}}

**欄位定義：**
- summary: 30 字內中文，一句話講主題+結論，不要重複主旨
- topic_tags: 1~3 個短標籤，從「PO確認/樣品催貨/價格異議/櫃位異動/交期延誤/驗貨/對帳/規格變更/付款通知/轉單」挑或自創
- state: 從「進行中、已完成、暫停、取消、僅參考」五選一
- entities.*: 找不到就回 []，**不要腦補**

**真實範例輸出（照抄這個格式）：**
{"summary": "Supremo Kaira 訂單數量加 4 雙確認完成", "topic_tags": ["PO確認", "加訂"], "state": "已完成", "entities": {"people": ["Heinke Lüttig"], "products": ["65L1083024 Kaira-SYMPATEX Atlantic"], "customers": ["Supremo"], "suppliers": [], "amounts": ["4 prs"], "dates_mentioned": [], "po_numbers": ["65L1083024"], "actions": ["已收到 4 雙加訂訂單"]}}

**絕對禁止：**
- 輸出 markdown 圍欄 ```
- 輸出任何解釋文字
- 改 key 名稱 (不要用 subject/body/content 這類)
- 在 entities 外面加新 key

=== 這封 email 的主旨 ===
__SUBJECT__

=== 這封 email 的內容 ===
__BODY__

現在照上面 schema 輸出 JSON（只回 JSON）："""


def _parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON: {text[:120]!r}")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("not dict")
    return parsed


def _extract_with(prompt_template: str, subject: str, body: str, model: str) -> dict | None:
    """One extraction with the given prompt template; 2 attempts, else None.
    兩個 arm 用同一套重試政策，失敗率才有可比性。"""
    from agent_core.gemini_client import _gemini_generate

    prompt = (
        prompt_template
        .replace("__SUBJECT__", (subject or "")[:200])
        .replace("__BODY__", (body or "")[:8000])
    )
    for attempt in range(2):
        try:
            resp = _gemini_generate(model=model, contents=[prompt])
            return _parse_json((resp.text or "").strip())
        except Exception:
            if attempt == 0:
                time.sleep(5)
    return None


def _jaccard(a: list | None, b: list | None) -> float:
    sa, sb = set(a or []), set(b or [])
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--days-back", type=int, default=21)
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()

    from agent_core import internal_emails as ie
    from agent_core import internal_emails_extract as ex

    candidate = ex.EXTRACT_PROMPT
    if candidate == BASELINE_PROMPT:
        print("⚠️ module 的 EXTRACT_PROMPT 跟 BASELINE 一模一樣 — 沒有要驗證的改動。")
        return 1
    model = ex.INGEST_MODEL

    svc = ie.get_service("gmail", "v1")
    ids = ie._fetch_thread_ids(svc, args.days_back, max_threads=max(args.n * 3, 300))
    print(f"[ab] 最近 {args.days_back} 天共 {len(ids)} 串，取最新 {args.n} 串可用的")

    def _one(tid: str) -> dict | None:
        try:
            local = ie.get_service("gmail", "v1")
            thread = local.users().threads().get(userId="me", id=tid, format="full").execute()
            ctx = ex.thread_to_context(thread, header_fn=ie._header)
            if not ctx or ie.should_exclude(ctx["first_sender"], ctx["subject"]):
                return None
            subject, body = ctx["subject"], ctx["combined_text"]
            return {
                "tid": tid,
                "subject": subject,
                "a": _extract_with(BASELINE_PROMPT, subject, body, model),
                # 對照組：baseline 再跑一次 — 用來區分「prompt 造成的漂移」
                # 與「模型本身的隨機性」。實驗組漂移 ≈ 對照組漂移 → prompt 無罪。
                "a2": _extract_with(BASELINE_PROMPT, subject, body, model),
                "b": _extract_with(candidate, subject, body, model),
            }
        except Exception as exc:
            print(f"[ab] thread {tid} 失敗：{type(exc).__name__}: {exc}")
            return None

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for r in pool.map(_one, ids):
            if r is not None:
                rows.append(r)
                if len(rows) % 10 == 0:
                    print(f"[ab] {len(rows)}/{args.n} 串完成")
            if len(rows) >= args.n:
                break

    if not rows:
        print("沒有可用 thread — 檢查 Gmail 授權 / days_back")
        return 1

    # ── metrics ──────────────────────────────────────────────────────
    a_fail = sum(1 for r in rows if r["a"] is None)
    a2_fail = sum(1 for r in rows if r["a2"] is None)
    b_fail = sum(1 for r in rows if r["b"] is None)
    both = [r for r in rows if r["a"] and r["b"]]
    ctrl = [r for r in rows if r["a"] and r["a2"]]

    def ent(p: dict, key: str) -> list:
        return (p.get("entities") or {}).get(key) or []

    def _agree(pairs, x, y, fn):
        vals = [fn(r[x], r[y]) for r in pairs]
        return sum(vals) / max(1, len(vals))

    state_eq = lambda p, q: 1.0 if (p.get("state") or "") == (q.get("state") or "") else 0.0  # noqa: E731
    po_eq = lambda p, q: _jaccard(ent(p, "po_numbers"), ent(q, "po_numbers"))  # noqa: E731
    cu_eq = lambda p, q: _jaccard(ent(p, "customers"), ent(q, "customers"))  # noqa: E731

    sa_dist = Counter((r["a"].get("state") or "?") for r in both)
    sb_dist = Counter((r["b"].get("state") or "?") for r in both)
    a_sum_empty = sum(1 for r in both if not (r["a"].get("summary") or "").strip())
    b_sum_empty = sum(1 for r in both if not (r["b"].get("summary") or "").strip())
    b_promised = [(r["subject"], ent(r["b"], "promised_dates"), ent(r["b"], "dates_mentioned"))
                  for r in both if ent(r["b"], "promised_dates")]
    a_leak = sum(1 for r in both if ent(r["a"], "promised_dates"))

    lines = [
        "=" * 64,
        f"EXTRACT_PROMPT A/B 報告 — {len(rows)} 串（model={model}）",
        "=" * 64,
        f"抽取失敗：baseline {a_fail}　baseline重跑 {a2_fail}　candidate {b_fail}（/{len(rows)}）",
        "",
        "漂移對照（實驗組 vs 對照組 — 兩者相近代表漂移來自模型隨機性，不是 prompt）：",
        f"  state 一致率：  candidate {_agree(both, 'a', 'b', state_eq):.0%}　vs 對照 {_agree(ctrl, 'a', 'a2', state_eq):.0%}",
        f"  po_numbers J：  candidate {_agree(both, 'a', 'b', po_eq):.3f}　vs 對照 {_agree(ctrl, 'a', 'a2', po_eq):.3f}",
        f"  customers  J：  candidate {_agree(both, 'a', 'b', cu_eq):.3f}　vs 對照 {_agree(ctrl, 'a', 'a2', cu_eq):.3f}",
        "  （第二視角 a2↔b — 若 ≈ 對照組數字，prompt 無罪定讞）：",
        f"  state {_agree([r for r in rows if r['a2'] and r['b']], 'a2', 'b', state_eq):.0%} / "
        f"po {_agree([r for r in rows if r['a2'] and r['b']], 'a2', 'b', po_eq):.3f} / "
        f"customers {_agree([r for r in rows if r['a2'] and r['b']], 'a2', 'b', cu_eq):.3f}",
        "",
        f"state 分布 baseline：{dict(sa_dist)}",
        f"state 分布 candidate：{dict(sb_dist)}",
        f"summary 空白：baseline {a_sum_empty}　candidate {b_sum_empty}",
        f"promised_dates 填充：{len(b_promised)}/{len(both)} 串（baseline 不應有：{a_leak} 串有 → 應為 0）",
        "-" * 64,
        "promised_dates 樣本（人工抽查這段）：",
    ]
    for subj, pd_, dm in b_promised[:25]:
        lines.append(f"  ・{subj[:58]}")
        lines.append(f"    promised={pd_}  dates_mentioned={dm}")
    if not b_promised:
        lines.append("  （沒有任何串抽出 promised_dates — 樣本期間可能真的沒有交期承諾，"
                     "建議拉長 --days-back 或加大 --n 再跑）")
    report = "\n".join(lines)
    print(report)
    out = "/tmp/extract_prompt_ab_report.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"\n報告已存 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
