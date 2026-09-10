"""Contextual Retrieval Phase 2：gmail 離線 A/B eval。

問題：LLM 生成的文件級 context 前綴，比現行靜態 [subject] 前綴，召回更好嗎？

設計（簡化 A/B，隔離唯一變數＝前綴）：
- 從 live gmail_threads_768 撈 N 個 thread 的 chunks（現況都是舊 [subject] 前綴）。
- 每 chunk 剝掉 [subject] 前綴得 payload；A/B 用**完全相同的 payload**，只換前綴：
    A = 現況「[subject] payload」（baseline）
    B = 「<LLM 文件級 context>\n\n payload」（contextual）
  → chunk 一一對應、payload 逐字相同，唯一變數是前綴文字。
- 每 thread 用 LLM 產一個「需要文件級脈絡才好答」的搜尋式問句，expected=該 thread。
- 兩組各自 embed（gemini-embedding-001, 768, 已 L2 正規化），純記憶體 cosine top-k
  檢索 → thread，算 recall@5/10/20 + MRR，比 A vs B。
- **不寫 chroma**（純讀 + 記憶體向量），不搶夜跑 single-writer。

用法：.venv/bin/python ctx_ab_eval.py [N]
"""
import os
import random
import sys

os.environ.setdefault("RED_CHROMA_HTTP_URL", "http://127.0.0.1:8000")
os.environ.setdefault("RED_EMBED_DIM", "768")
os.environ.setdefault("AGENT_DAEMON_MODE", "1")
os.environ["RAG_CONTEXTUAL_RETRIEVAL"] = "1"  # 讓 gen_doc_context 真的生成

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np  # noqa: E402

from agent_core.ingest.vector_store import _gemini_embed, get_store  # noqa: E402
from agent_core.ingest.contextualize import gen_doc_context  # noqa: E402
from agent_core.gemini_client import _gemini_generate  # noqa: E402

POOL = int(sys.argv[1]) if len(sys.argv) > 1 else 200   # distractor corpus 大小
NQ = int(sys.argv[2]) if len(sys.argv) > 2 else 50       # 產幾個 query
MIN_BODY = 200      # 太短的 thread（簽名檔/一行回覆）跳過
MAX_CHUNKS = 3      # 每 thread 最多取幾 chunk（控制 embedding 量）
SEED = 20260719

# 語意化 query：故意**不放**確切單號/型號/精確關鍵字，只描述情境/問題/主題。
# 這才測到 contextual 的主場——靠語意而非關鍵字精確匹配；若 chunk 沒有文件級
# 脈絡（客戶/專案），這種 query 就難命中，正是 context 前綴要補的。
_QUERY_PROMPT = """下面是一封公司內部郵件（主旨＋正文）。請站在「幾週後有同事\
只**大概記得**這件事、想找回這封信」的角度，產生**一個**繁體中文搜尋問句。要求：
- **不要**放確切的訂單號/LOT號/型號/料號等精確代碼——模擬「只記得情境、忘了確切編號」。
- 用情境、主題、問題本身來描述（例：「迪卡儂那批布重超標怎麼處理」而非「LOT211 PU467 布重」）。
- 像真的在搜尋，別照抄原文整句、別用「這封信」這種指代。
- 只回那一句問句，不要開場白、不要引號。

主旨：{subject}

正文：
{body}

搜尋問句："""


def _strip_prefix(doc: str, subject: str) -> str:
    prefix = f"[{(subject or '')[:80]}] "
    return doc[len(prefix):] if doc.startswith(prefix) else doc


def _gen_query(subject: str, body: str) -> str:
    prompt = _QUERY_PROMPT.format(subject=subject or "(無主旨)", body=body[:4000])
    resp = _gemini_generate(model="gemini-2.5-flash", contents=[prompt])
    out = (resp.text if hasattr(resp, "text") else str(resp)) or ""
    return " ".join(out.split()).strip().strip("`").strip()


def main() -> int:
    store = get_store("gmail_threads")
    all_tids = sorted(store.list_doc_ids())
    print(f"gmail_threads 共 {len(all_tids)} threads；corpus={POOL} query={NQ}", flush=True)
    random.seed(SEED)
    sample = random.sample(all_tids, min(POOL, len(all_tids)))

    # ── 撈 chunks、剝前綴、組 A/B chunk、生成 context + query ──
    threads = []  # {tid, subject, payloads}
    skipped = 0
    for tid in sample:
        res = store._with_collection(lambda c, t=tid: c.get(
            where={"doc_id": {"$eq": t}}, include=["documents", "metadatas"]))
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        if not docs:
            skipped += 1
            continue
        subject = str((metas[0] or {}).get("subject", ""))
        payloads = [_strip_prefix(d, subject) for d in docs][:MAX_CHUNKS]
        body = "\n".join(payloads)
        if len(body) < MIN_BODY:
            skipped += 1
            continue
        threads.append({"tid": tid, "subject": subject, "payloads": payloads, "body": body})

    print(f"有效 threads {len(threads)}（跳過 {skipped}：無 chunk 或太短）", flush=True)
    if len(threads) < 10:
        print("樣本太少，放棄。", flush=True)
        return 1

    # ── 生成 context（B 組前綴）+ query ──
    print("生成 context + query（LLM）…", flush=True)
    a_chunks, b_chunks, chunk_tid = [], [], []
    queries, q_expected = [], []
    ctx_ok = 0
    for i, t in enumerate(threads):
        ctx = gen_doc_context(t["body"], title=t["subject"], source="gmail")
        if ctx:
            ctx_ok += 1
        else:
            ctx = f"[{t['subject'][:80]}]"  # 生成失敗 fallback（B 退化成近似 A，公平計入）
        for p in t["payloads"]:
            a_chunks.append(f"[{t['subject'][:80]}] {p}")
            b_chunks.append(f"{ctx}\n\n{p}")
            chunk_tid.append(t["tid"])
        # 只對前 NQ 個 thread 產 query（其餘純當 distractor corpus）
        if i < NQ:
            try:
                q = _gen_query(t["subject"], t["body"])
            except Exception as e:  # noqa: BLE001
                q = ""
                print(f"  query 生成失敗 {t['tid']}: {e}", flush=True)
            if q:
                queries.append(q)
                q_expected.append(t["tid"])
        if (i + 1) % 40 == 0:
            print(f"  …{i + 1}/{len(threads)}", flush=True)

    print(f"context 生成成功 {ctx_ok}/{len(threads)}；query {len(queries)}；"
          f"chunks A={len(a_chunks)} B={len(b_chunks)}", flush=True)

    # ── embedding（分批）──
    def embed_all(texts, task):
        out = []
        for i in range(0, len(texts), 100):
            out.extend(_gemini_embed(texts[i:i + 100], task))
        return np.asarray(out, dtype=np.float32)

    print("embed A / B / queries …", flush=True)
    A = embed_all(a_chunks, "RETRIEVAL_DOCUMENT")
    B = embed_all(b_chunks, "RETRIEVAL_DOCUMENT")
    Q = embed_all(queries, "RETRIEVAL_QUERY")
    chunk_tid = np.array(chunk_tid)

    # ── 檢索 + metrics（向量已正規化，cosine = dot）──
    def eval_corpus(mat):
        sums = {5: 0.0, 10: 0.0, 20: 0.0}
        mrr = 0.0
        sims_all = Q @ mat.T  # (n_query, n_chunk)
        for qi in range(len(queries)):
            sims = sims_all[qi]
            order = np.argsort(-sims)
            # chunk→thread 去重，保留每 thread 最高分的名次
            seen, ranked = set(), []
            for idx in order:
                tid = chunk_tid[idx]
                if tid in seen:
                    continue
                seen.add(tid)
                ranked.append(tid)
            exp = q_expected[qi]
            for k in (5, 10, 20):
                if exp in ranked[:k]:
                    sums[k] += 1.0
            if exp in ranked:
                mrr += 1.0 / (ranked.index(exp) + 1)
        n = len(queries)
        return {f"recall@{k}": round(sums[k] / n, 3) for k in (5, 10, 20)} | {
            "mrr": round(mrr / n, 3)}

    a_m, b_m = eval_corpus(A), eval_corpus(B)
    print("\n" + "=" * 56)
    print(f"A/B eval — {len(queries)} queries, {len(threads)} threads "
          f"（distractor pool = 全樣本 threads）")
    print("=" * 56)
    print(f"{'metric':12s} {'A=[subject]':>14s} {'B=context':>14s} {'Δ':>10s}")
    for k in ("recall@5", "recall@10", "recall@20", "mrr"):
        d = b_m[k] - a_m[k]
        print(f"{k:12s} {a_m[k]:>14.3f} {b_m[k]:>14.3f} {d:>+10.3f}")
    print("=" * 56)
    print("\n樣本 query（前 5）：")
    for q, e in list(zip(queries, q_expected))[:5]:
        print(f"  · {q}  [expect {e}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
