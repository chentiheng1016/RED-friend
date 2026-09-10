"""產品照搜尋工具：以款號/鞋型關鍵字 或 參考圖 找關聯舊款。

視覺相似走 macOS Vision 特徵指紋（本機、免費）；索引在 var/data/product_photos/
（由 scripts/build_product_photo_index 建）。收到客人樣品單時，找出做過最相似的舊款。

fetch_shoe_photos 的由來：2026-07-28 UserA 案——問迪卡儂 #8916447 鞋圖，
freeform 只回得出文字描述＋Drive 資料夾位置，照片本體傳不進 Telegram。
這顆工具把「款號→照片檔→傳到當前對話」做成一條龍：客戶款號先過 ERP 鏡像
（SE_ORD_ITEM.CUST_LOT↔PROD_NO）確定性換成生管型體，再搜 Drive 圖檔下載，
回覆裡的 [[TG_PHOTO:...]] 標記由 daemon_telegram 回覆路徑轉成 sendPhoto。
"""
import io
import json
import os
import re

import numpy as np

from agent_core.logging_and_paths import DATA_DIR

_DIR = os.path.join(DATA_DIR, "product_photos")
# fetch_shoe_photos 的下載暫存區——daemon_telegram 回覆附圖只放行這個目錄
# （與 GENERATED_IMAGES_DIR），路徑白名單擋 prompt-injection 夾帶任意本機檔。
_FETCH_DIR = os.path.join(_DIR, "fetched")
_FETCH_KEEP_FILES = 100                    # 暫存區保留上限（超過刪最舊）
_TG_PHOTO_MAX_BYTES = 10 * 1024 * 1024     # Telegram sendPhoto 10MB 上限
_FETCH_MAX_PHOTOS = 5
# Drive 的 mimeType contains 'image/' 會把 .cdr（CorelDRAW 線稿）之類也算進來，
# Telegram sendPhoto 傳不了——只收跟 daemon 回覆附圖白名單一致的照片格式。
_PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp")
# Drive 搜圖的 drive 白名單（Codex P1, PR #305）：employee 拿到這顆 SAFE 工具
# 後，不能讓任意關鍵字用 owner 視野掃全部共用硬碟（會撈到會計掃描件等非產品
# 圖、繞過 search_drive_docs 的 per-color ACL）。預設只搜「開發部門」——鞋圖
# 歸檔地、也是產品照索引 builder 的主 drive；要擴充用 env 逗號分隔。
_DEFAULT_PHOTO_DRIVE_IDS = ("0AG3-cXn5dba7Uk9PVA",)  # 開發部門


def _allowed_photo_drive_ids() -> tuple[str, ...]:
    raw = os.environ.get("RED_SHOE_PHOTO_DRIVE_IDS", "").strip()
    if not raw:
        return _DEFAULT_PHOTO_DRIVE_IDS
    ids = tuple(p.strip() for p in raw.split(",") if p.strip())
    return ids or _DEFAULT_PHOTO_DRIVE_IDS


def _load():
    fp = np.load(os.path.join(_DIR, "fp.npy"))
    with open(os.path.join(_DIR, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    return fp, meta


def _link(fid: str) -> str:
    return f"https://drive.google.com/file/d/{fid}/view"


def _fmt(rows) -> str:
    from agent_core.customer_identify import customer_of_folder
    out = []
    for sim, m in rows:
        s = f"相似 {sim:.0%}｜" if sim is not None else ""
        cust = customer_of_folder(m.get("folder", ""))
        c = f"客戶 {cust}｜" if cust else ""
        out.append(f"• {s}款號 {m.get('model') or '?'}｜{c}{m.get('folder','')[:26]}｜{m.get('file','')[:30]}\n  {_link(m['id'])}")
    return "\n".join(out)


def _vision_fp(data: bytes) -> np.ndarray:
    try:
        import Vision
        from Foundation import NSData
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("以圖找圖需 macOS Vision（本工具須在 Mac 上執行）") from exc
    nsd = NSData.dataWithBytes_length_(data, len(data))
    h = Vision.VNImageRequestHandler.alloc().initWithData_options_(nsd, None)
    r = Vision.VNGenerateImageFeaturePrintRequest.alloc().init()
    h.performRequests_error_([r], None)
    res = r.results()
    if not res:
        raise RuntimeError("無法產生圖片特徵指紋")
    fp = res[0]
    raw = bytes(fp.data())
    n = fp.elementCount()
    v = np.frombuffer(raw, dtype=(np.float32 if len(raw) == n * 4 else np.float16)).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def search_product_photos(query: str = "", reference_file_id: str = "",
                          reference_image_path: str = "", top_k: int = 6) -> str:
    """從工廠產品照庫找關聯舊款——收到客人樣品單時，找出做過最相似的舊款（含款號 + Drive 連結）。

    三種用法擇一（視覺相似走 macOS Vision 特徵指紋、本機免費）：
    - reference_image_path：本機圖片路徑（如大王從 Telegram 上傳的照片路徑），以圖找圖。
      ★ 最適合「客人丟一張樣單照片 → 找做過的關聯舊款」這個情境。
    - reference_file_id：一張參考圖的 Drive file_id，以圖找圖。
    - query：款號或鞋型關鍵字做文字搜尋，如 "JEH151"、"雪靴"、"涼鞋"、"Lurchi"。

    Args:
        query: 款號 / 鞋型 / 品牌關鍵字（文字搜）。
        reference_image_path: 本機圖片路徑（Telegram 上傳的照片路徑）以圖找圖。
        reference_file_id: 參考圖的 Drive file_id（以圖找圖）。
        top_k: 回幾筆，預設 6（上限 20）。
    """
    try:
        fp, meta = _load()
    except FileNotFoundError:
        return "產品照索引尚未建立（var/data/product_photos）。請先跑 scripts/build_product_photo_index。"
    if not meta:
        return "產品照索引目前是空的。"
    k = max(1, min(int(top_k or 6), 20))

    def _topk(q):
        sims = fp @ q
        idx = np.argsort(-sims)[:k]
        return [(float(sims[i]), meta[i]) for i in idx]

    if reference_image_path:
        try:
            with open(reference_image_path.strip(), "rb") as f:
                q = _vision_fp(f.read())
        except FileNotFoundError:
            return f"找不到圖片：{reference_image_path}"
        except Exception as e:  # noqa: BLE001
            return f"讀取/分析圖片失敗：{type(e).__name__}: {str(e)[:80]}"
        rows = _topk(q)
        return f"以上傳的照片找到 {len(rows)} 個最相似舊款（產品照庫共 {len(meta)} 張）：\n" + _fmt(rows)

    if reference_file_id:
        from agent_core.google_auth import get_service
        from googleapiclient.http import MediaIoBaseDownload
        try:
            svc = get_service("drive", "v3")
            buf = io.BytesIO()
            d = MediaIoBaseDownload(buf, svc.files().get_media(fileId=reference_file_id.strip(), supportsAllDrives=True))
            done = False
            while not done:
                _, done = d.next_chunk()
            q = _vision_fp(buf.getvalue())
        except Exception as e:  # noqa: BLE001
            return f"下載/分析參考圖失敗（{reference_file_id}）：{type(e).__name__}: {str(e)[:80]}"
        rows = _topk(q)
        return f"以參考圖找到 {len(rows)} 個最相似舊款（產品照庫共 {len(meta)} 張）：\n" + _fmt(rows)

    if query:
        ql = query.strip().lower()
        hits = [(None, m) for m in meta
                if ql in (str(m.get("model", "")) + m.get("folder", "") + m.get("file", "")).lower()]
        if not hits:
            return f"產品照庫找不到含「{query}」的款（目前索引 {len(meta)} 張）。"
        return f"文字搜「{query}」找到 {min(len(hits), k)} 筆（共 {len(hits)}）：\n" + _fmt(hits[:k])

    return ("請給 reference_image_path（上傳的照片路徑）、reference_file_id（Drive file_id）"
            "或 query（款號/鞋型關鍵字）其中一個。")


def _erp_style_codes(keyword: str) -> list[str]:
    """客戶款號(CUST_LOT)→生管型體(PROD_NO) 確定性反查；鏡像不可用回空、不拋。

    回傳含完整型體-顏色（DJS336195-01GREEN）與型體基底（DJS336195）兩層，
    基底才搜得到 Drive 上「DJS336195.jpg」這類不帶色碼的鞋圖檔名。
    """
    try:
        from agent_core.erp_stock_query import _db_ready, _escape_like, _run
        if not _db_ready():
            return []
        rows = _run(
            "SELECT DISTINCT PROD_NO FROM SC00__SE_ORD_ITEM "
            "WHERE CUST_LOT LIKE ? ESCAPE '\\' AND PROD_NO IS NOT NULL",
            [f"%{_escape_like(keyword)}%"],
        )
    except Exception:  # noqa: BLE001
        return []
    codes: list[str] = []
    for (prod_no,) in rows:
        full = str(prod_no or "").strip()
        if not full:
            continue
        base = re.split(r"[-\s]", full, maxsplit=1)[0].strip()
        for code in (full, base):
            if len(code) >= 4 and code not in codes:
                codes.append(code)
    return codes


# 真實產品照的檔名慣例是「數字型體-客戶款號」（336195-8916447 GREEN綠.jpg，
# 不帶 DJS 前綴）；線稿/部位標註圖則是 DJS336195L1.jpg／DJS336195 pt (A005).jpg
# 這類。2026-07-28 UserA 案：問「彩圖」被傳線稿——搜尋沒展開數字核心撈不到
# 真實照、又按檔案大小排序把最大的線稿排最前。
_COLORWAY_PHOTO_RE = re.compile(r"\d{5,}\s*-\s*\d{5,}")
_LINEART_NAME_RE = re.compile(r"(?i)(?:L\d+\s*\.|\bpt\b|\(A\d{3}\))")


def _numeric_core(code: str) -> str:
    """'DJS336195-01GREEN' → '336195'（≥5 位數字核心；沒有回 ''）。"""
    m = re.search(r"\d{5,}", code or "")
    return m.group(0) if m else ""


def _erp_cust_lots(keyword: str) -> list[str]:
    """生管型體/數字核心 → 客戶款號（CUST_LOT）反查；鏡像不可用回空、不拋。

    真實照檔名帶客戶款號（336195-8916447…）——查型體時不反查款號就撈不到。
    """
    kw = (keyword or "").strip()
    if len(kw) < 5:
        return []
    try:
        from agent_core.erp_stock_query import _db_ready, _escape_like, _run
        if not _db_ready():
            return []
        rows = _run(
            "SELECT DISTINCT CUST_LOT FROM SC00__SE_ORD_ITEM "
            "WHERE PROD_NO LIKE ? ESCAPE '\\' AND CUST_LOT IS NOT NULL",
            [f"%{_escape_like(kw)}%"],
        )
    except Exception:  # noqa: BLE001
        return []
    lots: list[str] = []
    for (lot,) in rows:
        s = str(lot or "").strip()
        if s.isdigit() and len(s) >= 6 and s not in lots:
            lots.append(s)
    return lots


def _drive_image_hits(svc, keyword: str, page_size: int = 20) -> list[dict]:
    """白名單 drive 內搜「檔名含 keyword 的圖片檔」。單一 keyword、失敗回空。"""
    safe_kw = keyword.replace("\\", "\\\\").replace("'", "\\'")
    hits: list[dict] = []
    for drive_id in _allowed_photo_drive_ids():
        try:
            res = svc.files().list(
                q=(f"name contains '{safe_kw}' and mimeType contains 'image/' "
                   "and trashed=false"),
                corpora="drive", driveId=drive_id,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True, pageSize=page_size,
                fields="files(id,name,mimeType,size)",
            ).execute()
        except Exception:  # noqa: BLE001
            continue
        hits.extend(res.get("files", []) or [])
    return hits


def _prune_fetch_dir() -> None:
    try:
        entries = [os.path.join(_FETCH_DIR, n) for n in os.listdir(_FETCH_DIR)]
        files = sorted((p for p in entries if os.path.isfile(p)), key=os.path.getmtime)
        for path in files[:max(0, len(files) - _FETCH_KEEP_FILES)]:
            os.remove(path)
    except OSError:
        pass


def fetch_shoe_photos(query: str, top_k: int = 3) -> str:
    """把鞋款照片本體抓下來、直接傳進 Telegram 對話（款號→照片一條龍）。

    使用者問「給我 #8916447 的鞋子照片」這類需求時用這顆——search_product_photos
    只回 Drive 連結，這顆會把照片檔下載回來，並在回傳文字附上 [[TG_PHOTO:...]]
    標記；Telegram 回覆時系統會自動把標記轉成照片直接傳送。

    流程（確定性優先）：客戶款號先過 ERP 鏡像換成生管型體（如 8916447 →
    DJS336195），連同原關鍵字、數字核心（336195）與反查款號搜 Drive 圖片檔
    （檔名比對），也查本地產品照索引。**「彩圖」＝真實產品照**——排序自動把
    真實照（檔名帶款號，如 336195-8916447 GREEN綠.jpg）排最前、線稿/部位
    標註圖（DJS336195L1、pt (A005)）沉底，問彩圖不會再傳到線稿。
    **指定款號只回該款**：查 #8916447 時檔名命中該款就只傳該款，不會把同型體
    其他 STYLE（8916445/9026204…）一起傳；查無正主才退同型體參考並明講非本款。

    Args:
        query: 款號/型體關鍵字，可多個（空格或逗號分隔）。客戶 STYLE#（8916447）、
               生管型體（DJS336195）、品名（MH100）都可以。
        top_k: 最多傳幾張，預設 3（上限 5）。
    Returns:
        找到的照片清單＋ [[TG_PHOTO:路徑]] 標記行。⚠️ 回覆使用者時必須把
        [[TG_PHOTO:...]] 標記行原樣保留（系統轉成照片傳送，不會顯示原文）。
    """
    keywords = [k.strip().lstrip("#") for k in re.split(r"[,\s，、]+", query or "") if k.strip().lstrip("#")]
    if not keywords:
        return "請給款號或型體關鍵字，例：fetch_shoe_photos(\"8916447\") 或 \"DJS336195\"。"
    k = max(1, min(int(top_k or 3), _FETCH_MAX_PHOTOS))

    # ① 客戶款號 → 生管型體（ERP 鏡像確定性反查；查不到就用原關鍵字搜）
    expanded: list[str] = []
    resolved_note: list[str] = []
    for kw in keywords:
        if kw not in expanded:
            expanded.append(kw)
        for code in _erp_style_codes(kw):
            if code not in expanded:
                expanded.append(code)
                resolved_note.append(f"{kw}→{code}")
    # ①b 數字核心＋反查款號補進搜尋——真實照檔名是「336195-8916447 …」這種
    # 不帶 DJS 前綴、帶客戶款號的慣例，不展開就只撈得到線稿（彩圖案根因）。
    input_lots = {kw for kw in keywords if kw.isdigit() and len(kw) >= 6}
    lot_terms: set[str] = set(input_lots)
    for kw in list(expanded):
        core = _numeric_core(kw)
        if core and core not in expanded:
            expanded.append(core)
    for kw in list(expanded):
        for lot in _erp_cust_lots(kw):
            lot_terms.add(lot)
            if lot not in expanded:
                expanded.append(lot)

    # ② Drive 搜圖片檔（檔名含關鍵字）
    try:
        from agent_core.google_auth import get_service
        svc = get_service("drive", "v3")
    except Exception as e:  # noqa: BLE001
        return f"❌ Drive 服務初始化失敗：{type(e).__name__}: {str(e)[:80]}"
    candidates: list[dict] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for kw in expanded[:10]:
        for f in _drive_image_hits(svc, kw):
            fid, name = f.get("id", ""), f.get("name", "")
            if not fid or fid in seen_ids or name in seen_names:
                continue
            if not name.lower().endswith(_PHOTO_EXTS):
                continue
            seen_ids.add(fid)
            seen_names.add(name)
            candidates.append(f)

    # ③ 本地產品照索引補位（索引項本身就是 Drive 圖檔）
    if len(candidates) < k:
        try:
            _, meta = _load()
        except Exception:  # noqa: BLE001
            meta = []
        for m in meta:
            hay = (str(m.get("model", "")) + m.get("folder", "") + m.get("file", "")).lower()
            if any(kw.lower() in hay for kw in expanded):
                fid, name = m.get("id", ""), m.get("file", "")
                if not name.lower().endswith(_PHOTO_EXTS):
                    continue
                if fid and fid not in seen_ids and name not in seen_names:
                    seen_ids.add(fid)
                    seen_names.add(name)
                    candidates.append({"id": fid, "name": name, "size": "0"})

    if not candidates:
        hint = f"（款號解析：{'、'.join(resolved_note)}）" if resolved_note else ""
        return (f"Drive 與產品照索引都找不到「{'、'.join(keywords)}」的圖片檔{hint}。\n"
                "可改用 search_product_photos 文字搜，或請樣品室確認鞋圖歸檔位置。")

    # ③b 指定款號 only（2026-07-29 UserA 案：問 #8916447 被回整個型體 336195
    # 所有 STYLE）——使用者給了明確客戶款號且有檔名命中時，只留該款；型體/兄弟款
    # 展開只在找不到正主時當備援，且回覆會標明是同型體參考、非本款。
    family_fallback: list[str] = []
    if input_lots:
        exact = [f for f in candidates
                 if any(t in str(f.get("name") or "") for t in input_lots)]
        if exact:
            candidates = exact
        else:
            family_fallback = sorted(input_lots)

    # 排序：真實照優先（彩圖案）——①查詢款號命中 ②款號/配色照命名慣例
    # ③非線稿 ④大檔優先（縮圖/小icon 沉底）。線稿/部位標註圖永遠最後補位。
    def _size(f):
        try:
            return int(f.get("size") or 0)
        except (TypeError, ValueError):
            return 0

    def _rank(f):
        name = str(f.get("name") or "")
        input_hit = any(t in name for t in input_lots)
        real_hint = input_hit or any(t in name for t in lot_terms) \
            or bool(_COLORWAY_PHOTO_RE.search(name))
        lineart = bool(_LINEART_NAME_RE.search(name))
        return (not input_hit, not real_hint, lineart, -_size(f))

    candidates.sort(key=_rank)
    picked = [f for f in candidates if _size(f) <= _TG_PHOTO_MAX_BYTES][:k]

    # ④ 下載到暫存區（daemon 回覆附圖的路徑白名單目錄）
    from googleapiclient.http import MediaIoBaseDownload
    os.makedirs(_FETCH_DIR, exist_ok=True)
    lines: list[str] = []
    markers: list[str] = []
    for f in picked:
        try:
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(
                buf, svc.files().get_media(fileId=f["id"], supportsAllDrives=True))
            done = False
            while not done:
                _, done = dl.next_chunk()
            data = buf.getvalue()
            if not data or len(data) > _TG_PHOTO_MAX_BYTES:
                continue
            safe_name = re.sub(r"[^\w.\-()（）一-鿿]+", "_", f.get("name") or f["id"])
            # 截長檔名只截 stem、副檔名保留（Codex P2, PR #305）——截掉 .jpg
            # 會讓 daemon 回覆附圖閘門以副檔名拒收，下載了也傳不出去。
            stem, ext = os.path.splitext(safe_name)
            local = os.path.join(_FETCH_DIR, f"{f['id'][:8]}_{stem}"[:110] + ext)
            with open(local, "wb") as fh:
                fh.write(data)
            kind = ("線稿/標註圖" if _LINEART_NAME_RE.search(f.get("name") or "")
                    else "照片")
            lines.append(f"• {f.get('name','')[:60]}｜{kind}｜"
                         f"{len(data) / 1024:.0f}KB｜{_link(f['id'])}")
            markers.append(f"[[TG_PHOTO:{local}]]")
        except Exception as e:  # noqa: BLE001
            lines.append(f"• ❌ {f.get('name','')[:60]} 下載失敗：{type(e).__name__}")
    _prune_fetch_dir()

    if not markers:
        return ("找到候選圖檔但下載全數失敗：\n" + "\n".join(lines))
    head = f"找到 {len(markers)} 張「{'、'.join(keywords)}」鞋圖"
    if resolved_note:
        head += f"（ERP 款號解析：{'、'.join(resolved_note[:4])}）"
    if family_fallback:
        head += (f"\n⚠️ 查無檔名含款號 {'、'.join(family_fallback)} 的圖檔——以下是"
                 "同型體其他配色/線稿**參考**（非該款本尊），回覆時必須講明。")
    return (head + "：\n" + "\n".join(lines) + "\n\n" + "\n".join(markers) +
            "\n⚠️ 回覆時請把上面 [[TG_PHOTO:...]] 標記行原樣放在回覆最後，"
            "系統會自動轉成照片傳給使用者（標記本身不會顯示）。")


SKILL_TOOLS = [search_product_photos, fetch_shoe_photos]
