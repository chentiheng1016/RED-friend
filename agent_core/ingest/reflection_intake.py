"""反思層的「近期新進 doc」追蹤（intake ledger）。

反思（agent_core.reflection）要知道「過去 48 小時哪些文件剛進 RAG」。不能用
Chroma metadata range filter 反查——`synced_at` 是 ISO 字串，chromadb 對字串
metadata 只支援 $eq/$ne/$in/$nin，range 查詢會退化成全庫掃描（34GB/580 萬
chunk 級別）。改成 ingest 寫入當下順手記一筆，反思直接讀這份清單。

格式：append-only JSONL（`var/data/reflection_intake.jsonl`），每次
upsert_batch 一行 `{"ts", "c": collection, "docs": [{"d": doc_id, "g": group,
"t": title}...]}`（chunk 已去重成 doc 粒度）。選 JSONL 而非 locked_json：
夜跑一晚呼叫 upsert_batch 數千次，read-modify-write 整份 JSON 是 O(n²) I/O；
append 一行是 O(1)。舊資料由反思跑完後 prune()（單一慢讀者重寫，無競爭）。

⚠️ 這是夜跑熱路徑上的 hook：任何失敗都只准吞掉（drop 一筆 intake 記錄的代價
是「該文件這輪不被反思」，遠小於讓 7-15 小時的 sync 炸掉）。
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from agent_core.env_utils import env_bool, env_int
from agent_core.logging_and_paths import DATA_DIR, logger
from agent_core.provenance import is_red_generated_meta

INTAKE_FILE = os.path.join(DATA_DIR, "reflection_intake.jsonl")

# 只追蹤這三個來源 collection；反思產物自己（xiaohong_reflections）與
# 其他 collection（memory / sop）不追蹤，避免反思讀到自己的輸出。
_TRACKED_COLLECTIONS = frozenset({"drive_docs", "gmail_threads", "google_chat_messages"})

# 單行 docs 上限：一個 upsert_batch 正常是單一文件的幾十個 chunk（去重後
# 1-2 個 doc）；gmail 批次會多一些。超過就截斷 + 記 dropped，反正反思每組
# 也只取樣前 N 篇。
_MAX_DOCS_PER_LINE = 200


def _group_key(collection: str, meta: dict[str, Any]) -> str:
    """分組鍵：反思按「同來源、同容器」聚合。chat 用 space、drive 用共用硬碟、
    gmail 用信箱。取不到就落 ''（反思端會歸進 misc 組）。"""
    meta = meta or {}
    if collection == "google_chat_messages":
        return str(meta.get("space_name") or "")
    if collection == "drive_docs":
        return str(meta.get("drive_id") or meta.get("folder_id") or "")
    if collection == "gmail_threads":
        return str(meta.get("mailbox_email") or "")
    return ""


def record_batch(collection: str, chunk_ids: list, metadatas: list) -> None:
    """upsert_batch 成功後呼叫。絕不 raise、絕不明顯拖慢夜跑。

    env kill-switch：RED_REFLECTION_INTAKE=0 整個停用（夜跑出狀況時的安全閥）。
    """
    try:
        if not env_bool("RED_REFLECTION_INTAKE", True):
            return
        if collection not in _TRACKED_COLLECTIONS:
            return
        if not chunk_ids:
            return
        # chunk → doc 去重；每個 doc 留前 3 個 chunk id（文件開頭語境最完整，
        # 反思端直接 get_by_ids 撈原文，不用再對 metadata 反查）。
        seen: dict[str, dict[str, Any]] = {}
        for i, cid in enumerate(chunk_ids):
            meta = (metadatas[i] if i < len(metadatas) else None) or {}
            doc_id = str(meta.get("doc_id") or "")
            if not doc_id:
                continue
            # 小紅自產的內容（排程報表等）不進反思清單：反思會把它當「剛進來的
            # 新文件」讀，等於反思自己上一輪寫的東西。這跟上面刻意不追蹤
            # xiaohong_reflections 是同一個理由，只是這條從 gmail 線繞進來。
            if is_red_generated_meta(meta):
                continue
            entry = seen.get(doc_id)
            if entry is None:
                seen[doc_id] = {
                    "d": doc_id,
                    "g": _group_key(collection, meta),
                    # gmail 線的標題欄位是 subject（不是 title）——三條線欄位名
                    # 都要涵蓋，否則反思洞察的來源引用會變成不可讀的 thread id
                    "t": str(
                        meta.get("title") or meta.get("subject")
                        or meta.get("display_name") or ""
                    )[:120],
                    "ids": [str(cid)],
                }
            elif len(entry["ids"]) < 3:
                entry["ids"].append(str(cid))
        if not seen:
            return
        docs = list(seen.values())
        dropped = 0
        if len(docs) > _MAX_DOCS_PER_LINE:
            dropped = len(docs) - _MAX_DOCS_PER_LINE
            docs = docs[:_MAX_DOCS_PER_LINE]
        line = json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "c": collection,
            "docs": docs,
            **({"dropped": dropped} if dropped else {}),
        }, ensure_ascii=False)
        os.makedirs(DATA_DIR, exist_ok=True)
        # O_APPEND + flock 保護寫入。兩個紀律：
        # 1. flush 必須在解鎖**之前**——Python text-mode 檔案是雙層緩衝，
        #    不 flush 的話實際落盤發生在 with 區塊 close 時（鎖已放），
        #    flock 等於裝飾；prune 的改寫還可能蓋掉這種「解鎖後才落盤」
        #    的行（審查實測復現過）。
        # 2. LOCK_NB 非阻塞——這是夜跑熱路徑，prune 持鎖改寫的瞬間寧可
        #    丟這一筆 intake（該文件這輪不被反思），也不准 block 同步流程。
        with open(INTAKE_FILE, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fh.write(line + "\n")
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception as exc:
        # 熱路徑鐵則：intake 失敗只准吞。debug 級——夜跑 log 不要被刷爆。
        logger.debug("reflection intake 記錄失敗（略過）：%s", exc)


def read_recent(window_days: int = 2) -> list[dict[str, Any]]:
    """讀近 N 天的 intake 行（解析失敗的行直接跳過）。回傳原始行 dict 清單。

    errors="replace"：撕裂寫入/斷電可能在檔尾留半個 UTF-8 多位元組字（標題
    是中文），不 replace 的話 decode 會在 for 迭代裡炸 UnicodeDecodeError
    （ValueError 子類、except OSError 接不到）→ 反思 daemon 進 KeepAlive
    永久 crash-loop。replace 後壞行變 U+FFFD → json.loads 失敗 → 走既有的
    壞行跳過路徑；prune 的改寫會把它永久清掉（自癒）。"""
    if not os.path.exists(INTAKE_FILE):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(window_days)))
    out: list[dict[str, Any]] = []
    try:
        with open(INTAKE_FILE, "r", encoding="utf-8", errors="replace") as fh:
            # 共享鎖：跟 prune 的就地改寫互斥，避免讀到截斷到一半的內容。
            # （目前讀者只有反思程序自己、與 prune 循序不併發——這是未來
            # 有第二個讀者時的保險，成本兩行。）
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                        ts = datetime.fromisoformat(str(entry.get("ts") or ""))
                        # 比較留在 try 內：naive ts 對 aware cutoff 比較會丟
                        # TypeError——一行壞時間戳不准變成永久 crash-loop
                        if ts >= cutoff:
                            out.append(entry)
                    except (ValueError, TypeError):
                        continue
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        logger.warning("reflection intake 讀取失敗：%s", exc)
        return []
    return out


def prune(keep_days: int | None = None) -> int:
    """把超過 keep_days 的舊行整檔重寫掉。反思每日跑完呼叫一次（單一慢讀者，
    與夜跑 append 用同一把 flock 互斥）。回傳刪掉的行數。"""
    days = keep_days if keep_days is not None else env_int(
        "RED_REFLECTION_INTAKE_KEEP_DAYS", 7, min_value=1, max_value=90
    )
    if not os.path.exists(INTAKE_FILE):
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept: list[str] = []
    removed = 0
    try:
        # errors="replace"：理由同 read_recent——壞行要能被讀過去然後清掉，
        # 不能讓半個 UTF-8 字把唯一的自癒路徑（就是這個 prune）一起炸死。
        # ⚠️ 就地 truncate+rewrite 不是 crash-atomic：改寫中途斷電會丟整份
        # intake（代價=這 48h 的文件不被反思一次，可接受）。不用 temp+rename
        # 是因為換 inode 會讓「正 block 在舊 fd flock 上的 appender」寫進
        # 孤兒檔——常態下的靜默丟行比極罕見的斷電更貴。
        with open(INTAKE_FILE, "r+", encoding="utf-8", errors="replace") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                for raw in fh:
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(json.loads(stripped).get("ts") or ""))
                        if ts >= cutoff:
                            kept.append(stripped)
                        else:
                            removed += 1
                    except (ValueError, TypeError):
                        removed += 1  # 壞行順手清掉
                fh.seek(0)
                fh.truncate()
                if kept:
                    fh.write("\n".join(kept) + "\n")
                fh.flush()  # 落盤必須在解鎖前（同 record_batch 的理由）
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        logger.warning("reflection intake prune 失敗：%s", exc)
        return 0
    return removed
