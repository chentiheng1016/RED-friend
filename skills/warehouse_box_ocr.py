"""外箱手寫嘜頭 OCR — 倉庫點貨用（read_box_shipping_marks）。

出貨外箱上常有手寫 marker 嘜頭，台灣物流慣例三要素：
  1. 客戶名（箱頂手寫中文，例「名媛」）
  2. 帳號/單號記法「姓:數字」（例「何:2380」）
  3. 圈起來的件數（手畫圓圈內的數字，例 ㉒）

引擎：**純本機、無 LLM** — PaddleOCR PP-OCRv6 medium（onnxruntime 後端），
跑在專用 venv（var/venvs/box_ocr，用 `bin/setup-box-ocr` 建置；主 .venv 不裝
paddle，因為 paddlex 會降版 numpy 且 opencv 套件互踩）。skill 以 subprocess
呼叫 scripts/box_ocr_runner.py（含 0/90/180/270 旋轉投票 — 倉庫拍照常倒拍），
OCR 行結果再用本檔的規則式後處理對映到三欄 schema。單張 ~2–3s；資料夾批次
同一 process 只載一次模型。

封閉集合校正（lexicon）：客戶名與帳號代碼是有限集合 — OCR 輸出會對
`var/data/box_ocr_lexicon.json` 的白名單做編輯距離吸附與混淆字對映
（例：某客戶草書被讀成形近字 → 該誤讀不在客戶清單、距離 1 → 吸回正確
客戶名；混淆表則是「錯字→對字」直接對映）。數字欄若 lexicon 提供合法
候選清單，會用手寫數字混淆權重（0/4、1/7…）交叉驗證。校正動作記在輸出
"corrections" 欄，供人工覆核。**維護**：用 record_box_ocr_correction
累積（推薦），或大王直接編輯 lexicon JSON；出廠為空。

安全：
  - 員工（部門色 freeform）session 只能讀 Telegram 上傳目錄裡的檔、且禁
    資料夾批次（上傳目錄按日期共用、無 per-chat 隔離）。
  - 箱上寫什麼 OCR 就回什麼（外部可控內容）→ 欄位過 sanitize_for_llm
    再回 LLM context（同 vision.analyze_image Round 7 慣例）。
"""
import json
import os
import re

_MAX_IMAGE_MB = 20
_MAX_BATCH = 20
_FIELD_MAX_LEN = 60
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

_RESULT_MARKER = "RESULT_JSON:"
_RUNNER_TIMEOUT_S = 300  # 冷啟含模型載入；模型檔應已由 setup-box-ocr 預熱

# 辨識信心門檻：低於此分數的行不參與欄位對映
_MIN_SCORE = 0.5

# 印刷雜訊（非手寫嘜頭）：箱面尺寸、重量、數量、膠帶 logo、警示標語等
_PRINTED_NOISE = re.compile(
    r"(\d+(\.\d+)?\s*[xX×]\s*\d+|CM\b|MADE\s*IN|G\.?W|N\.?W|QTY|CTN|PCS|專用"
    r"|易碎|小心|此面向上|怕[濕雨]|輕放|勿[壓踏]"
    r"|FRAGILE|HANDLE\s*WITH\s*CARE|KEEP\s*DRY|THIS\s*SIDE\s*UP)",
    re.IGNORECASE,
)

# 「姓/代號:數字」— key 為 1-6 個非數字字元，value 為 1-7 位數
_REF_RE = re.compile(r"^([^\d:]{1,6}):(\d{1,7})$")
_CJK_RE = re.compile(r"[一-鿿]")

_NORMALIZE_TABLE = str.maketrans("０１２３４５６７８９：", "0123456789:")


def _clean_field(v):
    """字串欄位淨化：外部可控內容過 sanitize_for_llm、截長度。None/空 → None。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        from agent_core.prompt_injection import sanitize_for_llm
        s = sanitize_for_llm(s).strip()
    except Exception:
        pass
    return s[:_FIELD_MAX_LEN] or None


def _normalize_text(text: str) -> str:
    """全形數字/冒號轉半形、去空白。"""
    return (text or "").translate(_NORMALIZE_TABLE).replace(" ", "").strip()


def _extract_marks(lines: list) -> dict:
    """把 OCR 行結果（[{text, score, box}]）對映到嘜頭三欄 schema。

    規則（純函式、無模型）：
      1. 丟掉低信心行與印刷雜訊（尺寸/重量/膠帶 logo）。
      2. reference_entry：整行符合「key:數字」；或「key:」與純數字兩行
         同列相鄰（OCR 常把冒號前後拆開）。
      3. customer_name：剩餘含中文、無數字的行中最上方那行。
      4. circled_count：剩餘 1-4 位純數字行取信心最高者
         （圈圈本身不進 OCR 文字，數字照讀）。
    對映不到的欄位回 None，不亂編。
    """
    cands = []  # (text, score, box)
    for ln in lines or []:
        text = _normalize_text(str(ln.get("text") or ""))
        try:
            score = float(ln.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        box = ln.get("box") or None
        if not text or score < _MIN_SCORE:
            continue
        if _PRINTED_NOISE.search(text):
            continue
        cands.append((text, score, box))

    used = set()

    # ── reference_entry ──
    ref_key = ref_val = None
    for i, (text, _score, _box) in enumerate(cands):
        m = _REF_RE.match(text)
        if m:
            ref_key, ref_val = m.group(1), m.group(2)
            used.add(i)
            break
    if ref_key is None:
        # 拆行配對：「何:」＋同列右側最近的純數字
        for i, (text, _score, box) in enumerate(cands):
            if not (text.endswith(":") and len(text) >= 2 and not text[:-1].isdigit()):
                continue
            best = None
            for j, (text2, _s2, box2) in enumerate(cands):
                if j == i or not text2.isdigit():
                    continue
                if box and box2:
                    same_row = abs(((box2[1] + box2[3]) - (box[1] + box[3])) / 2) \
                        < max(box[3] - box[1], box2[3] - box2[1])
                    to_right = box2[0] >= box[0]
                    if not (same_row and to_right):
                        continue
                    gap = box2[0] - box[2]
                else:
                    # 無座標時退回行序鄰近 —— 只配對後方的數字，
                    # 否則負 gap 會讓 key 錯配到前面的數字
                    if j <= i:
                        continue
                    gap = j - i
                if best is None or gap < best[0]:
                    best = (gap, j)
            if best is not None:
                j = best[1]
                ref_key, ref_val = text[:-1], cands[j][0]
                used.update({i, j})
                break

    # ── customer_name：剩餘含中文、無數字、無冒號 → 取最上方 ──
    name = None
    name_y = None
    for i, (text, _score, box) in enumerate(cands):
        if i in used:
            continue
        if _CJK_RE.search(text) and not any(c.isdigit() for c in text) and ":" not in text:
            y = box[1] if box else float("inf")
            if name is None or y < name_y:
                name, name_y = text, y

    # ── circled_count：剩餘 1-4 位純數字 → 信心最高 ──
    count = None
    count_score = -1.0
    for i, (text, score, _box) in enumerate(cands):
        if i in used:
            continue
        if text.isdigit() and 1 <= len(text) <= 4 and score > count_score:
            count, count_score = int(text), score

    return {
        "customer_name": _clean_field(name),
        "reference_entry": {
            "key": _clean_field(ref_key),
            "value": _clean_field(ref_val),
        },
        "circled_count": count,
    }


# ── 封閉集合校正（lexicon） ──────────────────────────────────────────
# 種子刻意留空：lexicon 只該長「自家」的客戶名/代碼 — 用
# record_box_ocr_correction 累積、或直接編輯 var/data/box_ocr_lexicon.json。
# （曾以外部測試照的值當種子；那張非公司檔案，2026-07-24 已清空。）
_DEFAULT_LEXICON = {
    "customers": [],
    "ref_keys": [],
    "ref_values": [],
    "confusions": {
        "customer": {},
        "ref_key": {},
    },
}

# 手寫數字形近混淆對（雙向）：這些位置代換算 0.5 距離、其餘算 1
_DIGIT_CONFUSABLE = {frozenset(p) for p in
                     [("0", "4"), ("0", "8"), ("1", "7"), ("3", "8"),
                      ("4", "9"), ("5", "6"), ("7", "9")]}


def _lexicon_path() -> str:
    from agent_core.logging_and_paths import RUNTIME_ROOT
    return os.path.join(RUNTIME_ROOT, "data", "box_ocr_lexicon.json")


def _load_lexicon() -> dict:
    """讀 lexicon（無檔則以種子建檔）。壞檔回種子、不炸。

    一律深拷貝 — caller（record_box_ocr_correction）會就地改寫回傳值，
    淺拷貝會把寫入洩進模組層級的 _DEFAULT_LEXICON 共享 list/dict。
    """
    import copy
    path = _lexicon_path()
    try:
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(_DEFAULT_LEXICON, f, ensure_ascii=False, indent=2)
            return copy.deepcopy(_DEFAULT_LEXICON)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return copy.deepcopy(_DEFAULT_LEXICON)
        merged = copy.deepcopy(_DEFAULT_LEXICON)
        merged.update({k: v for k, v in data.items() if v is not None})
        return merged
    except Exception:
        return copy.deepcopy(_DEFAULT_LEXICON)


def _lev(a: str, b: str) -> int:
    """短字串 Levenshtein（詞表數十筆、線性掃即可，不引依賴）。"""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _digit_distance(a: str, b: str) -> float:
    """數字串加權距離：形近數字代換 0.5、其餘 1；長度不同退回 Levenshtein。"""
    if len(a) != len(b):
        return float(_lev(a, b))
    d = 0.0
    for ca, cb in zip(a, b):
        if ca == cb:
            continue
        d += 0.5 if frozenset((ca, cb)) in _DIGIT_CONFUSABLE else 1.0
    return d


def _snap_to_whitelist(value, whitelist, confusion_map, label):
    """把 OCR 值吸附到白名單。回 (corrected, correction_msg|None)。

    順序：完全命中 → 混淆表 → 編輯距離 1 內最近合法值。吸不到就原樣保留
    （白名單是校正不是過濾 — 新客戶還沒建檔時不能吞掉輸出）。
    """
    if not value:
        return value, None
    if value in (whitelist or []):
        return value, None
    mapped = (confusion_map or {}).get(value)
    if mapped:
        return mapped, f"{label}「{value}」依混淆表校正為「{mapped}」"
    best_d, ties = None, []
    for cand in (whitelist or []):
        d = _lev(value, cand)
        if best_d is None or d < best_d:
            best_d, ties = d, [cand]
        elif d == best_d:
            ties.append(cand)
    if ties and best_d <= 1 and max(len(value), len(ties[0])) >= 2:
        if len(ties) == 1:
            return ties[0], f"{label}「{value}」依白名單校正為「{ties[0]}」（距離 {best_d}）"
        # 同距離命中多個 → 不亂吸，標人工確認（與數字欄多候選行為一致）
        return value, (f"⚠️ {label}「{value}」同距離命中多個白名單值"
                       f"（{'/'.join(ties[:3])}），請人工確認")
    return value, None


def _apply_lexicon(marks: dict) -> tuple:
    """對 _extract_marks 結果套封閉集合校正。回 (marks, corrections)。"""
    lex = _load_lexicon()
    corrections = []

    name, msg = _snap_to_whitelist(
        marks.get("customer_name"), lex.get("customers"),
        (lex.get("confusions") or {}).get("customer"), "客戶名")
    marks["customer_name"] = name
    if msg:
        corrections.append(msg)

    ref = marks.get("reference_entry") or {}
    key, msg = _snap_to_whitelist(
        ref.get("key"), lex.get("ref_keys"),
        (lex.get("confusions") or {}).get("ref_key"), "帳號代碼")
    ref["key"] = key
    if msg:
        corrections.append(msg)

    value = ref.get("value")
    valid_values = [str(v) for v in (lex.get("ref_values") or [])]
    if value and valid_values and value not in valid_values:
        scored = sorted(((_digit_distance(value, v), v) for v in valid_values))
        near = [v for d, v in scored if d <= 1.0]
        if len(near) == 1:
            ref["value"] = near[0]
            corrections.append(f"數字「{value}」依合法清單校正為「{near[0]}」")
        elif len(near) > 1:
            corrections.append(
                f"⚠️ 數字「{value}」不在合法清單，候選有 {('/'.join(near[:3]))}，請人工確認")
        else:
            corrections.append(f"⚠️ 數字「{value}」不在合法清單，請人工確認")
    marks["reference_entry"] = ref
    return marks, corrections


def _box_ocr_python() -> str:
    """OCR 專用 venv 的直譯器路徑（RED_BOX_OCR_PYTHON 可覆寫）。"""
    exe = os.environ.get("RED_BOX_OCR_PYTHON", "").strip()
    if exe:
        return os.path.abspath(os.path.expanduser(exe))
    from agent_core.logging_and_paths import RUNTIME_ROOT
    return os.path.join(RUNTIME_ROOT, "venvs", "box_ocr", "bin", "python")


def _run_local_ocr(paths: list) -> list:
    """subprocess 跑 box_ocr_runner，回 [{file, lines|error}]。失敗 raise。"""
    import subprocess

    py = _box_ocr_python()
    if not os.path.isfile(py):
        raise RuntimeError(
            "本機 OCR 引擎未安裝：請先跑 ./bin/setup-box-ocr 建立專用 venv"
            f"（找不到 {py}）"
        )
    from agent_core.logging_and_paths import REPO_ROOT
    runner = os.path.join(REPO_ROOT, "scripts", "box_ocr_runner.py")
    proc = subprocess.run(
        [py, runner, *paths],
        capture_output=True, text=True, timeout=_RUNNER_TIMEOUT_S,
    )
    payload = None
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith(_RESULT_MARKER):
            payload = json.loads(line[len(_RESULT_MARKER):])
            break
    if payload is None:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise RuntimeError(f"OCR runner 無結果輸出（exit {proc.returncode}）：{tail}")
    if not payload.get("ok"):
        raise RuntimeError(str(payload.get("error") or "unknown runner error"))
    return payload.get("results") or []


def _telegram_upload_root() -> str:
    try:
        from agent_core.exchange_policy import telegram_upload_root
        return os.path.realpath(telegram_upload_root())
    except Exception:
        root = os.environ.get("RED_TELEGRAM_UPLOAD_DIR", "").strip()
        if not root:
            root = os.path.join(os.path.expanduser("~"), "Downloads", "小紅-uploads")
        return os.path.realpath(os.path.abspath(os.path.expanduser(root)))


def _employee_scope_error(clean_path: str) -> str:
    """部門色員工 session 只能讀 Telegram 上傳目錄。回錯誤訊息；空字串＝放行。"""
    try:
        from agent_core.agents.permission_matrix import Agent
        from agent_core.rag_gateway import current_request_caller
        if current_request_caller() == Agent.RED:
            return ""
    except Exception:
        # 非 agent context（owner REPL / 主 bot）→ 不設限
        return ""
    # 上傳目錄按日期共用、無 per-chat 隔離 —— 員工傳資料夾（例如把自己上傳
    # 路徑去掉檔名）會把當天所有人上傳的圖一次 OCR 掉 → 資料夾批次僅 owner。
    if os.path.isdir(clean_path):
        return "❌ 員工帳號一次辨識一張：請直接把箱照傳進對話，用回覆裡顯示的檔案路徑。"
    root = _telegram_upload_root()
    real = os.path.realpath(clean_path)
    if real == root or real.startswith(root + os.sep):
        return ""
    return "❌ 員工帳號只能辨識剛從 Telegram 上傳的箱照，請直接把照片傳進對話。"


def _check_size(clean_path: str) -> str:
    size_mb = os.path.getsize(clean_path) / (1024 * 1024)
    if size_mb > _MAX_IMAGE_MB:
        return f"圖片太大（{size_mb:.1f}MB > {_MAX_IMAGE_MB}MB 上限）"
    return ""


def read_box_shipping_marks(image_path: str) -> str:
    """辨識出貨外箱上的「手寫嘜頭」→ 結構化 JSON（倉庫點貨 / WMS 登錄用）。

    純本機 OCR（PaddleOCR PP-OCRv6 medium，onnxruntime），不經 LLM、零 API 費用。
    抽三個欄位：箱頂手寫客戶名、「代碼:數字」帳號記法（→ key/value）、圈內
    件數數字。內建 0/90/180/270 旋轉投票（倒拍/橫拍自動救回）與客戶名/代碼
    白名單校正（校正動作列在 "corrections"）。自動忽略箱上印刷字。單張 ~3 秒。

    ⚠️ 回傳的 JSON 請**原樣轉告使用者**：欄位是 null 就如實說「辨識不到」，
    絕對不要自行推測、補值或拿範例湊數；有 "note" 就照著提醒重拍。
    使用者指出某欄辨識錯誤時，改呼叫 record_box_ocr_correction 記錄更正
    （系統會立即學起來並累積微調訓練樣本）。

    Args:
        image_path: 箱照檔案路徑（Telegram 上傳後對話裡顯示的「路徑」），
                    或裝多張箱照的資料夾路徑（批次點貨，一次最多 20 張、
                    整批共用一次模型載入）。

    Returns:
        JSON 字串。單張 = {"customer_name", "reference_entry": {"key","value"},
        "circled_count"}，視情況多 "corrections"（校正紀錄）與 "note"（重拍
        建議）；資料夾 = {"results": [{"file","ok","marks"|"error",…}…]}。
    """
    try:
        from agent_core.file_ops import _clean_path
        clean = _clean_path(image_path)
    except ValueError as e:
        return str(e)
    if not clean:
        return "❌ image_path 不能空"
    if not os.path.exists(clean):
        return f"❌ 找不到檔案或資料夾：{clean}"
    err = _employee_scope_error(clean)
    if err:
        return err

    note = ""
    if os.path.isdir(clean):
        files = sorted(
            os.path.join(clean, n)
            for n in os.listdir(clean)
            if os.path.splitext(n)[1].lower() in _IMG_EXTS
            and os.path.isfile(os.path.join(clean, n))
        )
        if not files:
            return ("❌ 資料夾裡沒有可辨識的圖片"
                    f"（支援 {'/'.join(sorted(e.lstrip('.') for e in _IMG_EXTS))}）")
        if len(files) > _MAX_BATCH:
            note = f"共 {len(files)} 張，只處理前 {_MAX_BATCH} 張，其餘請分批。"
            files = files[:_MAX_BATCH]
        single = False
    else:
        files = [clean]
        single = True

    # 超大檔在送引擎前就剔除（單張直接擋；批次記為該張 error）
    size_errors = {p: _check_size(p) for p in files}
    ok_files = [p for p in files if not size_errors[p]]
    if single and size_errors[clean]:
        return f"❌ {size_errors[clean]}"

    try:
        ocr_results = _run_local_ocr(ok_files) if ok_files else []
    except Exception as e:  # noqa: BLE001 — 工具錯誤回字串給 LLM，不冒泡
        return f"❌ 嘜頭辨識失敗：{type(e).__name__}: {str(e)[:300]}"

    by_file = {r.get("file"): r for r in ocr_results}
    if single:
        r = by_file.get(files[0]) or (ocr_results[0] if ocr_results else {})
        if "lines" not in r:
            return f"❌ 嘜頭辨識失敗：{str(r.get('error') or '引擎無輸出')[:300]}"
        marks, corrections = _apply_lexicon(_extract_marks(r["lines"]))
        out = dict(marks)
        if corrections:
            out["corrections"] = corrections
        if all(v is None for v in (marks["customer_name"],
                                   marks["reference_entry"]["key"],
                                   marks["reference_entry"]["value"],
                                   marks["circled_count"])):
            out["note"] = ("三個欄位都辨識不到 — 常見原因是斜拍/倒拍/太遠/反光。"
                           "請正對嘜頭、字擺水平、佔滿畫面重拍一張再試。")
        return json.dumps(out, ensure_ascii=False, indent=2)

    results = []
    for p in files:
        entry = {"file": os.path.basename(p), "ok": False}
        r = by_file.get(p) or {}
        if size_errors[p]:
            entry["error"] = size_errors[p]
        elif "lines" in r:
            marks, corrections = _apply_lexicon(_extract_marks(r["lines"]))
            entry["marks"] = marks
            if corrections:
                entry["corrections"] = corrections
            entry["ok"] = True
        else:
            entry["error"] = str(r.get("error") or "引擎無輸出")[:200]
        results.append(entry)
    out = {"results": results}
    if note:
        out["note"] = note
    return json.dumps(out, ensure_ascii=False, indent=2)


# ── 糾正回饋迴圈 ─────────────────────────────────────────────────────
_FIELD_ALIASES = {
    "customer": "customer", "客戶名": "customer", "客戶": "customer",
    "name": "customer", "customer_name": "customer",
    "ref_key": "ref_key", "key": "ref_key", "代碼": "ref_key", "帳號代碼": "ref_key",
    "ref_value": "ref_value", "value": "ref_value", "數字": "ref_value",
    "帳號數字": "ref_value",
    "count": "count", "件數": "count", "circled_count": "count",
}
_FIELD_MAX = {"customer": 20, "ref_key": 10, "ref_value": 10, "count": 4}
_TRAIN_TARGET = 500  # 微調啟動的建議樣本量（行）


def _training_dir() -> str:
    from agent_core.logging_and_paths import RUNTIME_ROOT
    return os.path.join(RUNTIME_ROOT, "data", "box_ocr_training")


def _archive_training_sample(image_path: str, field: str, wrong: str, correct: str) -> tuple:
    """存標註樣本：labels.jsonl 追加一行、照片副本以內容 hash 去重。

    回 (樣本總數, 照片是否已存)。
    """
    import hashlib
    from datetime import datetime

    tdir = _training_dir()
    os.makedirs(os.path.join(tdir, "images"), exist_ok=True)
    img_rel = ""
    if image_path and os.path.isfile(image_path):
        with open(image_path, "rb") as f:
            data = f.read()
        digest = hashlib.md5(data).hexdigest()[:12]
        ext = os.path.splitext(image_path)[1].lower() or ".jpg"
        img_rel = os.path.join("images", f"{digest}{ext}")
        dst = os.path.join(tdir, img_rel)
        if not os.path.exists(dst):
            with open(dst, "wb") as f:
                f.write(data)
    labels = os.path.join(tdir, "labels.jsonl")
    entry = {"ts": datetime.now().isoformat(timespec="seconds"),
             "image": img_rel, "field": field, "wrong": wrong, "correct": correct}
    with open(labels, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    with open(labels, encoding="utf-8") as f:
        total = sum(1 for line in f if line.strip())
    return total, bool(img_rel)


def _update_lexicon_from_correction(field: str, wrong: str, correct: str) -> list:
    """把更正寫進 lexicon。回「改了什麼」訊息 list。"""
    lex = _load_lexicon()
    changed = []
    list_key = {"customer": "customers", "ref_key": "ref_keys",
                "ref_value": "ref_values"}.get(field)
    if list_key:
        whitelist = lex.setdefault(list_key, [])
        if correct not in whitelist:
            whitelist.append(correct)
            changed.append(f"白名單 {list_key} 加入「{correct}」")
    if field in ("customer", "ref_key") and wrong and wrong != correct:
        conf = lex.setdefault("confusions", {}).setdefault(field, {})
        if conf.get(wrong) != correct:
            conf[wrong] = correct
            changed.append(f"混淆表加入「{wrong}」→「{correct}」")
    if changed:
        path = _lexicon_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(lex, f, ensure_ascii=False, indent=2)
    return changed


def record_box_ocr_correction(field: str, correct_text: str, wrong_text: str = "",
                              image_path: str = "") -> str:
    """記錄一筆嘜頭辨識的人工更正 → 立即更新 lexicon、並累積微調訓練樣本。

    使用時機：使用者指出 read_box_shipping_marks 認錯字時（例：「不是名媛，
    是名美」→ field="customer", wrong_text="名媛", correct_text="名美"）。
    一次一個欄位，多欄都錯就呼叫多次。

    效果：① 正確值進白名單、wrong→correct 進混淆表（下一張立即生效）；
    ② 照片＋標註存入 var/data/box_ocr_training/，累積到位（約 500 行）即可
    啟動 PP-OCRv6 rec 微調。

    Args:
        field: 哪個欄位 — "customer"(客戶名) / "ref_key"(帳號代碼) /
               "ref_value"(帳號數字) / "count"(件數)。
        correct_text: 正確的字（件數/帳號數字須為純數字）。
        wrong_text: OCR 誤讀成的字；沒有或辨識為空就留空。
        image_path: 該箱照路徑（能給就給，會存成訓練樣本）。
    """
    # 員工（部門色）session 不開放 — lexicon 影響所有人的後續辨識輸出
    try:
        from agent_core.agents.permission_matrix import Agent
        from agent_core.rag_gateway import current_request_caller
        if current_request_caller() != Agent.RED:
            return "❌ 員工帳號無法直接修正辨識字典，請把更正內容回報給大王。"
    except Exception:
        pass

    norm_field = _FIELD_ALIASES.get(str(field or "").strip().lower()) \
        or _FIELD_ALIASES.get(str(field or "").strip())
    if not norm_field:
        return ("❌ field 要是 customer(客戶名) / ref_key(帳號代碼) / "
                "ref_value(帳號數字) / count(件數) 其中之一")

    # 入庫前淨化：更正文字之後會進 lexicon → 回流 LLM context（corrections
    # 訊息），指令樣式內容不能被持久化（同 Round 7 慣例）
    correct = _clean_field(_normalize_text(str(correct_text or ""))) or ""
    wrong = _clean_field(_normalize_text(str(wrong_text or ""))) or ""
    if not correct:
        return "❌ correct_text 不能空"
    if len(correct) > _FIELD_MAX[norm_field]:
        return f"❌ correct_text 太長（{norm_field} 上限 {_FIELD_MAX[norm_field]} 字）"
    if norm_field in ("ref_value", "count") and not correct.isdigit():
        return f"❌ {norm_field} 必須是純數字，收到「{correct}」"
    wrong = wrong[:_FIELD_MAX[norm_field] * 2]

    clean_img = ""
    img_note = ""
    if str(image_path or "").strip():
        try:
            from agent_core.file_ops import _clean_path
            clean_img = _clean_path(image_path)
        except ValueError as e:
            return str(e)
        if not os.path.isfile(clean_img):
            clean_img, img_note = "", "（照片路徑找不到，只記文字標註）"

    changed = _update_lexicon_from_correction(norm_field, wrong, correct)
    total, has_img = _archive_training_sample(clean_img, norm_field, wrong, correct)

    parts = [f"✅ 已記錄更正：{norm_field}「{wrong or '(空)'}」→「{correct}」"]
    if changed:
        parts.append("；".join(changed) + "（下一張立即生效）")
    elif norm_field == "count":
        parts.append("件數屬開放數字，不進白名單，僅存訓練標註")
    parts.append(f"訓練樣本累計 {total} 筆"
                 + ("（含照片）" if has_img else img_note))
    if total >= _TRAIN_TARGET:
        parts.append(f"📈 已達 {_TRAIN_TARGET} 筆建議門檻，可以啟動 rec 模型微調了")
    return "\n".join(parts)


SKILL_TOOLS = [read_box_shipping_marks, record_box_ocr_correction]
