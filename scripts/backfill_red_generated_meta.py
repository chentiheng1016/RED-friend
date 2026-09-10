"""One-off: 把「改動前就寄出去」的小紅自產報表補上 generated_by_red 標記。

為什麼需要這支：X-RED-Generated 是這次才加的信頭，**存量報表沒有**。
gmail_sync 只有在 thread 有新活動時才會重抽，所以既有的報表 chunk 會一直維持
沒有標記的狀態，rag_gateway 的過濾抓不到它們。

⚠️ 這支是**啟發式**的，跟正向路徑（讀信頭，零誤判）性質完全不同。
誤判的方向很不對稱：
  - 漏抓（false negative）＝ 一份舊報表留在 RAG 裡，就是現狀，不會更糟。
  - 誤抓（false positive）＝ 把真人寫的信標成自產 → 它從語意檢索裡**靜默消失**，
    沒有任何錯誤訊息。
所以規則刻意收得很緊，而且預設 dry-run：一定要先看過樣本再 --apply。

判定規則（兩條，取聯集）：
  1. 主旨以「【排程:」開頭 —— daemon_helpers.notify 的格式，只有小紅會產。
  2. 主旨整體長得像「【任務名】」 **而且** 寄件者就是信箱本人（自寄）——
     daemon_dispatcher 走 send_gmail_as(addr, addr, ...) 的自寄報表。
     單看「【】」不夠：同事也會用【】當主旨；一定要疊上自寄條件。

用法：
    python3 scripts/backfill_red_generated_meta.py              # dry-run，只印統計與樣本
    python3 scripts/backfill_red_generated_meta.py --apply      # 真的寫入
    python3 scripts/backfill_red_generated_meta.py --limit 5000 # 只掃前 N 筆（試跑）

只改 metadata，不重新 embed。已經標成 True 的 chunk 會跳過（idempotent）。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from email.utils import parseaddr

os.environ.setdefault("RED_CHROMA_HTTP_URL", "http://127.0.0.1:8000")
# RED_EMBED_DIM 必須在 import vector_store 前設好：沒帶的話開到的是裸名
# 'gmail_threads'（3072 舊索引、空的），production 資料全在 'gmail_threads_768'，
# 掃描會 seen=0 而且不報錯。與其他一次性腳本（reembed_gmail_768 等）同慣例。
os.environ.setdefault("RED_EMBED_DIM", "768")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_core.ingest.vector_store import get_store  # noqa: E402
from agent_core.provenance import METADATA_FIELD, is_red_generated_meta  # noqa: E402

COLLECTION = "gmail_threads"
PAGE = 2000

# notify() 的主旨格式：「【排程: 任務名】」。前綴唯一，單獨成立。
_SCHEDULED_PREFIX = "【排程:"
# dispatcher 的主旨格式：「【任務名】」——必須疊上「自寄」才算數。
_BRACKET_SUBJECT_RE = re.compile(r"^\s*【[^】]{1,80}】\s*$")


def _addr(header_value: str) -> str:
    """從 From 表頭取出純地址（"Name" <a@b.com> → a@b.com）。"""
    return (parseaddr(str(header_value or ""))[1] or "").strip().lower()


def looks_red_generated(meta: dict) -> str:
    """回傳命中的規則名稱；沒命中回空字串。"""
    subject = str(meta.get("subject") or "").strip()
    if not subject:
        return ""
    if subject.startswith(_SCHEDULED_PREFIX):
        return "scheduled_prefix"
    if _BRACKET_SUBJECT_RE.match(subject):
        sender = _addr(meta.get("sender"))
        mailbox = str(meta.get("mailbox_email") or "").strip().lower()
        # 自寄才算：dispatcher 是讓收件人以自己的身分寄給自己。
        if sender and mailbox and sender == mailbox:
            return "self_sent_bracket"
    return ""


def already_marked(meta: dict) -> bool:
    """已經標記過就跳過（idempotent）。判定規則見 agent_core.provenance。"""
    return is_red_generated_meta(meta)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="真的寫入 metadata（預設只 dry-run）")
    ap.add_argument("--limit", type=int, default=0,
                    help="最多掃描幾個 chunk（0＝全部）")
    ap.add_argument("--samples", type=int, default=25,
                    help="dry-run 時列出幾筆樣本主旨供人工檢查")
    args = ap.parse_args()

    store = get_store(COLLECTION)

    def col_get(**kw):
        return store._with_collection(lambda c: c.get(**kw))

    def col_update(**kw):
        return store._with_collection(lambda c: c.update(**kw))

    total = store.count()
    mode = "APPLY（會寫入）" if args.apply else "DRY-RUN（不寫入）"
    print(f"[backfill-generated] {COLLECTION} 總 chunk={total}  模式={mode}", flush=True)

    # 這支是補「存量」報表的標記，掃到空 collection 永遠是設定錯（開錯 dim /
    # 連錯 Chroma），不是合法狀態。不擋的話下面會一路跑完印出 seen=0 matched=0，
    # 看起來跟「沒有東西需要補」一模一樣——連 --apply 都會安靜地成功。
    if total == 0:
        print(
            f"[backfill-generated] ❌ {COLLECTION} 是空的，中止。\n"
            f"    這支是補存量資料的，掃到 0 筆代表開錯 collection 而不是沒事做。\n"
            f"    檢查上面有沒有 [vector_store] 的裸名警告；production 資料在\n"
            f"    'gmail_threads_768'，需要 RED_EMBED_DIM=768（本腳本已預設帶上，\n"
            f"    若外部 env 覆寫成別的值就會開到別的 collection）。\n"
            f"    現行 RED_EMBED_DIM={os.environ.get('RED_EMBED_DIM')!r} "
            f"RED_CHROMA_HTTP_URL={os.environ.get('RED_CHROMA_HTTP_URL')!r}",
            flush=True,
        )
        return 2

    offset = 0
    seen = matched = patched = already = errors = 0
    by_rule: dict[str, int] = {}
    # 以 thread 為單位收樣本，避免同一份報表的多個 chunk 洗版
    samples: dict[str, str] = {}
    t0 = time.time()

    while True:
        if args.limit and seen >= args.limit:
            break
        want = PAGE
        if args.limit:
            want = min(PAGE, args.limit - seen)
        try:
            res = col_get(include=["metadatas"], limit=want, offset=offset)
        except Exception as exc:
            print(f"[backfill-generated] get offset={offset} 失敗: {exc}", flush=True)
            errors += 1
            break
        ids = res.get("ids") or []
        metas = res.get("metadatas") or []
        if not ids:
            break

        up_ids, up_metas = [], []
        for cid, meta in zip(ids, metas):
            seen += 1
            meta = meta or {}
            if already_marked(meta):
                already += 1
                continue
            rule = looks_red_generated(meta)
            if not rule:
                continue
            matched += 1
            by_rule[rule] = by_rule.get(rule, 0) + 1
            doc_id = str(meta.get("doc_id") or cid)
            if len(samples) < args.samples and doc_id not in samples:
                samples[doc_id] = f"[{rule}] {str(meta.get('subject'))[:70]}"
            up_ids.append(cid)
            up_metas.append({**meta, METADATA_FIELD: True})

        if up_ids and args.apply:
            try:
                col_update(ids=up_ids, metadatas=up_metas)
                patched += len(up_ids)
            except Exception as exc:
                print(f"[backfill-generated] update offset={offset} 失敗: {exc}", flush=True)
                errors += 1

        offset += len(ids)
        if offset % (PAGE * 10) == 0 or len(ids) < want:
            rate = seen / max(1e-6, time.time() - t0)
            print(f"[backfill-generated] 進度 {seen}/{total} matched={matched} "
                  f"patched={patched} already={already} errors={errors} {rate:.0f} chunk/s",
                  flush=True)
        if len(ids) < want:
            break

    dt = time.time() - t0
    print(f"[backfill-generated] 掃描完成: seen={seen} matched={matched} "
          f"patched={patched} already={already} errors={errors} 耗時={dt:.0f}s", flush=True)
    print(f"[backfill-generated] 規則分布: {by_rule or '（無命中）'}", flush=True)

    if samples:
        print("[backfill-generated] 樣本（人工檢查用，每個 thread 一筆）：", flush=True)
        for subject in samples.values():
            print(f"    {subject}", flush=True)

    if not args.apply and matched:
        print(
            "[backfill-generated] ⚠️ 這是 dry-run，尚未寫入。\n"
            "    請先確認上面樣本**全部都是小紅自產的報表**——誤標的信會從語意\n"
            "    檢索中靜默消失。確認無誤後再加 --apply 重跑。",
            flush=True,
        )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
