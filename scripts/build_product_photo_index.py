"""建/更新產品照搜尋索引（規模化、resumable、零 API 成本）。

全公司 shared drive 的「款號資料夾」產品照 → macOS Vision 特徵指紋
（VNGenerateImageFeaturePrint，本機免費）→ var/data/product_photos/{fp.npy, meta.json}。
供 skills/product_photo_search.search_product_photos 以圖找圖。

只挑「款號夾」（夾名含鞋型詞、排除 Image/Logo/範本 等雜圖夾）——工廠產品照集中在
這類夾，盲掃整個 drive 會吃進大量 WeChat 截圖/單據掃描/logo，污染索引又慢。

用法（從 repo root 跑；worktree 需設 PYTHONPATH=.）：
  PYTHONPATH=. RED_DRIVE_USE_SA=1 .venv/bin/python scripts/build_product_photo_index.py             # 全 drive 增量建
  PYTHONPATH=. RED_DRIVE_USE_SA=1 .venv/bin/python scripts/build_product_photo_index.py --list-only  # 只列款號夾範圍
resumable：已索引的 file_id 會跳過；每 50 張存一次檔，中斷可續跑。
Drive API 呼叫皆帶指數退避重試（userRateLimitExceeded/429/503 自動退避），與夜跑/其他
session 共用 per-user 配額時不會一撞就死。
"""
import io
import json
import os
import random
import re
import sys
import time

import numpy as np

# 從 scripts/ 直接跑時把 repo root 補進 sys.path（比照 launchd/scripts/rag_sync.py），
# 讓手動跑與 launchd 排程都免帶 PYTHONPATH。
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.logging_and_paths import DATA_DIR  # noqa: E402

_OUTDIR = os.path.join(DATA_DIR, "product_photos")
_PROD = re.compile(r"靴|涼鞋|休閒鞋|童鞋|皮鞋|運動鞋|樣品鞋|球鞋|拖鞋")
_SKIP = re.compile(r"Image|Logo|公用|舊ERP|無法|地址|Tem|範本|^\.")
# 客戶照片樹（Phase C，2026-09-01）：「客戶鞋照片/Richter/FC-Husky 2.0/22w」這類
# 按客戶歸檔的實照，夾名（22w、FC-Husky 2.0）不含鞋型詞、_PROD 掃不到——實測
# Richter 185 張整棵樹都不在索引。改成：夾名命中 _CUST_ROOT 的當根，其**所有
# 子孫夾**全收，meta 的 folder 記相對路徑（"Richter/FC-Husky 2.0/22w"），文字搜
# "husky"/"richter" 才搜得到。
_CUST_ROOT = re.compile(r"客戶.*照片")
_MIN_BYTES = 30000
_DEV_DRIVE = "0AG3-cXn5dba7Uk9PVA"  # 開發部；舊索引只掃這裡，resume 時用來補 drive 標記


def _transient(e) -> bool:
    """Drive API 暫時性錯誤（值得退避重試）：429/5xx，或 403 的 rateLimit 類。"""
    from googleapiclient.errors import HttpError
    if not isinstance(e, HttpError):
        return False
    status = getattr(e.resp, "status", None)
    if status in (429, 500, 502, 503):
        return True
    return status == 403 and b"ateLimit" in (e.content or b"")


def _exec(req, tries: int = 7):
    """帶指數退避重試地執行一個 Drive API request。"""
    for i in range(tries):
        try:
            return req.execute()
        except Exception as e:  # noqa: BLE001 — 只重試暫時性，其餘立即 re-raise
            if _transient(e) and i < tries - 1:
                time.sleep(min(2 ** i + random.random(), 60))
                continue
            raise


def _model_of(folder: str) -> str:
    m = re.search(r"[A-Z]{1,3}[-\s]?J?[A-Z]{2,4}\d{2,4}|J[A-Z]{2}\d{3}|\d{2}L\d{6}|EX\d{3,5}|\d{4}-\d{4}", folder)
    return m.group(0) if m else ""


def _feature_vec(data: bytes):
    import Vision
    from Foundation import NSData
    nsd = NSData.dataWithBytes_length_(data, len(data))
    h = Vision.VNImageRequestHandler.alloc().initWithData_options_(nsd, None)
    r = Vision.VNGenerateImageFeaturePrintRequest.alloc().init()
    h.performRequests_error_([r], None)
    res = r.results()
    if not res:
        return None
    fp = res[0]
    raw = bytes(fp.data())
    n = fp.elementCount()
    v = np.frombuffer(raw, dtype=(np.float32 if len(raw) == n * 4 else np.float16)).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def _list_shared_drives(svc):
    """列舉帳號可見的全部 shared drive（id, name）。"""
    drives, tok = [], None
    while True:
        r = _exec(svc.drives().list(pageSize=100, fields="nextPageToken,drives(id,name)", pageToken=tok))
        drives += r.get("drives", [])
        tok = r.get("nextPageToken")
        if not tok:
            break
    return drives


def _list_folders(svc, drive_id):
    folders, tok = [], None
    while True:
        r = _exec(svc.files().list(corpora="drive", driveId=drive_id, includeItemsFromAllDrives=True,
                                   supportsAllDrives=True, fields="nextPageToken,files(id,name,parents)",
                                   pageSize=1000,
                                   pageToken=tok, q="mimeType='application/vnd.google-apps.folder' and trashed=false"))
        folders += r.get("files", [])
        tok = r.get("nextPageToken")
        if not tok:
            break
    return folders


def _customer_tree_labels(folders) -> dict:
    """客戶照片樹的 {folder_id: 相對路徑 label}（純函式、可測）。

    根＝夾名命中 _CUST_ROOT；label 從根的下一層算起（"Richter/FC-Husky 2.0/22w"）。
    命中 _SKIP 的節點連同子樹剪掉（客戶樹裡叫 Image/範本 的夾一樣是雜圖）。
    """
    from collections import defaultdict
    children = defaultdict(list)
    for f in folders:
        for p in f.get("parents") or []:
            children[p].append(f)
    out = {}
    for root in folders:
        if not _CUST_ROOT.search(root.get("name", "")):
            continue
        stack = [(c, c["name"]) for c in children[root["id"]]]
        while stack:
            f, label = stack.pop()
            if _SKIP.search(f["name"]):
                continue
            out[f["id"]] = label
            stack += [(c, f"{label}/{c['name']}") for c in children[f["id"]]]
    return out


def _target_folders(folders):
    """要索引的夾：[(folder_dict, label)]＝款號夾（label=夾名）∪ 客戶照片樹（label=路徑）。"""
    cust = _customer_tree_labels(folders)
    targets, seen = [], set()
    for f in folders:
        if f["id"] in cust:
            targets.append((f, cust[f["id"]]))
            seen.add(f["id"])
    for f in folders:
        if f["id"] not in seen and _PROD.search(f["name"]) and not _SKIP.search(f["name"]):
            targets.append((f, f["name"]))
    return targets


def _list_images(svc, drive_id, folder_id):
    imgs, tok = [], None
    while True:
        r = _exec(svc.files().list(corpora="drive", driveId=drive_id, includeItemsFromAllDrives=True,
                                   supportsAllDrives=True, fields="nextPageToken,files(id,name,size)", pageSize=1000,
                                   pageToken=tok,
                                   q=f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false"))
        imgs += r.get("files", [])
        tok = r.get("nextPageToken")
        if not tok:
            break
    return imgs


def main() -> None:
    from agent_core.google_auth import get_service
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload

    list_only = "--list-only" in sys.argv

    os.makedirs(_OUTDIR, exist_ok=True)
    meta_f, fp_f = os.path.join(_OUTDIR, "meta.json"), os.path.join(_OUTDIR, "fp.npy")
    svc = get_service("drive", "v3")

    def dl(fid):
        b = io.BytesIO()
        d = MediaIoBaseDownload(b, svc.files().get_media(fileId=fid, supportsAllDrives=True))
        done = False
        while not done:
            for i in range(7):
                try:
                    _, done = d.next_chunk()
                    break
                except HttpError as e:
                    if _transient(e) and i < 6:
                        time.sleep(min(2 ** i + random.random(), 60))
                        continue
                    raise
        return b.getvalue()

    drives = _list_shared_drives(svc)
    print(f"[product_index] 可見 shared drive {len(drives)} 個", flush=True)

    if list_only:
        # 乾跑：只列各 drive 的款號夾 + 待索引圖數（過 _MIN_BYTES），不下載不 Vision。
        g_folders = g_imgs = 0
        for dr in drives:
            folders = _list_folders(svc, dr["id"])
            prod = _target_folders(folders)
            if not prod:
                continue
            n_imgs = 0
            for fo, _label in prod:
                n_imgs += sum(1 for im in _list_images(svc, dr["id"], fo["id"])
                              if int(im.get("size", 0) or 0) >= _MIN_BYTES)
            g_folders += len(prod)
            g_imgs += n_imgs
            print(f"  {dr['name'][:26]:26} 款號夾 {len(prod):>3}／{len(folders):>4}  圖 {n_imgs:>5}", flush=True)
        print(f"[product_index] 合計：款號夾 {g_folders}、待索引圖 ~{g_imgs}", flush=True)
        return

    # resume：載入既有索引
    meta = json.load(open(meta_f, encoding="utf-8")) if os.path.exists(meta_f) else []
    vecs = list(np.load(fp_f)) if os.path.exists(fp_f) else []
    # resume 對齊：上次若在「存完 fp.npy、還沒存 meta.json」(或反之) 當機，兩者長度可能差 1。
    if len(vecs) != len(meta):
        n = min(len(vecs), len(meta))
        vecs, meta = vecs[:n], meta[:n]
    # 舊索引（只開發部、無 drive 標記）補齊 drive 欄，供跨 drive 顯示/篩選一致。
    for m in meta:
        m.setdefault("drive_id", _DEV_DRIVE)
        m.setdefault("drive_name", "開發部門")  # 對齊 drives().list() 回傳的真實 drive 名
    done_ids = {m["id"] for m in meta}
    print(f"[product_index] 啟動，已索引 {len(meta)} 張（resume）", flush=True)

    def _save():
        np.save(fp_f, np.vstack(vecs))
        json.dump(meta, open(meta_f, "w"), ensure_ascii=False)

    t0 = time.time()
    new = 0
    for dr in drives:
        folders = _list_folders(svc, dr["id"])
        prod = _target_folders(folders)
        if not prod:
            continue
        print(f"[product_index] {dr['name'][:26]}：款號夾 {len(prod)}／{len(folders)}", flush=True)
        for fo, label in prod:
            model = _model_of(label)
            for im in _list_images(svc, dr["id"], fo["id"]):
                if im["id"] in done_ids or int(im.get("size", 0) or 0) < _MIN_BYTES:
                    continue
                try:
                    v = _feature_vec(dl(im["id"]))
                except Exception:  # noqa: BLE001 — 單張壞圖/下載失敗跳過，不中斷整輪
                    continue
                if v is None:
                    continue
                vecs.append(v)
                meta.append({"id": im["id"], "file": im["name"], "folder": label,
                             "model": model, "drive_id": dr["id"], "drive_name": dr["name"]})
                done_ids.add(im["id"])
                new += 1
                if new % 50 == 0:
                    _save()
                    print(f"[product_index] 進度 {len(meta)} 張（本次 +{new}）[{time.time()-t0:.0f}s]", flush=True)
    _save()
    print(f"[product_index] ✅ 完成，共 {len(meta)} 張、本次 +{new}、{time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
