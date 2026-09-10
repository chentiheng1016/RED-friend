"""Google Drive → ChromaDB ingest pipeline.

Supported MIME types:
  application/vnd.google-apps.document     → export as plain text
  application/vnd.google-apps.spreadsheet  → export as CSV
  application/vnd.google-apps.presentation → export as .pptx (keeps slide images
    for OCR; falls back to plain text if the file exceeds Drive's 10 MB export cap)
  text/plain | text/csv | text/markdown | text/x-vcard
  text/html | text/xml | application/json   → fetched directly via get_media
  application/pdf                          → extracted with pypdf
  application/msword (.doc)                → extracted with macOS textutil
  application/vnd.ms-excel (.xls)          → extracted with xlrd
    ↳ Excel 2003 XML Spreadsheet mislabeled as .xls → extracted with ElementTree
  application/vnd.ms-powerpoint (.ppt)     → extracted with textutil/strings
  OpenDocument (.odt/.ods/.odp)            → extracted from content.xml
  application/illustrator (.ai)            → extracted as PDF text / strings fallback
  audio/video (opt-in)                     → transcribed/summarized with Gemini
  application/vnd.ms-outlook (.msg)        → extracted with olefile
  message/rfc822 (.eml) / MHTML (.mht)     → extracted with stdlib email
  application/rtf (.rtf)                   → extracted with macOS textutil
  …spreadsheetml.sheet/template (.xlsx/.xltx) → extracted with openpyxl
  …ms-excel.sheet.macroenabled.12 (.xlsm)  → extracted with openpyxl
  …wordprocessingml.document/template (.docx/.dotx) → extracted with python-docx
  …presentationml.presentation (.pptx)     → extracted with python-pptx
    (slide text + tables + speaker notes + embedded-image OCR)

Chunking:
  Plain text split into ~CHUNK_SIZE char overlapping windows.
  Each chunk stored as one ChromaDB document with metadata.

Entry points:
  sync_folder(folder_id)        — ingest all supported files in a Drive folder
  sync_file(file_id)            — ingest / re-sync a single file
  sync_shared_drive(drive_id)   — ingest a whole Shared Drive recursively (all subfolders)
  sync_all_drives()             — ingest ALL accessible Drive files (no folder constraint)
  sync_status()                 — returns dict with count + last_synced_at

  is_shared_drive_id(s)         — heuristic predicate for "0A…"-style Shared Drive root IDs
"""
from __future__ import annotations

import atexit
import hashlib
import io
import json
import logging
import multiprocessing as mp
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_core.env_utils import (
    env_bool as _env_bool,
    env_float as _env_float,
    env_int as _env_int,
)
from agent_core.logging_and_paths import DATA_DIR, _atomic_write_text
from agent_core.ingest.vector_store import (
    GeminiHardQuotaError,
    get_embedding_hard_quota_message,
    get_store,
)
from agent_core.rag_gateway import metadata_access_fields, metadata_access_matches
from agent_core.ingest import contextualize

_log = logging.getLogger(__name__)

CHUNK_SIZE = 600
CHUNK_OVERLAP = 80

_EXPORTABLE = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
_HTML_MIMES = {"text/html", "application/xhtml+xml"}
_DIRECT_TEXT = {
    "text/plain",
    "text/csv",
    "text/markdown",
    "text/xml",
    "application/xml",
    "application/json",
    "text/json",
    "text/x-sql",
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "text/vcard",
    "text/x-vcard",
    "text/x-url",
    "text/vtt",
    "application/dxf",
    "model/iges",
    "chemical/x-ncbi-asn1-ascii",
    "chemical/x-embl-dl-nucleotide",
    "application/postscript",
    "image/svg+xml",
    *_HTML_MIMES,
}

_PDF_MIME  = "application/pdf"
_XLS_MIME = "application/vnd.ms-excel"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_XLSM_MIME = "application/vnd.ms-excel.sheet.macroenabled.12"
_XLSM_MIME_CAMEL = "application/vnd.ms-excel.sheet.macroEnabled.12"
_XLTX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.template"
_SPREADSHEET_MIMES = {_XLS_MIME, _XLSX_MIME, _XLSM_MIME, _XLSM_MIME_CAMEL, _XLTX_MIME}
_DOC_MIME = "application/msword"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_DOTX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.template"
_PPT_MIME = "application/vnd.ms-powerpoint"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_GSLIDES_MIME = "application/vnd.google-apps.presentation"
_FOLDER_MIME = "application/vnd.google-apps.folder"
_MSG_MIME = "application/vnd.ms-outlook"
_EML_MIME = "message/rfc822"
_MHTML_MIME = "multipart/related"
_RTF_MIMES = {"application/rtf", "text/rtf", "application/x-rtf"}
_ODT_MIME = "application/vnd.oasis.opendocument.text"
_ODS_MIME = "application/vnd.oasis.opendocument.spreadsheet"
_ODP_MIME = "application/vnd.oasis.opendocument.presentation"
_XPS_MIME = "application/vnd.ms-xpsdocument"
_OCTET_MIME = "application/octet-stream"
_SHORTCUT_MIME = "application/x-ms-shortcut"
_GOOGLE_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
_VISIO_MIME = "application/vnd.visio"
_DWG_MIME = "image/vnd.dwg"
_AI_MIME = "application/illustrator"
_TNEF_MIME = "application/ms-tnef"

# Google 原生檔匯出成 Office **binary** 再走對應 binary extractor（≠ _EXPORTABLE
# 的純文字匯出）。Slides 走這條而非 text/plain：text/plain 匯出會**丟掉投影片圖片**，
# 而簡報常是圖片型（截圖、拍照、掃描）——匯出 .pptx 才能保留 ppt/media/ 裡的圖，
# 交給 _extract_pptx 內嵌 OCR。匯出的 bytes 走 pool binary extractor（與上傳的
# .pptx 同路徑，SIGSEGV 隔離）；超過 Drive 10MB 匯出上限時退回 text/plain（見
# _download_google_native）。
_EXPORTABLE_BINARY = {
    _GSLIDES_MIME: _PPTX_MIME,
}
_STRINGS_FALLBACK_MIMES = {
    _OCTET_MIME,
    _SHORTCUT_MIME,
    _VISIO_MIME,
    _DWG_MIME,
    _TNEF_MIME,
    "application/x-font-ttf",
    "font/ttf",
    "font/otf",
    "application/x-cab",
    "application/x-compressed",
    "application/x-diskcopy",
    "application/x-tar",
    "application/x-iwork-numbers-sffnumbers",
}
_DENIED_RAG_SUFFIXES = (".cdr", ".exe", ".chm", ".zip", ".rar")

# Image MIMEs handled by Gemini multimodal (OCR + visual caption in one call).
# Heic/heif need the SDK build to actually decode them; the call falls through
# to a Gemini-side error if not supported, which the outer try/except records.
# Formats Gemini Vision accepts directly.
_IMAGE_MIMES = {
    "image/png", "image/jpeg", "image/jpg",
    "image/webp", "image/gif",
    "image/heic", "image/heif",
}
# Formats Gemini rejects ("Unsupported MIME type") — PIL-convert to JPEG first
# via _extract_convertible_image. bmp/tiff/x-icon used to be in _IMAGE_MIMES
# (sent direct) and 400'd on every sync; routed here 2026-06-09 so they OCR.
_CONVERTIBLE_IMAGE_MIMES = {
    "image/x-photoshop",
    "image/x-nikon-nef",
    "application/x-msmetafile",
    "image/bmp",
    "image/tiff", "image/x-tiff",
    "image/x-icon",
}
_MEDIA_MIMES = {
    "audio/mpeg", "audio/mp3", "audio/mp4", "audio/x-m4a", "audio/wav", "audio/x-wav",
    "video/mp4", "video/quicktime", "video/x-msvideo", "video/mp2t",
}
# _MEDIA_MIMES 裡 Gemini 多模態「不」支援的容器：本地抽幀路徑吃得下（ffmpeg
# 讀得懂 MPEG-TS），但 Gemini fallback 上傳必 400 INVALID_ARGUMENT——走到
# fallback 就直接 raise 固定簽名標確定性錯誤，別每晚白傳一次。
_GEMINI_UNSUPPORTED_MEDIA_MIMES = {"video/mp2t"}

# 各 media 容器的 magic bytes（前綴 / 指定 offset 的 tag）。
#
# 為什麼需要：**Drive 的 mimeType 會說謊。** 2026-08-15 實例：兩個 5KB 的
# `zh_TW.ini` / `zh_CN.ini` 在 Drive 上的 mimeType 是 `audio/mpeg`，於是每晚被
# 當音訊上傳給 Gemini、每晚回一次 400 INVALID_ARGUMENT，兩檔從來沒被索引過。
# 對照組：_GEMINI_UNSUPPORTED_MEDIA_MIMES 是「mime 是對的、但 Gemini 不吃」，
# 這裡是「mime 根本就是錯的」——後者不該放棄檔案，該改走文字路徑把內容撈出來。
_MEDIA_MAGIC: dict[str, tuple[bytes, ...]] = {
    "audio/wav": (b"RIFF",),
    "audio/x-wav": (b"RIFF",),
    "video/x-msvideo": (b"RIFF",),
    "video/mp2t": (b"\x47",),
}
# 這些容器的 magic 在 offset 4（ftyp box），不是檔頭。
_MEDIA_MAGIC_AT_4: dict[str, tuple[bytes, ...]] = {
    "audio/mp4": (b"ftyp",),
    "audio/x-m4a": (b"ftyp",),
    "video/mp4": (b"ftyp",),
    "video/quicktime": (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"),
}
# MPEG audio（mp3）不能用前綴列舉，要按規格判 —— 見 _mpeg_audio_bytes_mismatch。
_MPEG_AUDIO_MIMES = {"audio/mpeg", "audio/mp3"}


def _mpeg_audio_bytes_mismatch(data: bytes) -> bool:
    """MPEG audio 專用判準：BOM 先判，再看 frame sync。

    為什麼不能像其他容器那樣列舉前綴：合法的 MPEG audio frame header 是
    **11 bits 的 sync**（byte0=0xFF、byte1 高 3 bits 全 1），後面的 version /
    layer 欄位把 byte1 撐開成 0xE0–0xFF 一整段。原本只列 Layer III 的四個值
    （FB/F3/F2/FA），於是這些**真音檔**全被判成「mime 說謊」、被丟去文字抽取，
    轉錄內容整個掉掉：

        MPEG2.5 LayerIII  0xE3   ← 8/11.025/12 kHz 低位元率語音
                                   （錄音筆、通訊軟體語音訊息就是這個）
        MPEG1   LayerII   0xFD
        MPEG2   LayerII   0xF4
        MPEG1   LayerI    0xFE

    ⚠️ 但**也不能只看 sync**：UTF-16 LE 的 BOM `\\xff\\xfe` 本身就是合法的
    MPEG1 LayerI sync（0xFE），只看 sync 會把 zh_TW.ini 那種誤標檔放行、
    400 照吃。BOM 是確定性證據（沒有影音容器以 BOM 開頭），所以順序固定為
    「BOM 先判 → 再看 sync」，兩層缺一不可。

    這個順序有個**已知且刻意**的取捨：`FF FE` 這兩個 byte 同時是 UTF-16 LE
    BOM 與合法的 MPEG1 LayerI sync，光看檔頭無法分辨，只能擇一。選 BOM 是因為
    兩邊發生率差太多 —— MPEG1 LayerI（.mp1，用於 DCC／Video CD 時代）現實中
    幾乎絕跡，而「UTF-16 文字檔被標成 audio/mpeg」是這個語料庫**現在就有兩支**
    的實況。LayerI 的另一個合法 header（`FF FF`）不衝突，照樣放行。
    """
    if _starts_with_text_bom(data):
        return True
    if data.startswith(b"ID3"):
        return False
    if len(data) < 2:
        return False    # 無從判斷 → 不表態
    return not (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)


def _media_bytes_mismatch(data: bytes, mime_type: str) -> bool:
    """bytes 看起來不像它自稱的 media 容器 → True（＝Drive 的 mime 說謊）。

    保守設計：**只有在確定不符時才回 True。** 沒收錄 magic 的 mime、或
    data 太短無從判斷，一律回 False 走原路徑 —— 寧可白傳一次，也不要把真的
    音訊/影片誤判成文字、丟掉它的轉錄內容。
    """
    if not data:
        return False
    if mime_type in _MPEG_AUDIO_MIMES:
        return _mpeg_audio_bytes_mismatch(data)
    head = _MEDIA_MAGIC.get(mime_type)
    if head:
        if len(data) < max(len(m) for m in head):
            return False
        return not data.startswith(head)
    at4 = _MEDIA_MAGIC_AT_4.get(mime_type)
    if at4:
        if len(data) < 12:
            return False
        return not data[4:8].startswith(at4)
    return False
_IGNORED_RAG_MIME_TYPES = {
    "application/x-msdownload",
    "application/x-msi",
    "application/x-msdos-program",
    "application/x-dosexec",
    "application/x-iso9660-image",
    "application/x-apple-diskimage",
    "application/x-7z-compressed",
    "application/x-gzip",
    "application/x-zip",
    "application/x-zip-compressed",
    "application/vnd.ms-pki.seccat",
    "application/vnd.ms-pki.stl",
    "application/x-x509-ca-cert",
    "application/x-pkcs7-certificates",
    "application/x-pkcs12",
    "application/msaccess",
    "application/vnd.palm",
    "application/x-xpinstall",
    _GOOGLE_SHORTCUT_MIME,
}
_IGNORED_RAG_SUFFIXES = (
    ".dll", ".msi", ".ocx",
    ".iso", ".dmg", ".7z", ".gz",
    ".cer", ".crt", ".p7b", ".pfx", ".p12", ".cat",
    ".mdb", ".pdb", ".xpi",
)

_COLLECTION = "drive_docs"
_SKIP_STATE_FILE = os.path.join(DATA_DIR, "drive_sync_skip_state.json")
_SKIP_STATE_LOCK = threading.RLock()
_SKIP_STATE_WRITE_LOCK = threading.Lock()
_SKIP_STATE_CACHE: dict[str, Any] | None = None
_SKIP_STATE_DEFER_DEPTH = 0
_SKIP_STATE_DIRTY = False
_SKIP_STATE_SAVE_GENERATION = 0
_SKIP_STATE_LAST_WRITTEN_GENERATION = 0
_SUPPORTED_MIMES_CACHE_KEY: tuple[Any, ...] | None = None
_SUPPORTED_MIMES_CACHE: tuple[str, ...] | None = None
_SUPPORTED_MIME_SET_CACHE: frozenset[str] | None = None
_SUPPORTED_MIMES_LOCK = threading.RLock()


# httplib2 has a socket timeout via google_auth.get_service(), but a slow
# streaming response can still make the sync look frozen. These wall-clock
# guards bound Drive RPCs and per-file download/extract work so one bad file
# cannot wedge the whole RAG pass.
_DRIVE_RPC_TIMEOUT_S = _env_int("RAG_DRIVE_RPC_TIMEOUT_S", 150, min_value=0)
_DRIVE_RPC_RETRY_MAX_ATTEMPTS = _env_int("RAG_DRIVE_RPC_RETRY_MAX_ATTEMPTS", 4, min_value=0)
_DRIVE_RPC_RETRY_BASE_SLEEP_S = _env_int("RAG_DRIVE_RPC_RETRY_BASE_SLEEP_S", 15, min_value=0)
_DRIVE_RPC_RETRY_MAX_SLEEP_S = _env_int("RAG_DRIVE_RPC_RETRY_MAX_SLEEP_S", 3600, min_value=0)
_DRIVE_FILE_TEXT_TIMEOUT_S = _env_int("RAG_DRIVE_FILE_TEXT_TIMEOUT_S", 600, min_value=0)
_DRIVE_TEXTUTIL_TIMEOUT_S = _env_int("RAG_DRIVE_TEXTUTIL_TIMEOUT_S", 60, min_value=0)
# LibreOffice headless 轉檔（xlrd 解析不了的真 BIFF .xls → xlsx）單檔上限。
# 要留在 _DRIVE_FILE_TEXT_TIMEOUT_S（600s）之內，否則 pool 看門狗會先動手。
_DRIVE_SOFFICE_TIMEOUT_S = _env_int("RAG_DRIVE_SOFFICE_TIMEOUT_S", 120, min_value=0)
_DRIVE_MAX_DOWNLOAD_MB = _env_int("RAG_DRIVE_MAX_DOWNLOAD_MB", 50, min_value=0)
_DRIVE_MAX_DOWNLOAD_BYTES = _DRIVE_MAX_DOWNLOAD_MB * 1024 * 1024
_DRIVE_SPREADSHEET_MAX_DOWNLOAD_MB = _env_int(
    "RAG_DRIVE_SPREADSHEET_MAX_DOWNLOAD_MB",
    350,
    min_value=0,
)
_DRIVE_SPREADSHEET_MAX_DOWNLOAD_BYTES = _DRIVE_SPREADSHEET_MAX_DOWNLOAD_MB * 1024 * 1024
# Extract-stage byte ceiling. The download gate above bounds the *download*,
# but binary extraction then holds the whole file in memory AND pickles it to
# a spawn subprocess (doubling it), and openpyxl/pypdf can balloon memory
# parsing it. A 75 MB xlsx (well under the 350 MB spreadsheet download gate)
# SIGSEGV'd the whole rag_sync daemon on 2026-06-01. Files larger than this
# are skipped (recorded as skip markers) instead of being handed to the
# extractor — kept generous (30 MB) so normal docs/sheets still extract.
_DRIVE_EXTRACT_MAX_MB = _env_int("RAG_DRIVE_EXTRACT_MAX_MB", 30, min_value=0)
_DRIVE_EXTRACT_MAX_BYTES = _DRIVE_EXTRACT_MAX_MB * 1024 * 1024
_DRIVE_EXTRACT_START_METHOD = os.environ.get("RAG_DRIVE_EXTRACT_START_METHOD", "spawn")
_DRIVE_MAX_CHUNKS_PER_FILE = _env_int("RAG_DRIVE_MAX_CHUNKS_PER_FILE", 500, min_value=0)
# Purge 防呆：Drive 偶爾短回清單卻謊報完整（incompleteSearch=False）——2026-06-28 倉庫
# 列到 1,098/實際 1,790 → purge 誤刪 ~700 已索引檔。若一輪要刪的佔已索引比例 > MAX_FRACTION
# 且絕對量 ≥ MIN_ABS，判定為可疑短清單、跳過 purge（檔在 Drive、下輪正常列檔自癒）。
_PURGE_MAX_FRACTION = _env_float("RAG_DRIVE_PURGE_MAX_FRACTION", 0.5, min_value=0.0)
_PURGE_MIN_ABS = _env_int("RAG_DRIVE_PURGE_MIN_ABS", 50, min_value=0)

# Image-specific size gate: skip icons/thumbnails (no useful content, but
# Gemini still bills the call) and oversized images (>10MB risks token /
# request limits, also slow). 5KB lower bound trims the vast majority of
# OS-generated thumbnails without losing legitimate small screenshots.
_IMAGE_MIN_BYTES = _env_int("RAG_IMAGE_MIN_BYTES", 5 * 1024, min_value=0)
_IMAGE_MAX_BYTES = _env_int("RAG_IMAGE_MAX_BYTES", 10 * 1024 * 1024, min_value=0)
# 這條斷路器比對的是 cost.jsonl 的當日金額，而那本帳的刻度換過三次：
#   紀元①（~2026-07-31）：_PRICING 嚴重低估 → 名目 $1 實際放行約 $670/日真金
#   紀元②（08-01 #328）：改用帳單數字，但那是**新台幣**誤當美元 → 放大 ~32×
#   紀元③（08-05 #353）：官方 USD 牌價，帳本數字終於就是美元
#
# ⚠️ 所以「把舊值乘除匯率」是錯的錨點——舊值本身在當時就沒有意義（#353 把預設
# 從 100 機械換算成 3.0，等於換算一個從未正確過的數字）。改用**設計意圖**定錨，
# 那個意圖在 plist 註解裡寫得很清楚：既要能在 1–2 週內排完 ~24k 張圖的積壓，
# 又要「capped so a Drive photo burst can't spend tens of USD in one run」。
#
# 紀元③的真實單價下，一張圖 OCR ≈ 1k tokens × $1.50/M ≈ $0.0015 → $5/日 約可處理
# 3,300 張，24k 張積壓約一週排完，且單日封頂 $5 遠低於「tens of USD」。
# 與 rag_sync_daily.plist 的部署值一致（有 test_drive_sync_cost_limits 釘住）。
_IMAGE_DAILY_COST_LIMIT_USD = _env_float("RAG_IMAGE_DAILY_COST_LIMIT_USD", 5.0, min_value=0.0)
_IMAGE_COST_CHECK_TTL_S = _env_int("RAG_IMAGE_COST_CHECK_TTL_S", 5, min_value=0)
# macOS 內建 Vision OCR（ocrmac）：文件類圖片的免費首選。抽到 ≥ MIN_CHARS 字就直接用它、
# 完全不打 Gemini、也不碰每日 OCR 預算閘（24k 圖片 backlog 因此免費清完）。非 macOS（Cloud
# Run）/ 沒裝 ocrmac / 抽不到足夠字（自然照片/手寫/糊）→ 回 None → fallback 到 Gemini vision
# （OCR+語意描述，行為與今日完全相同）。kill switch：RAG_VISION_OCR=0。
_VISION_OCR_ENABLED = _env_bool("RAG_VISION_OCR", True)
_VISION_OCR_MIN_CHARS = _env_int("RAG_VISION_OCR_MIN_CHARS", 24, min_value=1)
_VISION_OCR_LANGS = ("zh-Hant", "zh-Hans", "en-US", "vi-VT")
# 圖片只走 macOS Vision、**不退 Gemini**（大王指示）。開了之後 _extract_image：ocrmac 抽到
# 任何字就用、全無字（自然照片無文字）就跳過，完全不打 Gemini Vision——省成本、避開 Gemini
# preview 模型 high-demand 503（夜跑卡圖片的元凶）。取捨：無文字產品照失去 Gemini 視覺描述。
# 預設關（沿用 ocrmac→Gemini 舊行為）；設 1 啟用。
_IMAGE_VISION_ONLY = _env_bool("RAG_IMAGE_VISION_ONLY", False)
# 掃描型 PDF（無文字層）退路：pypdf 抽到空 → rasterize 逐頁 → 走上面的 ocrmac Vision OCR
# （本地免費、不碰 Gemini/503）。**預設關**：OCR 出的文字仍要 embed、會加重夜跑 embed 瓶頸，
# 故主力走離峰 backfill，夜跑 inline 退路預設關。詳見 docs/pdf_ocr_fallback_design.md。
_PDF_OCR_ENABLED = _env_bool("RAG_PDF_OCR", False)
_PDF_OCR_MAX_PAGES = _env_int("RAG_PDF_OCR_MAX_PAGES", 15, min_value=1)
_PDF_OCR_DPI = _env_int("RAG_PDF_OCR_DPI", 200, min_value=72, max_value=600)
_PDF_OCR_MAX_CHARS = _env_int("RAG_PDF_OCR_MAX_CHARS", 200_000, min_value=1)
_XLSX_EMBEDDED_IMAGE_MAX_COUNT = _env_int("RAG_XLSX_EMBEDDED_IMAGE_MAX_COUNT", 25, min_value=0)
_PPTX_EMBEDDED_IMAGE_MAX_COUNT = _env_int("RAG_PPTX_EMBEDDED_IMAGE_MAX_COUNT", 25, min_value=0)
_MEDIA_MAX_BYTES = _env_int("RAG_MEDIA_MAX_BYTES", 250 * 1024 * 1024, min_value=0)
# 同上（三個計價紀元的說明見 _IMAGE_DAILY_COST_LIMIT_USD）：不機械換算舊值，
# 直接對齊部署值 US$1/日。媒體走 flash-latest 抽幀 + pro-latest deep 分析，單件
# 比圖片貴得多，所以刻意比 image 那條緊。
_MEDIA_DAILY_COST_LIMIT_USD = _env_float("RAG_MEDIA_DAILY_COST_LIMIT_USD", 1.0, min_value=0.0)
_MEDIA_COST_CHECK_TTL_S = _env_int("RAG_MEDIA_COST_CHECK_TTL_S", 30, min_value=0)

# Image OCR/captioning is intentionally opt-in. A production Drive can contain
# tens of thousands of photos and thumbnails; including image MIME types in the
# listing query by default can turn a normal daily text ingest into a huge
# Gemini-generation job.
_DRIVE_ENABLE_IMAGE_INGEST = _env_bool("RAG_DRIVE_ENABLE_IMAGE_INGEST", False)
_DRIVE_ENABLE_MEDIA_INGEST = _env_bool("RAG_DRIVE_ENABLE_MEDIA_INGEST", False)
# 影片走「本地 ffmpeg 抽幀 + Gemini Vision 逐幀語意描述 + 音軌轉錄」。
# 整檔 inline 上傳受 Gemini ~20MB request 上限卡死，長片一發 call 的畫面
# 描述也淺；抽幀只送 N 張 jpeg，250MB 的影片也處理得動。媒體 ingest 本身
# 已是 opt-in（上面那支旗標），此旗標只控制影片的處理方式。
_DRIVE_ENABLE_VIDEO_FRAMES = _env_bool("RAG_DRIVE_ENABLE_VIDEO_FRAMES", True)
_VIDEO_MAX_FRAMES = _env_int("RAG_DRIVE_VIDEO_MAX_FRAMES", 8, min_value=1, max_value=32)
_VIDEO_AUDIO_MAX_MINUTES = _env_int("RAG_DRIVE_VIDEO_AUDIO_MAX_MINUTES", 30, min_value=1)

# 會議錄影資料夾白名單（逗號分隔 folder id）。名單內資料夾的影片改走本地
# whisper 中文音訊轉稿（會議畫面是頭像/投影片，抽幀只會存回無用的畫面描述，
# 只有語音有價值）；名單外的影片仍走 _DRIVE_ENABLE_VIDEO_FRAMES 抽幀。
# 見 _extract_meeting_transcript。教學影片（ERP 操作）不要放進來——它們要看螢幕。
_MEETING_FOLDER_IDS = frozenset(
    f.strip() for f in os.environ.get("RAG_MEETING_FOLDER_IDS", "").split(",") if f.strip()
)
_IMAGE_HARD_QUOTA_LOCK = threading.Lock()
_IMAGE_HARD_QUOTA_MESSAGE: str | None = None
_IMAGE_COST_LOCK = threading.Lock()
_IMAGE_COST_CHECKED_AT = 0.0
_IMAGE_COST_BUDGET_DATE: str | None = None
_IMAGE_COST_BUDGET_MESSAGE: str | None = None
_MEDIA_COST_LOCK = threading.Lock()
_MEDIA_COST_CHECKED_AT = 0.0
_MEDIA_COST_BUDGET_DATE: str | None = None
_MEDIA_COST_BUDGET_MESSAGE: str | None = None


class _WallClockTimeoutError(TimeoutError):
    """Raised when a worker thread timed out and may still be running."""


class _ExtractTooLargeError(Exception):
    """Raised when a file is too large to extract safely (would risk an
    OOM / SIGSEGV in the extractor or the pickle-to-subprocess hand-off)."""


def _run_with_timeout(timeout_s: int, label: str, fn):
    if timeout_s <= 0:
        return fn()
    holder: dict[str, Any] = {}

    def target() -> None:
        try:
            holder["value"] = fn()
        except BaseException as exc:
            holder["exc"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        _clear_google_service_cache()
        raise _WallClockTimeoutError(f"{label} exceeded {timeout_s}s")
    if "exc" in holder:
        raise holder["exc"]
    return holder.get("value")


def _process_context():
    try:
        return mp.get_context(_DRIVE_EXTRACT_START_METHOD)
    except ValueError:
        return mp.get_context("spawn")


def _extract_dispatch(op: str, mime_type: str, data: bytes) -> str:
    """Run one extraction. Lives at module level so the spawned worker (which
    re-imports this module) can call it. Only touches the input bytes + Gemini
    (API-key auth) — never the Drive OAuth service, so no token-file contention
    with the main process."""
    if op == "binary":
        return _extract_binary_text(mime_type, data)
    if op == "image":
        return _extract_image(data, mime_type)
    if op == "convertible_image":
        return _extract_convertible_image(data, mime_type)
    if op == "media":
        return _extract_media(data, mime_type)
    raise RuntimeError(f"unknown extract op: {op!r}")


def _persistent_extract_worker_main(conn) -> None:
    """Long-lived extraction worker. Receives {op, mime_type, input_path,
    output_dir} over the pipe, runs the (crash-prone, non-thread-safe) parser /
    Gemini call, and writes the result to output_dir — mirroring the temp-file
    handoff the old per-file worker used. A SIGSEGV here only kills this child;
    the parent detects the dead pipe and respawns. Input/result go through temp
    files (not the pipe) so a 250 MB media payload isn't pickled."""
    while True:
        try:
            req = conn.recv()
        except (EOFError, OSError):
            return
        if not isinstance(req, dict) or req.get("op") == "shutdown":
            return
        output_dir = Path(req["output_dir"])
        try:
            data = Path(req["input_path"]).read_bytes()
            text = _extract_dispatch(req["op"], req["mime_type"], data)
            (output_dir / "result.txt").write_text(text or "", encoding="utf-8")
            (output_dir / "status").write_text("ok", encoding="ascii")
        except GeminiHardQuotaError as exc:
            # Preserve the type across the process boundary — sync_file relies
            # on it to mark image/media ingest unavailable for the rest of the run.
            (output_dir / "error.txt").write_text(str(exc), encoding="utf-8", errors="replace")
            (output_dir / "status").write_text("hard_quota", encoding="ascii")
        except BaseException:
            (output_dir / "error.txt").write_text(
                traceback.format_exc(), encoding="utf-8", errors="replace"
            )
            (output_dir / "status").write_text("error", encoding="ascii")
        try:
            conn.send({"done": True})
        except (EOFError, OSError):
            return


class _ExtractPool:
    """Single persistent extraction worker the main process can forcibly kill.

    Replaces the old thread-with-wall-clock-timeout (which left an un-killable
    orphan thread alive in the crash-prone, non-thread-safe Gemini/grpc/httplib2
    stack — the 2026-06-02 SIGSEGV) and the spawn-per-file worker (which paid the
    interpreter+import cost on every one of ~24k files). The worker is spawned
    once and reused; on hang it is killed, on segfault it is respawned, and in
    both cases only the offending file fails while the daemon keeps running."""

    def __init__(self, worker_main=None) -> None:
        self._lock = threading.Lock()
        self._proc = None
        self._conn = None
        # Injectable for tests (must be a module-level fn so spawn can pickle it).
        self._worker_main = worker_main or _persistent_extract_worker_main

    def _teardown_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except BaseException:
                pass
            self._conn = None
        if self._proc is not None:
            try:
                if self._proc.is_alive():
                    self._proc.kill()
                self._proc.join(5)
            except BaseException:
                pass
            self._proc = None

    def _ensure_worker_locked(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            return
        self._teardown_locked()
        ctx = _process_context()
        parent_conn, child_conn = ctx.Pipe()
        proc = ctx.Process(
            target=self._worker_main,
            args=(child_conn,),
            daemon=True,
        )
        proc.start()
        child_conn.close()  # parent keeps only its end so EOF propagates on crash
        self._proc = proc
        self._conn = parent_conn

    def run(self, op: str, mime_type: str, data: bytes, label: str, timeout: int) -> str:
        # timeout <= 0 → run in-process (used by tests and as an escape hatch).
        # Mirrors _run_with_timeout's timeout<=0 semantics.
        if timeout is not None and timeout <= 0:
            return _extract_dispatch(op, mime_type, data)

        with self._lock:
            output_dir = Path(tempfile.mkdtemp(prefix="rag_drive_extract_"))
            input_path = output_dir / "input.bin"
            try:
                input_path.write_bytes(data)
                self._ensure_worker_locked()
                try:
                    self._conn.send(
                        {
                            "op": op,
                            "mime_type": mime_type,
                            "input_path": str(input_path),
                            "output_dir": str(output_dir),
                        }
                    )
                except BaseException as exc:
                    self._teardown_locked()
                    raise RuntimeError(f"{label} worker send failed: {exc}")

                if not self._conn.poll(timeout):
                    # Worker is wedged (e.g. a hung Gemini call). Kill it — the
                    # stuck call dies with the process; no orphan thread survives.
                    self._teardown_locked()
                    raise TimeoutError(f"{label} exceeded {timeout}s")

                try:
                    self._conn.recv()
                except (EOFError, OSError):
                    code = self._proc.exitcode if self._proc is not None else None
                    self._teardown_locked()
                    raise RuntimeError(f"{label} worker crashed (exitcode {code})")

                status_path = output_dir / "status"
                status = (
                    status_path.read_text(encoding="ascii").strip()
                    if status_path.exists()
                    else ""
                )
                if status == "ok":
                    return (output_dir / "result.txt").read_text(encoding="utf-8")
                err_path = output_dir / "error.txt"
                err = (
                    err_path.read_text(encoding="utf-8", errors="replace")
                    if err_path.exists()
                    else ""
                )
                if status == "hard_quota":
                    raise GeminiHardQuotaError(err.strip() or f"{label} hard quota")
                if status == "error":
                    last_line = err.strip().splitlines()[-1] if err.strip() else "unknown error"
                    raise RuntimeError(f"{label} failed: {last_line}")
                code = self._proc.exitcode if self._proc is not None else None
                raise RuntimeError(f"{label} worker produced no status (exitcode {code})")
            finally:
                shutil.rmtree(output_dir, ignore_errors=True)

    def shutdown(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.send({"op": "shutdown"})
                except BaseException:
                    pass
            self._teardown_locked()


_EXTRACT_POOL = _ExtractPool()
atexit.register(_EXTRACT_POOL.shutdown)


def _run_binary_extract_with_timeout(mime_type: str, data: bytes, label: str) -> str:
    # Guard BEFORE the worker hand-off: parsing a huge payload with
    # openpyxl/pypdf can OOM/SIGSEGV. Skip oversized files instead of risking it.
    if _DRIVE_EXTRACT_MAX_BYTES > 0 and len(data) > _DRIVE_EXTRACT_MAX_BYTES:
        raise _ExtractTooLargeError(
            f"{label}: {len(data)} bytes > extract limit "
            f"{_DRIVE_EXTRACT_MAX_BYTES} ({_DRIVE_EXTRACT_MAX_MB} MB)"
        )
    return _EXTRACT_POOL.run("binary", mime_type, data, label, _DRIVE_FILE_TEXT_TIMEOUT_S)


def _drive_retry_after_seconds(exc: BaseException) -> int | None:
    resp = getattr(exc, "resp", None)
    getter = getattr(resp, "get", None)
    if not callable(getter):
        return None
    try:
        raw = getter("retry-after") or getter("Retry-After")
        if raw is None:
            return None
        return max(0, int(raw))
    except Exception:
        return None


def _drive_http_status(exc: BaseException) -> int | None:
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status)
    except Exception:
        return None


def _is_retryable_drive_rpc_error(exc: BaseException) -> bool:
    # A wall-clock timeout leaves the request thread alive; retrying would
    # overlap non-thread-safe googleapiclient/httplib2 objects.
    if isinstance(exc, _WallClockTimeoutError):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    status = _drive_http_status(exc)
    msg = str(exc).lower()
    if status in {429, 500, 502, 503, 504}:
        return True
    if status == 403 and (
        "userratelimitexceeded" in msg
        or "user rate limit exceeded" in msg
        or "ratelimitexceeded" in msg
        or "rate limit exceeded" in msg
    ):
        return True
    return False


def _cap_drive_retry_sleep_seconds(sleep_s: float) -> float:
    if _DRIVE_RPC_RETRY_MAX_SLEEP_S <= 0:
        return 0.0
    return min(max(0.0, float(sleep_s)), float(_DRIVE_RPC_RETRY_MAX_SLEEP_S))


def _drive_retry_sleep_seconds(exc: BaseException, attempt: int) -> float:
    retry_after = _drive_retry_after_seconds(exc)
    if retry_after is not None:
        return _cap_drive_retry_sleep_seconds(float(retry_after))
    base = _DRIVE_RPC_RETRY_BASE_SLEEP_S * (2 ** max(0, attempt - 1))
    if base <= 0:
        return 0.0
    return _cap_drive_retry_sleep_seconds(float(base) + random.uniform(0.0, 1.0))


def _drive_request_factory(request_or_factory):
    if hasattr(request_or_factory, "execute"):
        return lambda: request_or_factory
    return request_or_factory


def _execute_drive_request(request_or_factory, label: str):
    request_factory = _drive_request_factory(request_or_factory)
    attempts = max(1, _DRIVE_RPC_RETRY_MAX_ATTEMPTS)
    attempt = 0
    while True:
        try:
            request = request_factory()
            return _run_with_timeout(_DRIVE_RPC_TIMEOUT_S, label, request.execute)
        except Exception as exc:
            attempt += 1
            if attempt >= attempts or not _is_retryable_drive_rpc_error(exc):
                raise
            sleep_s = _drive_retry_sleep_seconds(exc, attempt)
            _log.warning(
                "Drive RPC retry %s/%s %s: sleep %ss after %s",
                attempt,
                attempts,
                label,
                sleep_s,
                _summarize_exception(exc),
            )
            if sleep_s:
                time.sleep(sleep_s)


def _clear_google_service_cache() -> None:
    try:
        from agent_core import google_auth
        google_auth._service_cache.clear()
    except Exception:
        pass


def _download_size_bytes(meta: dict[str, Any]) -> int:
    try:
        return int(meta.get("size") or 0)
    except Exception:
        return 0


def _summarize_exception(exc: BaseException, limit: int = 240) -> str:
    msg = " ".join(str(exc).split())
    if len(msg) <= limit:
        return msg
    return msg[: max(0, limit - 3)] + "..."


def _is_hard_quota_error(exc: BaseException) -> bool:
    # 委派給 gemini_client 的單一事實來源，別再長出第三份複本
    # （prepayment credits 燒乾曾只有 gemini_client 認得，見 vector_store 同名函式）。
    from agent_core.gemini_client import _is_non_retryable_quota_error
    return _is_non_retryable_quota_error(str(exc))


def _latch_image_hard_quota(exc: BaseException) -> str:
    global _IMAGE_HARD_QUOTA_MESSAGE
    msg = _summarize_exception(exc)
    with _IMAGE_HARD_QUOTA_LOCK:
        if _IMAGE_HARD_QUOTA_MESSAGE is None:
            _IMAGE_HARD_QUOTA_MESSAGE = msg
        return _IMAGE_HARD_QUOTA_MESSAGE


def _get_image_hard_quota_message() -> str | None:
    with _IMAGE_HARD_QUOTA_LOCK:
        return _IMAGE_HARD_QUOTA_MESSAGE


def _caller_cost_today_usd(caller: str) -> float:
    """Return today's logged Gemini spend for one caller.

    The cost tracker timestamps are local ISO strings, so this uses local
    midnight to match cost_today()/cost_alert(). Importing lazily keeps Drive
    ingest usable in small test/runtime environments where the cost dashboard
    is not configured.
    """
    try:
        from agent_core.cost_tracker import _load_entries
    except Exception:
        return 0.0

    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = 0.0
    for entry in _load_entries(hours=25):
        if entry.get("caller") != caller:
            continue
        try:
            if datetime.fromisoformat(str(entry.get("ts", ""))) < midnight:
                continue
            total += float(entry.get("cost_usd") or 0.0)
        except (ValueError, TypeError):
            continue
    return total


def _get_image_cost_budget_message() -> str | None:
    """Latch a daily cost circuit-breaker for Drive image OCR.

    Image OCR is the only Drive ingest path that calls Gemini generation once
    per file. A Shared Drive can easily contain tens of thousands of images, so
    relying only on request-size gates is not enough. When today's
    drive_sync._extract_image spend reaches RAG_IMAGE_DAILY_COST_LIMIT_USD
    (default: $100, 2026-08-01 帳單校正後的真實美元刻度), this returns a stable message that sync_file treats like a
    quota stop: existing chunks are preserved and later files are skipped
    before download/extract.
    """
    global _IMAGE_COST_CHECKED_AT, _IMAGE_COST_BUDGET_DATE, _IMAGE_COST_BUDGET_MESSAGE
    if _IMAGE_DAILY_COST_LIMIT_USD <= 0:
        return None

    now = time.monotonic()
    today = datetime.now().date().isoformat()
    with _IMAGE_COST_LOCK:
        if _IMAGE_COST_BUDGET_DATE != today:
            _IMAGE_COST_BUDGET_DATE = today
            _IMAGE_COST_CHECKED_AT = 0.0
            _IMAGE_COST_BUDGET_MESSAGE = None
        if _IMAGE_COST_BUDGET_MESSAGE:
            return _IMAGE_COST_BUDGET_MESSAGE
        if _IMAGE_COST_CHECKED_AT and now - _IMAGE_COST_CHECKED_AT < _IMAGE_COST_CHECK_TTL_S:
            return None

    spent = _caller_cost_today_usd("drive_sync._extract_image")
    message = None
    if spent >= _IMAGE_DAILY_COST_LIMIT_USD:
        message = (
            f"daily Drive image OCR budget exceeded: "
            f"${spent:.4f} >= ${_IMAGE_DAILY_COST_LIMIT_USD:.2f}"
        )

    with _IMAGE_COST_LOCK:
        _IMAGE_COST_CHECKED_AT = now
        if message and _IMAGE_COST_BUDGET_MESSAGE is None:
            _IMAGE_COST_BUDGET_MESSAGE = message
        return _IMAGE_COST_BUDGET_MESSAGE


def _get_image_stop_message() -> str | None:
    return _get_image_hard_quota_message() or _get_image_cost_budget_message()


def _get_media_cost_budget_message() -> str | None:
    """Latch a daily cost circuit-breaker for Drive audio/video extraction."""
    global _MEDIA_COST_CHECKED_AT, _MEDIA_COST_BUDGET_DATE, _MEDIA_COST_BUDGET_MESSAGE
    if _MEDIA_DAILY_COST_LIMIT_USD <= 0:
        return None

    now = time.monotonic()
    today = datetime.now().date().isoformat()
    with _MEDIA_COST_LOCK:
        if _MEDIA_COST_BUDGET_DATE != today:
            _MEDIA_COST_BUDGET_DATE = today
            _MEDIA_COST_CHECKED_AT = 0.0
            _MEDIA_COST_BUDGET_MESSAGE = None
        if _MEDIA_COST_BUDGET_MESSAGE:
            return _MEDIA_COST_BUDGET_MESSAGE
        if _MEDIA_COST_CHECKED_AT and now - _MEDIA_COST_CHECKED_AT < _MEDIA_COST_CHECK_TTL_S:
            return None

    spent = _caller_cost_today_usd("drive_sync._extract_media")
    message = None
    if spent >= _MEDIA_DAILY_COST_LIMIT_USD:
        message = (
            f"daily Drive media transcription budget exceeded: "
            f"${spent:.4f} >= ${_MEDIA_DAILY_COST_LIMIT_USD:.2f}"
        )

    with _MEDIA_COST_LOCK:
        _MEDIA_COST_CHECKED_AT = now
        if message and _MEDIA_COST_BUDGET_MESSAGE is None:
            _MEDIA_COST_BUDGET_MESSAGE = message
        return _MEDIA_COST_BUDGET_MESSAGE


def _get_media_stop_message() -> str | None:
    return _get_image_hard_quota_message() or _get_media_cost_budget_message()


def _sync_complete_allows_skip(existing: dict[str, Any]) -> bool:
    value = existing.get("sync_complete", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no"}
    return bool(value)


def _load_skip_state() -> dict[str, Any]:
    global _SKIP_STATE_CACHE
    with _SKIP_STATE_LOCK:
        if _SKIP_STATE_CACHE is not None:
            return _SKIP_STATE_CACHE

    try:
        with open(_SKIP_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    files = data.get("files")
    if not isinstance(files, dict):
        files = {}
    loaded = {"version": 1, "files": files}

    with _SKIP_STATE_LOCK:
        if _SKIP_STATE_CACHE is None:
            _SKIP_STATE_CACHE = loaded
        return _SKIP_STATE_CACHE


def _snapshot_skip_state_unlocked(state: dict[str, Any]) -> dict[str, Any]:
    files = state.get("files")
    snapshot_files = {}
    if isinstance(files, dict):
        snapshot_files = {
            file_id: dict(marker) if isinstance(marker, dict) else marker
            for file_id, marker in files.items()
        }
    return {"version": state.get("version", 1), "files": snapshot_files}


def _write_skip_state_snapshot(generation: int, snapshot: dict[str, Any]) -> None:
    global _SKIP_STATE_LAST_WRITTEN_GENERATION
    text = json.dumps(snapshot, ensure_ascii=False, indent=2)
    with _SKIP_STATE_WRITE_LOCK:
        with _SKIP_STATE_LOCK:
            if (
                generation < _SKIP_STATE_SAVE_GENERATION
                or generation <= _SKIP_STATE_LAST_WRITTEN_GENERATION
            ):
                return
        # Disk I/O intentionally happens outside _SKIP_STATE_LOCK so marker
        # readers/writers are not blocked on filesystem latency.
        _save_skip_state_text(text)
        with _SKIP_STATE_LOCK:
            _SKIP_STATE_LAST_WRITTEN_GENERATION = max(
                _SKIP_STATE_LAST_WRITTEN_GENERATION,
                generation,
            )


def _persist_skip_state_request(
    save_request: tuple[int, dict[str, Any]] | None,
    *,
    suppress_errors: bool = False,
) -> None:
    if not save_request:
        return
    try:
        _write_skip_state_snapshot(*save_request)
    except Exception:
        _log.exception("failed to persist Drive skip state")
        if not suppress_errors:
            raise


def _save_skip_state_text(text: str) -> None:
    os.makedirs(os.path.dirname(_SKIP_STATE_FILE) or ".", exist_ok=True)
    _atomic_write_text(_SKIP_STATE_FILE, text)


def _save_or_defer_skip_state_unlocked(
    state: dict[str, Any],
) -> tuple[int, dict[str, Any]] | None:
    global _SKIP_STATE_DIRTY, _SKIP_STATE_SAVE_GENERATION
    if _SKIP_STATE_DEFER_DEPTH > 0:
        _SKIP_STATE_DIRTY = True
        return None
    _SKIP_STATE_SAVE_GENERATION += 1
    return _SKIP_STATE_SAVE_GENERATION, _snapshot_skip_state_unlocked(state)


class _DeferredSkipStateWrites:
    def __enter__(self):
        global _SKIP_STATE_DEFER_DEPTH
        with _SKIP_STATE_LOCK:
            _SKIP_STATE_DEFER_DEPTH += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        global _SKIP_STATE_DEFER_DEPTH, _SKIP_STATE_DIRTY
        save_request = None
        state = _load_skip_state()
        with _SKIP_STATE_LOCK:
            _SKIP_STATE_DEFER_DEPTH = max(0, _SKIP_STATE_DEFER_DEPTH - 1)
            if _SKIP_STATE_DEFER_DEPTH == 0 and _SKIP_STATE_DIRTY:
                _SKIP_STATE_DIRTY = False
                save_request = _save_or_defer_skip_state_unlocked(state)
        _persist_skip_state_request(save_request, suppress_errors=exc_type is not None)
        return False


def _defer_skip_state_writes() -> _DeferredSkipStateWrites:
    return _DeferredSkipStateWrites()


def _skip_marker_size_bytes(marker: dict[str, Any], reason: str) -> int:
    raw_size = marker.get("size_bytes")
    if raw_size is not None:
        try:
            return int(raw_size)
        except Exception:
            return 0
    # Legacy markers written before structured size_bytes was stored.
    match = re.search(r"too_large: (\d+) bytes", reason)
    return int(match.group(1)) if match else 0


# Permanent, file-content extraction failures: the file's bytes cannot be
# parsed on any run — encryption without a usable password, structural
# corruption, or a decompression-bomb guard. Detected by substring — class
# name where the message is generic, message text where the class name doesn't
# survive — because extraction runs in the _ExtractPool subprocess and
# surfaces here as a RuntimeError carrying only the original error's LAST
# traceback line (a multi-line exception message loses its class name and all
# but its final line). Re-attempted only when the file's modifiedTime changes
# (e.g. a decrypted copy is re-uploaded).
_PERMANENT_EXTRACT_ERROR_SIGNATURES = (
    "FileNotDecryptedError",   # encrypted PDF, no/incorrect password
    "PdfStreamError",          # truncated / structurally corrupt PDF
    "EmptyFileError",          # zero-byte / empty PDF
    "PdfReadError",            # generic malformed-PDF parse failure
    "LimitReachedError",       # pypdf decompression-bomb guard tripped
    "BadZipFile",              # corrupt docx/xlsx/pptx (zip container)
    # xlrd.biffh.XLRDError message for password-protected .xls — xlrd has no
    # decryption support at all, so the same bytes fail on every run.
    "Workbook is encrypted",
    # pypdf NotImplementedError for PDFs whose /Filter isn't the Standard
    # security handler (e.g. certificate/AES-based encryption) — pypdf can
    # never open these, deterministic in the file's bytes.
    "only Standard PDF encryption handler is available",
    # openpyxl.utils.exceptions.InvalidFileException — load_workbook's
    # magic-byte check: the bytes aren't an OOXML zip at all (misnamed legacy
    # .xls, .xlsb, or corrupt container). Class name is unique to openpyxl.
    "InvalidFileException",
    # xlrd.biffh.XLRDError for an OLE2 compound document without a Workbook
    # stream (typically Office-encrypted .xls, or a non-Excel OLE2 file
    # misnamed .xls) — xlrd can never open it.
    "Can't find workbook in OLE2 compound document",
    # xlrd.biffh.XLRDError for OOXML bytes misnamed .xls — xlrd ≥ 2.0 dropped
    # xlsx support entirely, so the same bytes fail on every run.
    "Excel xlsx file; not supported",
    # openpyxl.reader.excel raises ValueError("Unable to read workbook: …\n…\n
    # Please see the exception for more details.") around any invalid-XML
    # parse failure inside the workbook. The message spans three lines and the
    # extract-pool wrapper keeps only the LAST traceback line, so neither the
    # class name nor the first line survives — match the final sentence.
    "Please see the exception for more details.",
    # openpyxl TypeError from descriptor validation while parsing corrupt
    # conditional-formatting/style ranges. Class name is just "TypeError", so
    # match the full openpyxl-specific message instead.
    "expected <class 'openpyxl.worksheet.cell_range.MultiCellRange'>",
    # _extract_media 的確定性守門：Gemini 不支援的媒體容器（video/mp2t）在
    # 抽幀也救不回時，同一份 bytes 每晚上傳必 400 INVALID_ARGUMENT——標永久。
    "media_mime_unsupported_by_gemini",
)


def _permanent_extract_error_signature(exc: BaseException) -> str:
    msg = str(exc)
    for sig in _PERMANENT_EXTRACT_ERROR_SIGNATURES:
        if sig in msg:
            return sig
    return type(exc).__name__


def _is_permanent_extract_error(exc: BaseException) -> bool:
    """True when a Drive extraction error will recur identically every run.

    Conservative by design: only known file-content failures qualify, so
    transient network/API errors keep bubbling up as retryable `error`s.
    """
    return any(sig in str(exc) for sig in _PERMANENT_EXTRACT_ERROR_SIGNATURES)


def _looks_like_spreadsheet_name(title: str) -> bool:
    return title.strip().lower().endswith((".xls", ".xlsx", ".xlsm", ".xltx"))


def _download_limit_bytes_for_mime(mime_type: str) -> int:
    if mime_type in _SPREADSHEET_MIMES:
        return _DRIVE_SPREADSHEET_MAX_DOWNLOAD_BYTES
    if mime_type in _MEDIA_MIMES:
        return _MEDIA_MAX_BYTES
    return _DRIVE_MAX_DOWNLOAD_BYTES


def _download_limit_bytes_for_skip_marker(marker: dict[str, Any]) -> int:
    mime_type = str(marker.get("mime_type", ""))
    if mime_type in _SPREADSHEET_MIMES or _looks_like_spreadsheet_name(str(marker.get("title", ""))):
        return _DRIVE_SPREADSHEET_MAX_DOWNLOAD_BYTES
    if mime_type in _MEDIA_MIMES:
        return _MEDIA_MAX_BYTES
    return _DRIVE_MAX_DOWNLOAD_BYTES


def _is_drive_ignored_file_type(name: str, mime_type: str) -> bool:
    clean = (name or "").strip().lower()
    return (mime_type or "") in _IGNORED_RAG_MIME_TYPES or clean.endswith(_IGNORED_RAG_SUFFIXES)


_PDF_OCR_CAPABLE_CACHE: bool | None = None


def _pdf_ocr_capable() -> bool:
    """RAG_PDF_OCR=1 之外，環境是否真跑得動 PDF OCR 鏈（pypdfium2 rasterize +
    macOS Vision OCR）。跑不動（Cloud Run / 沒裝依賴 / RAG_VISION_OCR=0）時，
    放行端不放行重試（重下載也抽不出字）、記錄端不蓋 ocr_attempted（環境修好
    後舊 marker 才會被放行重試）。import 探測結果 process 內快取。"""
    global _PDF_OCR_CAPABLE_CACHE
    if _PDF_OCR_CAPABLE_CACHE is None:
        capable = False
        if _VISION_OCR_ENABLED:
            try:
                import pypdfium2  # noqa: F401

                from agent_core.vision_ocr import available as _vision_available

                capable = _vision_available()
            except Exception:  # noqa: BLE001 — 沒裝 pypdfium2（Cloud Run）
                capable = False
        _PDF_OCR_CAPABLE_CACHE = capable
    return _PDF_OCR_CAPABLE_CACHE


def _marker_is_pdf(marker: dict[str, Any]) -> bool:
    """skip-marker 是否屬於 PDF（供 OCR 退路重試判斷）。新 marker 帶 mime_type；
    舊的 empty_text marker（OCR 上線前寫的）沒存 mime → 退而用 title 副檔名判。"""
    if str(marker.get("mime_type", "")) == _PDF_MIME:
        return True
    return str(marker.get("title", "")).lower().endswith(".pdf")


def _skip_marker_matches(
    marker: dict[str, Any],
    modified_time: str,
    folder_id: str,
    drive_id: str,
    file_id: str = "",
) -> bool:
    reason = str(marker.get("reason", ""))
    if reason == "empty_text":
        # 開 RAG_PDF_OCR 後，「未經 OCR」的舊 empty_text 掃描 PDF 視為待重試（OCR 退路
        # 會抽出字）；OCR 跑過仍空的 marker 帶 ocr_attempted（見 _prepare_file 記錄處），
        # 沿用 modifiedTime 檢查——否則印章/相片掃描 PDF 每晚重下載重 OCR 無限循環。
        # 非 PDF 的 empty_text（圖片/空表）仍沿用 modifiedTime 檢查，不無謂重抽。
        # _pdf_ocr_capable：flag 開著但環境跑不動 OCR（Cloud Run / 缺依賴）時
        # 不放行——重下載也抽不出字，且沒真 OCR 就不該消耗這次重試機會。
        if (
            _PDF_OCR_ENABLED
            and _marker_is_pdf(marker)
            and not marker.get("ocr_attempted")
            and _pdf_ocr_capable()
        ):
            return False
    elif reason == "unsupported mime_type":
        mime_type = str(marker.get("mime_type", ""))
        if mime_type and mime_type in _supported_mime_set():
            return False
        if _is_drive_ignored_file_type(str(marker.get("title", "")), mime_type):
            return False
    elif reason == "duplicate_content":
        canonical = str(marker.get("canonical_doc_id", ""))
        content_hash = str(marker.get("content_hash", ""))
        if not canonical or not content_hash:
            return False
        # Self-clear if no other complete copy still indexes this content IN
        # THE SAME SCOPE, so sync_file re-embeds this file rather than leaving
        # the bytes orphaned out of the index after the canonical copy was
        # deleted from Drive. Scope must match the gate's same-scope rule,
        # otherwise the marker would survive even though a scoped query can no
        # longer reach the content.
        store = get_store(_COLLECTION)
        finder = getattr(store, "find_duplicate_doc_id", None)
        if not callable(finder) or not finder(
            content_hash, file_id, drive_id, folder_id
        ):
            return False
    elif reason.startswith("too_large:"):
        size_bytes = _skip_marker_size_bytes(marker, reason)
        limit_bytes = _download_limit_bytes_for_skip_marker(marker)
        if limit_bytes <= 0:
            return False
        if size_bytes and size_bytes <= limit_bytes:
            return False
    elif reason.startswith("extract_too_large:"):
        # Honor the extract-stage size skip so the daemon doesn't re-download
        # + re-crash on the same oversized file every sync. Re-attempt only if
        # the extract limit has since been raised above the recorded size.
        size_bytes = _skip_marker_size_bytes(marker, reason)
        limit_bytes = _DRIVE_EXTRACT_MAX_BYTES
        if limit_bytes <= 0:
            return False
        if size_bytes and size_bytes <= limit_bytes:
            return False
    elif reason == "ignored filename":
        if not _is_drive_junk_file(marker.get("title") or ""):
            return False
    elif reason == "ignored file_type":
        if not _is_drive_ignored_file_type(
            marker.get("title") or "",
            str(marker.get("mime_type", "")),
        ):
            return False
    elif reason.startswith("extract_error:"):
        # Permanent parse failure (encrypted/corrupt/decompression-bomb). Honor
        # the marker unconditionally; the modifiedTime check below re-attempts
        # only when the file's bytes change.
        pass
    elif reason == "meeting_folder_non_video":
        # 會議資料夾政策性跳過（Meet 英文轉稿/Gemini 自動筆記）。資料夾若已
        # 不再列為會議資料夾，marker 自動失效讓檔案重新入索引；否則沿用下方
        # modifiedTime 檢查。沒有這個分支 marker 寫了也沒人認 → 每晚重抓。
        if not _is_meeting_folder(folder_id or str(marker.get("folder_id") or "")):
            return False
    else:
        return False
    if not modified_time:
        return False
    if marker.get("modified_time", "") != modified_time:
        return False
    if drive_id and marker.get("drive_id", "") != drive_id:
        return False
    if folder_id and marker.get("folder_id", "") != folder_id:
        return False
    return True


def _get_matching_skip_marker(
    file_id: str,
    *,
    modified_time: str,
    folder_id: str,
    drive_id: str,
) -> dict[str, Any] | None:
    state = _load_skip_state()
    with _SKIP_STATE_LOCK:
        marker = state.get("files", {}).get(file_id)
        if isinstance(marker, dict) and _skip_marker_matches(
            marker,
            modified_time,
            folder_id,
            drive_id,
            file_id,
        ):
            return dict(marker)
    return None


def _record_skip_marker(
    file_id: str,
    *,
    reason: str,
    modified_time: str,
    folder_id: str,
    drive_id: str,
    title: str,
    extra: dict[str, Any] | None = None,
) -> None:
    if not modified_time:
        return
    now = datetime.now(timezone.utc).isoformat()
    state = _load_skip_state()
    save_request = None
    with _SKIP_STATE_LOCK:
        marker = {
            "reason": reason,
            "modified_time": modified_time,
            "folder_id": folder_id,
            "drive_id": drive_id,
            "title": title,
            "seen_at": now,
        }
        if extra:
            marker.update(extra)
        state.setdefault("files", {})[file_id] = marker
        save_request = _save_or_defer_skip_state_unlocked(state)
    _persist_skip_state_request(save_request)


def _clear_skip_marker(file_id: str) -> None:
    state = _load_skip_state()
    save_request = None
    with _SKIP_STATE_LOCK:
        files = state.setdefault("files", {})
        if file_id not in files:
            return
        files.pop(file_id, None)
        save_request = _save_or_defer_skip_state_unlocked(state)
    _persist_skip_state_request(save_request)


# ── chunk helpers ────────────────────────────────────────────────────

def _chunk_text(text: str, title: str = "", context: str = "") -> list[str]:
    """Split text into ~CHUNK_SIZE windows, each carrying document-level context
    as a prefix so a chunk reading "保固期 12 個月" keeps its connection to
    "ABC合約_2026Q1.docx" at the embedding level (helps customer/spec-name queries).

    Two prefix modes:
    - `context` given (Contextual Retrieval, flag on): prefix is the LLM-written
      doc summary followed by a blank line. payload_size stays CHUNK_SIZE — the
      prefix is bolted on, NOT charged against the window — so chunk boundaries
      depend only on the body, keeping `__c{i}` ids aligned when a file is
      re-embedded to a newer ctx_ver so upsert overwrites cleanly (design §3.2).
    - else (flag off, today's behaviour verbatim): prefix is `[title] `, and the
      window shrinks by len(prefix) to stay under the embedding budget.
      Pathologically long titles (capped at 80 chars) keep at least 1 char.
    """
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if not text:
        return []
    if context:
        prefix = f"{context}\n\n"
        payload_size = CHUNK_SIZE
    else:
        # Cap the prefix so a freakishly long filename doesn't squeeze payload to zero.
        safe_title = (title or "")[:80]
        prefix = f"[{safe_title}] " if safe_title else ""
        payload_size = max(1, CHUNK_SIZE - len(prefix))

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + payload_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(prefix + chunk)
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def _cap_chunks_for_index(
    chunks: list[str],
    file_id: str,
    title: str,
) -> tuple[list[str], int]:
    source_count = len(chunks)
    if _DRIVE_MAX_CHUNKS_PER_FILE <= 0 or source_count <= _DRIVE_MAX_CHUNKS_PER_FILE:
        return chunks, source_count
    print(
        f"[rag_sync] Drive file {file_id} {title!r}: "
        f"truncate chunks {source_count}->{_DRIVE_MAX_CHUNKS_PER_FILE}",
        flush=True,
    )
    return chunks[:_DRIVE_MAX_CHUNKS_PER_FILE], source_count


# ── binary extractors ───────────────────────────────────────────────
# Each takes raw bytes and returns extracted text. Exceptions bubble:
# sync_folder/sync_shared_drive's outer try/except records them as
# "error: …" without deleting any existing chunks for the file.

def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    noisy_loggers = [logging.getLogger("pypdf"), logging.getLogger("pypdf._reader")]
    previous_levels = [logger.level for logger in noisy_loggers]
    try:
        for logger in noisy_loggers:
            logger.setLevel(logging.ERROR)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception as exc:
                    raise RuntimeError(f"encrypted_pdf: {exc}") from exc
            parts: list[str] = []
            for page in reader.pages:
                text = page.extract_text() or ""
                if text.strip():
                    parts.append(text)
            return "\n\n".join(parts)
    finally:
        for logger, level in zip(noisy_loggers, previous_levels):
            logger.setLevel(level)


def _rasterize_pdf(data: bytes, max_pages: int, dpi: int) -> list[bytes]:
    """PDF bytes → 逐頁 PNG bytes（最多 max_pages 頁，超過記 log 不靜默截斷）。

    用 pypdfium2（已是相依，與 _extract_pdf 的 pypdf 同在 requirements-docs.txt）render。
    沒裝 / 解檔失敗 → 回 []，呼叫端維持 empty_text（行為同今日）。跑在 _ExtractPool 子程序內，
    與 _extract_pdf 同；頁數上限避免一份大掃描檔卡死整條抽取。
    """
    try:
        import pypdfium2 as pdfium  # PDFium render；to_pil 需要 Pillow（已裝）
    except Exception:  # noqa: BLE001 — 沒裝（Cloud Run / 未安裝）→ 交回 empty_text
        return []
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception:  # noqa: BLE001 — 壞檔 / 非 PDF
        return []
    pages: list[bytes] = []
    try:
        page_count = len(pdf)
        scale = dpi / 72.0
        for i in range(page_count):
            if i >= max_pages:
                print(
                    f"[rag_sync] PDF OCR: 超過 {max_pages} 頁上限，截斷"
                    f"（共 {page_count} 頁）",
                    flush=True,
                )
                break
            try:
                pil = pdf[i].render(scale=scale).to_pil()
                buf = io.BytesIO()
                pil.save(buf, format="PNG")
                pages.append(buf.getvalue())
            except Exception:  # noqa: BLE001 — 單頁 render 失敗：跳過該頁、保留其餘
                continue
    finally:
        try:
            pdf.close()
        except Exception:  # noqa: BLE001
            pass
    return pages


def _extract_pdf_ocr(data: bytes) -> str:
    """掃描型 PDF（無文字層）退路：rasterize 逐頁 → ocrmac Vision OCR（本地、免費、不碰 Gemini/503）。

    回 OCR 文字；未啟用（RAG_PDF_OCR=0）/ 非 macOS / 沒裝 pypdfium2 / rasterize 失敗 / 全空 → 回 ""，
    維持原本 empty_text 行為。OCR 出的文字仍要 embed，故本退路預設關（見模組頂的旗標說明）。
    """
    if not _PDF_OCR_ENABLED:
        return ""
    pages = _rasterize_pdf(data, _PDF_OCR_MAX_PAGES, _PDF_OCR_DPI)
    if not pages:
        return ""
    parts: list[str] = []
    total = 0
    for idx, png in enumerate(pages):
        page_text = _extract_image_vision(png, "image/png")
        if not page_text:
            continue
        parts.append(page_text)
        total += len(page_text)
        if total >= _PDF_OCR_MAX_CHARS:
            print(
                f"[rag_sync] PDF OCR: 達字數上限 {_PDF_OCR_MAX_CHARS}，於第 {idx + 1} 頁停",
                flush=True,
            )
            break
    return "\n\n".join(parts).strip()


def _extract_xlsx(data: bytes) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        parts: list[str] = []
        for sheet in wb.worksheets:
            parts.append(f"# {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    parts.append("\t".join(cells))
        embedded_images = _extract_xlsx_embedded_images(data)
        if embedded_images:
            parts.append(embedded_images)
        return "\n".join(parts)
    finally:
        wb.close()


_IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


def _image_mime_from_name(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return _IMAGE_MIME_BY_SUFFIX.get(suffix, "")


def _prepare_image_for_gemini(
    data: bytes,
    mime_type: str,
    *,
    force_convert: bool = False,
) -> tuple[bytes, str]:
    """Return image bytes small enough for Gemini, downsampling when practical."""
    size = len(data)
    if size < _IMAGE_MIN_BYTES:
        return b"", mime_type
    if size <= _IMAGE_MAX_BYTES and not force_convert:
        return data, mime_type
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        image.load()
        if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
            background = Image.new("RGB", image.size, (255, 255, 255))
            rgba = image.convert("RGBA")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")

        best: bytes | None = None
        for max_edge in (3072, 2048, 1536, 1024):
            resized = image.copy()
            resized.thumbnail((max_edge, max_edge))
            for quality in (85, 75, 65, 55):
                buf = io.BytesIO()
                resized.save(buf, format="JPEG", quality=quality, optimize=True)
                candidate = buf.getvalue()
                best = candidate if best is None or len(candidate) < len(best) else best
                if len(candidate) <= _IMAGE_MAX_BYTES:
                    return candidate, "image/jpeg"
        if best and len(best) <= _IMAGE_MAX_BYTES:
            return best, "image/jpeg"
    except Exception as exc:
        _log.warning("failed to downsample image for Gemini: %s", _summarize_exception(exc))
    return b"", mime_type


def _extract_convertible_image(data: bytes, mime_type: str) -> str:
    prepared, prepared_mime = _prepare_image_for_gemini(data, mime_type, force_convert=True)
    if not prepared:
        return ""
    return _extract_image(prepared, prepared_mime)


def _extract_xlsx_embedded_images(data: bytes) -> str:
    if _XLSX_EMBEDDED_IMAGE_MAX_COUNT <= 0:
        return ""

    import zipfile

    parts: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            image_names = [
                name
                for name in sorted(archive.namelist())
                if name.startswith("xl/media/") and _image_mime_from_name(name)
            ]
            for idx, name in enumerate(image_names[:_XLSX_EMBEDDED_IMAGE_MAX_COUNT], 1):
                raw = archive.read(name)
                mime_type = _image_mime_from_name(name)
                prepared, prepared_mime = _prepare_image_for_gemini(raw, mime_type)
                if not prepared:
                    continue
                try:
                    text = _extract_image(prepared, prepared_mime)
                except GeminiHardQuotaError as exc:
                    parts.append(f"# Embedded image OCR skipped\n{exc}")
                    break
                except Exception as exc:
                    parts.append(
                        f"# Embedded image {idx}: {name}\n"
                        f"[image OCR failed: {_summarize_exception(exc)}]"
                    )
                    continue
                if text.strip():
                    parts.append(f"# Embedded image {idx}: {name}\n{text}")
            if len(image_names) > _XLSX_EMBEDDED_IMAGE_MAX_COUNT:
                parts.append(
                    "# Embedded image OCR truncated\n"
                    f"Processed {_XLSX_EMBEDDED_IMAGE_MAX_COUNT} of {len(image_names)} images."
                )
    except zipfile.BadZipFile:
        return ""
    return "\n\n".join(parts)


def _decode_text_bytes(data: bytes) -> str:
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return _decode_text_bytes(value)
    return str(value)


def _extract_html(data: bytes | str) -> str:
    raw = data if isinstance(data, str) else _decode_text_bytes(data)
    if not raw.strip():
        return ""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw)

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _extract_text_by_mime(mime_type: str, data: bytes) -> str:
    if mime_type in _HTML_MIMES:
        return _extract_html(data)
    return _decode_text_bytes(data)


def _extract_strings(data: bytes, suffix: str = ".bin", min_length: int = 4) -> str:
    """Best-effort text salvage for legacy binary formats without a parser."""
    strings_bin = shutil.which("strings")
    if not strings_bin:
        return _decode_text_bytes(data)

    temp_dir = Path(tempfile.mkdtemp(prefix="rag_drive_strings_"))
    try:
        input_path = temp_dir / f"input{suffix}"
        input_path.write_bytes(data)
        proc = subprocess.run(
            [strings_bin, "-n", str(min_length), str(input_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_DRIVE_TEXTUTIL_TIMEOUT_S or None,
            check=False,
        )
        if proc.returncode != 0:
            return _decode_text_bytes(data)
        lines: list[str] = []
        seen: set[str] = set()
        for raw_line in _decode_text_bytes(proc.stdout).splitlines():
            line = raw_line.strip()
            if not line or line in seen:
                continue
            seen.add(line)
            lines.append(line)
        return "\n".join(lines)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _looks_text_like(data: bytes) -> bool:
    if not data:
        return True
    sample = data[:4096]
    if b"\x00" in sample:
        return False
    control = sum(1 for b in sample if b < 32 and b not in {9, 10, 12, 13})
    return control / max(1, len(sample)) < 0.05


# BOM → codec。有 BOM 就是**確定性證據**，不必猜。
_TEXT_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),   # 要排在 utf-16-le 之前（前綴相同）
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def _starts_with_text_bom(data: bytes) -> bool:
    """開頭是文字編碼 BOM → 這份 bytes 是文字檔，不可能是任何影音容器。

    給 _mpeg_audio_bytes_mismatch 當「確定性證據」用：MPEG 的 frame sync 判準
    寬到會放行 UTF-16 LE 的 BOM（0xFF 0xFE），得靠這條先攔下來。
    """
    return any(data.startswith(bom) for bom, _codec in _TEXT_BOMS)


def _decode_bom_text(data: bytes) -> str:
    """有 BOM 的文字檔照 BOM 解碼；沒 BOM 或解不開回空字串。

    為什麼需要：`_looks_text_like` / `_extract_strings` 都假設單位元組或 UTF-8。
    UTF-16 的內容每隔一個 byte 是 `\\x00`，走 strings 只會抽出一堆碎片
    （實例：Drive 上一個 UTF-16 LE 的 `zh_TW.ini`，5,364 bytes 抽出來是
    `'Bf@S\\n2QX[\\nck8^'` 這種亂碼）。**亂碼進索引比抽不到更糟** —— 抽不到至少
    看得出來，亂碼會被當成內容。
    """
    for bom, codec in _TEXT_BOMS:
        if data.startswith(bom):
            try:
                text = data.decode(codec, errors="replace")
            except Exception:
                return ""
            # utf-8-sig 自己吃掉 BOM，utf-16/32 解出來會留一個 ZWNBSP
            return text.lstrip("﻿")
    return ""


def _extract_textlike_binary(data: bytes) -> str:
    bom_text = _decode_bom_text(data)
    if bom_text.strip():
        return bom_text
    if _looks_text_like(data):
        return _decode_text_bytes(data)
    return _extract_strings(data)


def _format_xls_cell(book, cell) -> str:
    import xlrd
    if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
        return ""
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(cell.value, book.datemode).isoformat(sep=" ")
        except Exception:
            return str(cell.value).strip()
    if cell.ctype == xlrd.XL_CELL_NUMBER and isinstance(cell.value, float):
        if cell.value.is_integer():
            return str(int(cell.value))
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if cell.value else "FALSE"
    return str(cell.value).strip()


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _xml_attr(elem, name: str) -> str:
    for key, value in elem.attrib.items():
        if _xml_local_name(key) == name:
            return str(value)
    return ""


def _looks_like_xml_spreadsheet(data: bytes) -> bool:
    head = _decode_text_bytes(data[:1024]).lstrip().lower()
    return head.startswith("<?xml") and ("spreadsheet" in head or "<workbook" in head)


def _spreadsheet_xml_cell_text(cell) -> str:
    data_nodes = [elem for elem in cell.iter() if _xml_local_name(elem.tag) == "Data"]
    sources = data_nodes or [cell]
    pieces: list[str] = []
    for source in sources:
        text = " ".join(part.strip() for part in source.itertext() if part and part.strip())
        if text:
            pieces.append(text)
    return re.sub(r"\s+", " ", " ".join(pieces)).strip()


def _extract_spreadsheet_xml(data: bytes) -> str:
    # defusedxml：Drive 檔案內容為 untrusted（多員工＋外部方可投放）。stdlib
    # ElementTree 會展開 DTD 內部實體 → billion-laughs（~1KB 檔炸成多 GB，繞過
    # _DRIVE_EXTRACT_MAX_BYTES）。defusedxml.fromstring forbid_entities 直接擋掉。
    import defusedxml.ElementTree as ET

    root = ET.fromstring(data.lstrip())
    worksheets = [elem for elem in root.iter() if _xml_local_name(elem.tag) == "Worksheet"]
    if not worksheets and _xml_local_name(root.tag) == "Worksheet":
        worksheets = [root]

    parts: list[str] = []
    for sheet_idx, worksheet in enumerate(worksheets, 1):
        title = _xml_attr(worksheet, "Name") or f"Sheet{sheet_idx}"
        parts.append(f"# {title}")
        for row in (elem for elem in worksheet.iter() if _xml_local_name(elem.tag) == "Row"):
            cells = [
                text
                for text in (
                    _spreadsheet_xml_cell_text(cell)
                    for cell in row
                    if _xml_local_name(cell.tag) == "Cell"
                )
                if text
            ]
            if cells:
                parts.append("\t".join(cells))
    return "\n".join(parts)


def _zip_member_text(data: bytes, names: tuple[str, ...]) -> str:
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in names:
            try:
                with archive.open(name) as fh:
                    return fh.read().decode("utf-8", errors="replace")
            except KeyError:
                continue
    return ""


def _extract_odf_text(data: bytes) -> str:
    # defusedxml：Drive 檔案內容為 untrusted（多員工＋外部方可投放）。stdlib
    # ElementTree 會展開 DTD 內部實體 → billion-laughs（~1KB 檔炸成多 GB，繞過
    # _DRIVE_EXTRACT_MAX_BYTES）。defusedxml.fromstring forbid_entities 直接擋掉。
    import defusedxml.ElementTree as ET

    raw = _zip_member_text(data, ("content.xml",))
    if not raw.strip():
        return ""
    root = ET.fromstring(raw)
    parts: list[str] = []
    for elem in root.iter():
        if _xml_local_name(elem.tag) not in {"h", "p"}:
            continue
        text = re.sub(r"\s+", " ", " ".join(t.strip() for t in elem.itertext() if t.strip())).strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def _extract_ods(data: bytes) -> str:
    # defusedxml：Drive 檔案內容為 untrusted（多員工＋外部方可投放）。stdlib
    # ElementTree 會展開 DTD 內部實體 → billion-laughs（~1KB 檔炸成多 GB，繞過
    # _DRIVE_EXTRACT_MAX_BYTES）。defusedxml.fromstring forbid_entities 直接擋掉。
    import defusedxml.ElementTree as ET

    raw = _zip_member_text(data, ("content.xml",))
    if not raw.strip():
        return ""
    root = ET.fromstring(raw)
    parts: list[str] = []
    for table_idx, table in enumerate((e for e in root.iter() if _xml_local_name(e.tag) == "table"), 1):
        title = _xml_attr(table, "name") or f"Sheet{table_idx}"
        parts.append(f"# {title}")
        for row in (e for e in table if _xml_local_name(e.tag) == "table-row"):
            cells: list[str] = []
            for cell in row:
                if _xml_local_name(cell.tag) != "table-cell":
                    continue
                text = re.sub(
                    r"\s+",
                    " ",
                    " ".join(t.strip() for t in cell.itertext() if t.strip()),
                ).strip()
                if text:
                    cells.append(text)
            if cells:
                parts.append("\t".join(cells))
    return "\n".join(parts)


def _extract_xls(data: bytes) -> str:
    import xlrd
    try:
        # logfile 導進 StringIO 丟棄：xlrd 撞 NAME formula 解析限制時（見下方
        # soffice 退路）會把 evaluate_name_formula 的 debug dump 直噴預設
        # logfile=sys.stdout，一晚上百檔會灌爆 daemon log。
        book = xlrd.open_workbook(
            file_contents=data, on_demand=True, logfile=io.StringIO()
        )
    except (xlrd.XLRDError, AssertionError, UnicodeDecodeError):
        # AssertionError：xlrd 解 SST（shared string table）時的內部斷言
        # （book.py handle_sst 的 `assert _unused_i == nstrings - 1`）——會計
        # Drive 的 PKL packing list 類 .xls 會炸這個而非 XLRDError，內容其實
        # 完好，soffice 轉檔可救；str() 是空字串，re-raise 時 skip-marker
        # 簽名分類會 fallback 到 type name，維持 retryable。
        # UnicodeDecodeError：同為 SST 解析（越南文舊檔藏非法 utf-16 編碼，
        # "'utf-16-le' codec can't decode …: illegal encoding"，年終獎金系列）
        # ——一樣繞過 XLRDError、soffice 轉檔實測可救（33K+ chars 完整表格）。
        # 副檔名誤標 .xls 的 OOXML（zip magic）：xlrd ≥ 2.0 一律拒收
        # （"Excel xlsx file; not supported"），但內容完好，改走 openpyxl。
        # 容器真的壞掉時 openpyxl 會炸 BadZipFile / InvalidFileException，
        # 照樣冒出（兩者都在 _PERMANENT_EXTRACT_ERROR_SIGNATURES）。
        if data[:4] == b"PK\x03\x04":
            return _extract_xlsx(data)
        if _looks_like_xml_spreadsheet(data):
            return _extract_spreadsheet_xml(data)
        # 真 BIFF 但 xlrd 解析不了——會計 Drive 2017-19 舊檔（匯款/銀行/收支）
        # 撞 "Excessive indirect references in NAME formula" 的 xlrd 已知限制，
        # 儲存格內容其實完好：LibreOffice headless 轉成 xlsx 再抽。轉不了
        # （soffice 未裝／轉檔失敗）就讓 xlrd 原錯誤冒出——維持 retryable，
        # 也保留 skip-marker 依原簽名分類（如 "Workbook is encrypted"）。
        converted = _convert_xls_to_xlsx_via_soffice(data)
        if converted is None:
            raise
        return _extract_xlsx(converted)
    try:
        parts: list[str] = []
        for sheet in book.sheets():
            parts.append(f"# {sheet.name}")
            for row_idx in range(sheet.nrows):
                cells = [
                    text
                    for text in (_format_xls_cell(book, cell) for cell in sheet.row(row_idx))
                    if text
                ]
                if cells:
                    parts.append("\t".join(cells))
        return "\n".join(parts)
    finally:
        release = getattr(book, "release_resources", None)
        if callable(release):
            release()


# launchd daemon 的 PATH 沒有 /Applications 下的 app bundle，逐一探測。
_SOFFICE_CANDIDATES = (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/opt/homebrew/bin/soffice",
    "/usr/local/bin/soffice",
)


def _find_soffice() -> str | None:
    found = shutil.which("soffice")
    if found:
        return found
    for candidate in _SOFFICE_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def _convert_xls_to_xlsx_via_soffice(data: bytes) -> bytes | None:
    """xlrd 解析不了的真 BIFF .xls → LibreOffice headless 轉出 xlsx bytes。

    soffice 不存在或轉檔失敗回 None（記 warning），由 caller 重新 raise
    xlrd 原錯誤：錯誤保持 retryable，裝上 LibreOffice 後下一輪自動救回。
    """
    soffice = _find_soffice()
    if not soffice:
        return None
    temp_dir = Path(tempfile.mkdtemp(prefix="rag_drive_soffice_"))
    try:
        input_path = temp_dir / "input.xls"
        input_path.write_bytes(data)
        proc = subprocess.run(
            [
                soffice,
                "--headless",
                # 每次轉檔用獨立 profile：共用 profile 有單一 instance 鎖，
                # 另一個 LibreOffice（GUI 或並行轉檔）開著時 headless 會
                # 靜默失敗（exit 0、無輸出檔）。
                f"-env:UserInstallation=file://{temp_dir}/profile",
                "--convert-to",
                "xlsx",
                "--outdir",
                str(temp_dir),
                str(input_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_DRIVE_SOFFICE_TIMEOUT_S or None,
            check=False,
        )
        output_path = temp_dir / "input.xlsx"
        # returncode 0 但沒輸出檔也是失敗（soffice 的靜默失敗模式）。
        if proc.returncode != 0 or not output_path.exists():
            err = _decode_text_bytes(proc.stderr).strip()
            _log.warning(
                "soffice xls→xlsx 轉檔失敗（exit %s）: %s", proc.returncode, err or "無輸出檔"
            )
            return None
        return output_path.read_bytes()
    except subprocess.TimeoutExpired:
        _log.warning("soffice xls→xlsx 轉檔逾時（%ss）", _DRIVE_SOFFICE_TIMEOUT_S)
        return None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _extract_with_textutil(data: bytes, suffix: str) -> str:
    textutil = shutil.which("textutil")
    if not textutil:
        raise RuntimeError("legacy_doc_extractor_unavailable: textutil not found")

    timeout = None if _DRIVE_TEXTUTIL_TIMEOUT_S <= 0 else _DRIVE_TEXTUTIL_TIMEOUT_S
    temp_dir = Path(tempfile.mkdtemp(prefix="rag_drive_textutil_"))
    try:
        input_path = temp_dir / f"input{suffix}"
        input_path.write_bytes(data)
        proc = subprocess.run(
            [textutil, "-convert", "txt", "-stdout", str(input_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            err = _decode_text_bytes(proc.stderr).strip()
            raise RuntimeError(f"textutil_extract_failed: {err or proc.returncode}")
        return _decode_text_bytes(proc.stdout)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _extract_doc(data: bytes) -> str:
    return _extract_with_textutil(data, ".doc")


def _extract_ppt(data: bytes) -> str:
    try:
        text = _extract_with_textutil(data, ".ppt")
    except RuntimeError:
        return _extract_strings(data, ".ppt")
    return text if text.strip() else _extract_strings(data, ".ppt")


def _extract_rtf(data: bytes) -> str:
    return _extract_with_textutil(data, ".rtf")


def _extract_docx(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    parts: list[str] = []
    for p in doc.paragraphs:
        if p.text and p.text.strip():
            parts.append(p.text)
    for tbl in doc.tables:
        for row in tbl.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append("\t".join(cells))
    return "\n".join(parts)


def _decode_msg_text_stream(stream_name: str, data: bytes) -> str:
    if stream_name.endswith("001F"):
        text = data.decode("utf-16-le", errors="replace")
    elif stream_name.endswith("001E"):
        text = data.decode("cp1252", errors="replace")
    else:
        text = _decode_text_bytes(data)
    return text.rstrip("\x00").strip()


def _read_msg_stream_bytes(ole, path: list[str]) -> bytes | None:
    try:
        with ole.openstream(path) as stream:
            return stream.read()
    except Exception:
        return None


def _read_msg_property_text(ole, prop_id: str, storage: str = "") -> str:
    for type_id in ("001F", "001E"):
        stream_name = f"__substg1.0_{prop_id}{type_id}"
        path = [storage, stream_name] if storage else [stream_name]
        data = _read_msg_stream_bytes(ole, path)
        if data:
            return _decode_msg_text_stream(stream_name, data)
    return ""


def _read_msg_property_bytes(ole, prop_id: str, type_id: str, storage: str = "") -> bytes | None:
    stream_name = f"__substg1.0_{prop_id}{type_id}"
    path = [storage, stream_name] if storage else [stream_name]
    return _read_msg_stream_bytes(ole, path)


def _read_msg_attachment_names(ole) -> list[str]:
    storages = sorted(
        {
            path[0]
            for path in ole.listdir(streams=True, storages=False)
            if path and str(path[0]).startswith("__attach")
        }
    )
    names: list[str] = []
    for storage in storages:
        for prop_id in ("3707", "3704", "3001"):
            name = _read_msg_property_text(ole, prop_id, storage)
            if name:
                names.append(name)
                break
    return names


def _extract_msg(data: bytes) -> str:
    import olefile

    ole = olefile.OleFileIO(io.BytesIO(data))
    try:
        parts: list[str] = []
        field_values = (
            ("Subject", _read_msg_property_text(ole, "0037")),
            (
                "From",
                _read_msg_property_text(ole, "0C1A")
                or _read_msg_property_text(ole, "0C1F")
                or _read_msg_property_text(ole, "5D01"),
            ),
            ("To", _read_msg_property_text(ole, "0E04")),
            ("Cc", _read_msg_property_text(ole, "0E03")),
        )
        for label, value in field_values:
            if value:
                parts.append(f"{label}: {value}")

        body = _read_msg_property_text(ole, "1000")
        html_body = _read_msg_property_bytes(ole, "1013", "0102")
        if body:
            parts.append(body)
        elif html_body:
            parts.append(_extract_html(html_body))

        attachment_names = _read_msg_attachment_names(ole)
        if attachment_names:
            parts.append("Attachments:\n" + "\n".join(f"- {name}" for name in attachment_names))
        return "\n\n".join(part for part in parts if part.strip())
    finally:
        close = getattr(ole, "close", None)
        if callable(close):
            close()


def _extract_eml(data: bytes) -> str:
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(data)
    parts: list[str] = []
    for header in ("Subject", "From", "To", "Cc", "Date"):
        value = message.get(header)
        if value:
            parts.append(f"{header}: {value}")

    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachment_names: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        content_type = part.get_content_type()
        if filename or disposition == "attachment":
            if filename:
                attachment_names.append(filename)
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            content = _decode_text_bytes(payload)
        text = _coerce_text(content).strip()
        if not text:
            continue
        if content_type == "text/plain":
            plain_parts.append(text)
        elif content_type == "text/html":
            html_parts.append(_extract_html(text))

    if plain_parts:
        parts.append("\n\n".join(plain_parts))
    elif html_parts:
        parts.append("\n\n".join(part for part in html_parts if part.strip()))
    if attachment_names:
        parts.append("Attachments:\n" + "\n".join(f"- {name}" for name in attachment_names))
    return "\n\n".join(part for part in parts if part.strip())


def _extract_mhtml(data: bytes) -> str:
    return _extract_eml(data)


def _extract_pptx(data: bytes) -> str:
    """Slide text + table cells + speaker notes + embedded-image OCR, slide-by-slide.

    Embedded images are OCR'd from the ppt/media/ archive
    (_extract_pptx_embedded_images) and appended after the slide text, reusing
    the same local-Vision-first path as standalone image files — many decks are
    picture-heavy (screenshots, photos, scans) with little text on the slide
    itself. Slides without any extractable text drop out of the result so empty
    cover slides don't pollute the chunk stream.
    """
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    slides: list[str] = []
    for idx, slide in enumerate(prs.slides, 1):
        parts = [f"# Slide {idx}"]
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    parts.append(t)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        parts.append("\t".join(cells))
        if getattr(slide, "has_notes_slide", False):
            note = slide.notes_slide.notes_text_frame.text.strip()
            if note:
                parts.append(f"[Speaker notes]: {note}")
        if len(parts) > 1:  # header alone doesn't count
            slides.append("\n".join(parts))
    body = "\n\n".join(slides)
    embedded = _extract_pptx_embedded_images(data)
    if embedded:
        body = f"{body}\n\n{embedded}" if body else embedded
    return body


def _extract_pptx_embedded_images(data: bytes) -> str:
    """OCR images embedded in a .pptx, read straight from the ppt/media/ archive.

    Mirrors _extract_xlsx_embedded_images: scanning the zip (not walking
    python-pptx shapes) also catches pictures inside group shapes and on slide
    layouts/masters, and the archive stores each image once so a picture reused
    across slides is OCR'd once. Local Vision OCR is free; the Gemini fallback
    (when Vision finds no text) is bounded by the per-image size gate and the
    daily OCR cost circuit-breaker inside _extract_image.
    """
    if _PPTX_EMBEDDED_IMAGE_MAX_COUNT <= 0:
        return ""

    import zipfile

    parts: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            image_names = [
                name
                for name in sorted(archive.namelist())
                if name.startswith("ppt/media/") and _image_mime_from_name(name)
            ]
            for idx, name in enumerate(image_names[:_PPTX_EMBEDDED_IMAGE_MAX_COUNT], 1):
                raw = archive.read(name)
                mime_type = _image_mime_from_name(name)
                prepared, prepared_mime = _prepare_image_for_gemini(raw, mime_type)
                if not prepared:
                    continue
                try:
                    text = _extract_image(prepared, prepared_mime)
                except GeminiHardQuotaError as exc:
                    parts.append(f"# Embedded image OCR skipped\n{exc}")
                    break
                except Exception as exc:
                    parts.append(
                        f"# Embedded image {idx}: {name}\n"
                        f"[image OCR failed: {_summarize_exception(exc)}]"
                    )
                    continue
                if text.strip():
                    parts.append(f"# Embedded image {idx}: {name}\n{text}")
            if len(image_names) > _PPTX_EMBEDDED_IMAGE_MAX_COUNT:
                parts.append(
                    "# Embedded image OCR truncated\n"
                    f"Processed {_PPTX_EMBEDDED_IMAGE_MAX_COUNT} of {len(image_names)} images."
                )
    except zipfile.BadZipFile:
        return ""
    return "\n\n".join(parts)


def _extract_xps(data: bytes) -> str:
    import zipfile
    # defusedxml：Drive 檔案內容為 untrusted（多員工＋外部方可投放）。stdlib
    # ElementTree 會展開 DTD 內部實體 → billion-laughs（~1KB 檔炸成多 GB，繞過
    # _DRIVE_EXTRACT_MAX_BYTES）。defusedxml.fromstring forbid_entities 直接擋掉。
    import defusedxml.ElementTree as ET

    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in sorted(archive.namelist()):
            if not name.lower().endswith((".fpage", ".xml")):
                continue
            raw = archive.read(name)
            try:
                root = ET.fromstring(raw)
            except Exception:
                continue
            page_parts: list[str] = []
            for elem in root.iter():
                text = elem.attrib.get("UnicodeString")
                if text and text.strip():
                    page_parts.append(text.strip())
            if page_parts:
                parts.append("\n".join(page_parts))
    return "\n\n".join(parts)


def _extract_ai(data: bytes) -> str:
    pdf_text = ""
    if data.lstrip().startswith(b"%PDF"):
        try:
            pdf_text = _extract_pdf(data)
        except Exception:
            pdf_text = ""
    string_text = _extract_strings(data, ".ai")
    return "\n\n".join(part for part in (pdf_text, string_text) if part.strip())


def _extract_image_vision(data: bytes, mime_type: str) -> str | None:
    """macOS 內建 Vision OCR（在裝置上、免費、無 API、無 503）抽圖片文字。

    給 _extract_image 當文件類圖片的免費首選。回 OCR 文字；非 macOS / 沒裝 ocrmac / 解圖失敗
    / 抽不到文字 → 回 None，呼叫端 fallback 到 Gemini vision。只做 OCR、無語意描述（自然照片
    的描述仍由 Gemini fallback 產生）。Cloud Run（Linux）走這條自然回 None。

    實作委派給共用咽喉點 agent_core.vision_ocr（影片關鍵幀 gating 也走那裡），
    這裡只留 RAG 自己的開關與語言設定。
    """
    if not _VISION_OCR_ENABLED:
        return None
    from agent_core.vision_ocr import ocr_image

    result = ocr_image(data, languages=list(_VISION_OCR_LANGS))
    return (result or {}).get("text") or None


def _extract_image(data: bytes, mime_type: str) -> str:
    """Extract OCR text (+ visual caption) from an image.

    優先用 macOS 內建 Vision OCR（_extract_image_vision，本地免費）：文件類圖片抽到足夠文字就
    直接用，完全不打 Gemini、也不消耗每日 OCR 預算。Vision 不可用 / 抽不到字（自然照片、手寫、
    糊）才 fallback 到 Gemini 多模態（OCR + 一兩句視覺描述，兩者都進索引），行為與原本相同。

    Tiny images (icons, thumbnails — < _IMAGE_MIN_BYTES) and oversized
    images (> _IMAGE_MAX_BYTES) are skipped at this layer to bound cost
    and avoid Gemini's request-size limits.
    """
    size = len(data)
    if size < _IMAGE_MIN_BYTES or size > _IMAGE_MAX_BYTES:
        return ""

    # 免費本地 Vision OCR 先行：文件類圖片抽到 ≥ 門檻字數就用它，不碰 Gemini 與預算閘。
    vision_text = _extract_image_vision(data, mime_type)
    if vision_text and len(vision_text) >= _VISION_OCR_MIN_CHARS:
        return f"OCR:\n{vision_text}"

    # 大王指示：圖片只走 macOS Vision、不退 Gemini（避 503/省成本）。ocrmac 抽到少量字也用、
    # 全無字（自然照片）就跳過——完全不打 Gemini Vision。取捨：無文字產品照失去視覺描述。
    if _IMAGE_VISION_ONLY:
        return f"OCR:\n{vision_text}" if vision_text else ""

    quota_msg = get_embedding_hard_quota_message() or _get_image_stop_message()
    if quota_msg:
        raise GeminiHardQuotaError(f"Gemini hard quota exhausted: {quota_msg}")

    from google.genai import types
    from agent_core.gemini_client import _gemini_generate, GEMINI_MODEL

    prompt = (
        "From this image, extract:\n"
        "1. ALL visible text (OCR), preserving original language. "
        "If there's no readable text, write '(no text)'.\n"
        "2. A 1-2 sentence visual description of what the image shows.\n\n"
        "Format response exactly as:\n"
        "OCR:\n"
        "<text or (no text)>\n\n"
        "DESC:\n"
        "<description>"
    )
    image_part = types.Part.from_bytes(data=data, mime_type=mime_type)
    try:
        resp = _gemini_generate(model=GEMINI_MODEL, contents=[image_part, prompt])
    except Exception as exc:
        if _is_hard_quota_error(exc):
            msg = _latch_image_hard_quota(exc)
            raise GeminiHardQuotaError(f"Gemini hard quota exhausted: {msg}") from exc
        raise
    return (resp.text or "").strip()


_VIDEO_SUFFIX_BY_MIME = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/mp2t": ".ts",
}


def _extract_video_via_frames(data: bytes, mime_type: str) -> str:
    """影片 → 本地抽幀 + 音軌 → Gemini Vision 逐幀語意描述 + 轉錄。

    Gemini 呼叫**必須留在本模組** — cost_tracker 以呼叫 stack 推 caller，
    media 每日預算斷路器查的是 "drive_sync.*" caller；搬去 video_frames
    會逃出預算閘。回 "" 代表本路徑不可用（ffmpeg 缺 / 抽不出幀），
    呼叫端 fallback video_understanding 整檔管線。
    """
    from agent_core.ingest import video_frames as vf

    if not vf.ffmpeg_available():
        return ""

    with tempfile.NamedTemporaryFile(
        suffix=_VIDEO_SUFFIX_BY_MIME.get(mime_type, ".mp4"),
        prefix="red_vid_",
    ) as tmp:
        tmp.write(data)
        tmp.flush()
        frames = vf.sample_frames(tmp.name, max_frames=_VIDEO_MAX_FRAMES)
        if not frames:
            return ""
        audio = vf.extract_audio_track(tmp.name, max_minutes=_VIDEO_AUDIO_MAX_MINUTES)

    from google.genai import types
    from agent_core.gemini_client import _gemini_generate, GEMINI_MODEL

    contents: list[Any] = [
        f"這是一支影片的 {len(frames)} 張均勻取樣畫面（標籤是影片時間 mm:ss）。\n"
        "你是製鞋廠的記錄員，請用繁體中文逐幀做語意描述：場景與製程階段"
        "（裁斷/針車/成型/品檢/包裝/開會/樣品展示…）、人員動作、機台與物料、"
        "鞋款特徵；畫面上的可見文字（標籤、白板、文件、螢幕）逐字抄錄。"
        "最後用 2-3 句總結整支影片在做什麼。只描述看得到的，不要腦補。"
    ]
    for ts, jpg in frames:
        contents.append(f"【畫面 @ {vf.format_ts(ts)}】")
        contents.append(types.Part.from_bytes(data=jpg, mime_type="image/jpeg"))
    try:
        resp = _gemini_generate(model=GEMINI_MODEL, contents=contents)
        visual = (resp.text or "").strip()
    except Exception as exc:
        if _is_hard_quota_error(exc):
            msg = _latch_image_hard_quota(exc)
            raise GeminiHardQuotaError(f"Gemini hard quota exhausted: {msg}") from exc
        raise

    audio_text = ""
    if audio:
        try:
            resp = _gemini_generate(
                model=GEMINI_MODEL,
                contents=[
                    types.Part.from_bytes(data=audio, mime_type="audio/mp3"),
                    "把這段音訊完整轉錄成文字，保留原語言與人名；"
                    "沒有任何語音就只回「（無語音）」。",
                ],
            )
            audio_text = (resp.text or "").strip()
        except Exception as exc:
            if _is_hard_quota_error(exc):
                msg = _latch_image_hard_quota(exc)
                raise GeminiHardQuotaError(f"Gemini hard quota exhausted: {msg}") from exc
            # 畫面描述已到手 — 轉錄失敗不整檔放棄
            print(f"[rag_sync] 影片音軌轉錄失敗（畫面描述照用）：{exc}", flush=True)

    sections: list[str] = []
    if visual:
        sections.append(f"【影片畫面描述（{len(frames)} 幀取樣）】\n{visual}")
    if audio_text and audio_text != "（無語音）":
        sections.append(f"【語音轉錄】\n{audio_text}")
    return "\n\n".join(sections)


def _extract_media(data: bytes, mime_type: str) -> str:
    """用 Gemini 多模態抽影音內容：影片走「旁白語意×畫面動作」融合抽取
    （時間戳分段，讓 RAG chunk 本身有教學連貫性），音檔走完整轉錄。

    影片預設先走本地抽幀路徑（RAG_DRIVE_ENABLE_VIDEO_FRAMES，見
    _extract_video_via_frames：ffmpeg 取樣 + 音軌，免上傳整檔）；抽幀
    不可用或抽不出內容時，與音檔一樣 fallback 到
    agent_core.video_understanding 共用管線：小檔 inline、大檔自動轉
    Gemini Files API — 舊版一律 Part.from_bytes inline 會撞 Gemini ~20 MB
    請求上限，大於 20 MB 的影片（_MEDIA_MAX_BYTES 允許到 250 MB）每晚
    都 400 失敗。caller 標籤固定為 "drive_sync._extract_media"：
    _get_media_cost_budget_message 的每日預算斷路器按這個字串對帳，
    經過共用模組轉手後 stack 推斷對不到，必須顯式傳。
    """
    size = len(data)
    if _MEDIA_MAX_BYTES > 0 and size > _MEDIA_MAX_BYTES:
        return ""

    quota_msg = get_embedding_hard_quota_message() or _get_media_stop_message()
    if quota_msg:
        raise GeminiHardQuotaError(f"Gemini hard quota exhausted: {quota_msg}")

    if mime_type.startswith("video/") and _DRIVE_ENABLE_VIDEO_FRAMES:
        text = _extract_video_via_frames(data, mime_type)
        if text:
            return text

    # Gemini Files API 不收這些容器（如 video/mp2t）：抽幀失敗（壞 TS 片段）
    # 或未開抽幀而走到這裡，上傳必 400、由檔案 bytes 決定——raise 固定簽名
    # 讓 _PERMANENT_EXTRACT_ERROR_SIGNATURES 標永久跳過，停止每晚無效重試。
    if mime_type in _GEMINI_UNSUPPORTED_MEDIA_MIMES:
        raise RuntimeError(f"media_mime_unsupported_by_gemini: {mime_type}")

    from agent_core.video_understanding import (
        drive_ingest_prompt_for,
        generate_from_media,
    )

    try:
        return generate_from_media(
            data=data,
            mime_type=mime_type,
            prompt=drive_ingest_prompt_for(mime_type),
            caller="drive_sync._extract_media",
        )
    except Exception as exc:
        if _is_hard_quota_error(exc):
            msg = _latch_image_hard_quota(exc)
            raise GeminiHardQuotaError(f"Gemini hard quota exhausted: {msg}") from exc
        raise


def _is_meeting_folder(folder_id: str) -> bool:
    return bool(folder_id) and folder_id in _MEETING_FOLDER_IDS


def _extract_meeting_transcript(service, file_id: str, mime_type: str) -> str:
    """會議錄影走本地 whisper 中文音訊轉稿（忽略影像畫面）。

    會議錄影的畫面是與會者頭像/投影片，抽幀無意義——_DRIVE_ENABLE_VIDEO_FRAMES
    那條路徑對會議只會存回一堆「圓形頭像顯示夕陽」的廢描述，語音（真正有價值
    的內容）反而沒進 RAG。這裡改用 local_asr（whisper.cpp，繁中 + OpenCC +
    幻覺過濾）轉整檔——不受抽幀路徑 _VIDEO_AUDIO_MAX_MINUTES 的 30 分鐘上限，
    長會議也完整轉——再過 asr_glossary 修專有名詞。本機零 API 成本；whisper 是
    獨立 subprocess（自帶 RED_ASR_TIMEOUT_S 天花板），主執行緒直接呼叫安全，
    不需 _ExtractPool 隔離。轉不出內容回 ""（交 caller 記 skip，不 fallback
    抽幀，免得又寫回無用的畫面描述）。
    """
    resp = _execute_drive_request(
        lambda: service.files().get_media(fileId=file_id, supportsAllDrives=True),
        f"Drive get_media {file_id}",
    )
    if not isinstance(resp, bytes):
        raise RuntimeError(f"get_media returned non-bytes for {mime_type}")
    if _MEDIA_MAX_BYTES > 0 and len(resp) > _MEDIA_MAX_BYTES:
        return ""

    from agent_core import local_asr

    if not local_asr.is_available():
        # whisper 暫時不可用（binary/模型缺、RED_ASR_DISABLE）是 transient：raise 讓
        # caller 記 retryable error、環境修好後重試，而非 return "" 落入 empty_text
        # 永久 skip-marker（之後修好也不會重轉）。
        raise RuntimeError("local_asr_unavailable")

    tmp_dir = tempfile.mkdtemp(prefix="red_meeting_")
    tmp_path = os.path.join(
        tmp_dir, "recording" + _VIDEO_SUFFIX_BY_MIME.get(mime_type, ".mp4")
    )
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(resp)
        result = local_asr.transcribe_local(tmp_path, language="zh")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if result is None:
        # 轉錄失敗（ffmpeg 逾時 / whisper 崩潰回 None）也是 transient → raise 重試。
        raise RuntimeError("local_asr_failed")
    if not (result.get("text") or "").strip():
        # 轉錄成功但確實無語音（空會議 / 純靜音）→ 回 ""，記 empty_text（合理 skip）。
        return ""
    transcript = local_asr.format_transcript(result)
    try:
        from agent_core.asr_glossary import correct_transcript

        corrected = correct_transcript(transcript)
        if corrected.get("ok") and (corrected.get("text") or "").strip():
            transcript = corrected["text"]
    except Exception:
        pass  # 術語修正是加分項，失敗不擋主轉稿
    return f"【會議錄影中文轉稿】\n{transcript}"


def _extract_binary_text(mime_type: str, data: bytes) -> str:
    if mime_type == _PDF_MIME:
        text = _extract_pdf(data)
        # 掃描型 PDF：pypdf 抽不到文字層時，gated 退路用本地 Vision OCR（預設關）。
        if not text.strip() and _PDF_OCR_ENABLED:
            text = _extract_pdf_ocr(data)
        return text
    if mime_type == _XLS_MIME:
        return _extract_xls(data)
    if mime_type in (_XLSX_MIME, _XLSM_MIME, _XLSM_MIME_CAMEL, _XLTX_MIME):
        return _extract_xlsx(data)
    if mime_type == _DOC_MIME:
        return _extract_doc(data)
    if mime_type in (_DOCX_MIME, _DOTX_MIME):
        return _extract_docx(data)
    if mime_type == _PPT_MIME:
        return _extract_ppt(data)
    if mime_type == _MSG_MIME:
        return _extract_msg(data)
    if mime_type == _EML_MIME:
        return _extract_eml(data)
    if mime_type == _MHTML_MIME:
        return _extract_mhtml(data)
    if mime_type in _RTF_MIMES:
        return _extract_rtf(data)
    if mime_type == _PPTX_MIME:
        return _extract_pptx(data)
    if mime_type == _ODT_MIME or mime_type == _ODP_MIME:
        return _extract_odf_text(data)
    if mime_type == _ODS_MIME:
        return _extract_ods(data)
    if mime_type == _XPS_MIME:
        return _extract_xps(data)
    if mime_type == _AI_MIME:
        return _extract_ai(data)
    if mime_type in _STRINGS_FALLBACK_MIMES:
        return _extract_textlike_binary(data)
    if mime_type in _IMAGE_MIMES:
        return _extract_image(data, mime_type)
    if mime_type in _CONVERTIBLE_IMAGE_MIMES:
        return _extract_convertible_image(data, mime_type)
    if mime_type in _MEDIA_MIMES:
        return _extract_media(data, mime_type)
    raise RuntimeError(f"unsupported binary mime_type: {mime_type}")


# Set, not dict-of-functions — keeping it data-only lets tests patch the
# module-level _extract_* names; a dict captures function references at
# import time and bypasses mock.patch.object.
_BINARY_EXTRACTABLE = {
    _PDF_MIME,
    _XLS_MIME,
    _XLSX_MIME,
    _XLSM_MIME,
    _XLSM_MIME_CAMEL,
    _XLTX_MIME,
    _DOC_MIME,
    _DOCX_MIME,
    _DOTX_MIME,
    _PPT_MIME,
    _PPTX_MIME,
    _MSG_MIME,
    _EML_MIME,
    _MHTML_MIME,
    _ODT_MIME,
    _ODS_MIME,
    _ODP_MIME,
    _XPS_MIME,
    _AI_MIME,
    *_STRINGS_FALLBACK_MIMES,
    *_RTF_MIMES,
}


def _binary_extractable_mimes() -> set[str]:
    mimes = set(_BINARY_EXTRACTABLE)
    if _DRIVE_ENABLE_IMAGE_INGEST:
        mimes |= set(_IMAGE_MIMES)
        mimes |= set(_CONVERTIBLE_IMAGE_MIMES)
    if _DRIVE_ENABLE_MEDIA_INGEST:
        mimes |= set(_MEDIA_MIMES)
    return mimes


def _supported_mimes_cache_key() -> tuple[Any, ...]:
    return (
        bool(_DRIVE_ENABLE_IMAGE_INGEST),
        bool(_DRIVE_ENABLE_MEDIA_INGEST),
        id(_EXPORTABLE),
        len(_EXPORTABLE),
        id(_EXPORTABLE_BINARY),
        len(_EXPORTABLE_BINARY),
        id(_DIRECT_TEXT),
        len(_DIRECT_TEXT),
        id(_BINARY_EXTRACTABLE),
        len(_BINARY_EXTRACTABLE),
        id(_IMAGE_MIMES),
        len(_IMAGE_MIMES),
        id(_CONVERTIBLE_IMAGE_MIMES),
        len(_CONVERTIBLE_IMAGE_MIMES),
        id(_MEDIA_MIMES),
        len(_MEDIA_MIMES),
    )


def _supported_mimes() -> tuple[str, ...]:
    """Single source of truth for the listing-query mime filter."""
    global _SUPPORTED_MIMES_CACHE_KEY, _SUPPORTED_MIMES_CACHE, _SUPPORTED_MIME_SET_CACHE
    with _SUPPORTED_MIMES_LOCK:
        cache_key = _supported_mimes_cache_key()
        if _SUPPORTED_MIMES_CACHE is None or _SUPPORTED_MIMES_CACHE_KEY != cache_key:
            supported = (
                tuple(_EXPORTABLE)
                + tuple(_EXPORTABLE_BINARY)
                + tuple(_DIRECT_TEXT)
                + tuple(_binary_extractable_mimes())
            )
            supported_set = frozenset(supported)
            _SUPPORTED_MIMES_CACHE_KEY = cache_key
            _SUPPORTED_MIMES_CACHE = supported
            _SUPPORTED_MIME_SET_CACHE = supported_set
        return _SUPPORTED_MIMES_CACHE


def _supported_mime_set() -> frozenset[str]:
    with _SUPPORTED_MIMES_LOCK:
        _supported_mimes()
        return _SUPPORTED_MIME_SET_CACHE or frozenset()


def _drive_listing_mimes() -> tuple[str, ...]:
    return tuple(dict.fromkeys(tuple(_supported_mimes()) + tuple(_IGNORED_RAG_MIME_TYPES)))


def _is_office_lock_file(name: str) -> bool:
    """MS Office writes a hidden owner file alongside an open document, with
    the same name prefixed by '~$'. These files have an Office MIME type but
    are NOT valid OOXML zips — pypdf/openpyxl/python-docx all error on them.
    The Drive listing returns them too; filter at the listing layer so they
    never reach the extractor.
    """
    return (name or "").startswith("~$")


def _is_drive_junk_file(name: str) -> bool:
    """Return True for Drive listing artifacts that should never be indexed.

    Besides Office lock files, macOS uploads AppleDouble sidecars such as
    `._photo.jpg` when copying folders. They often have image MIME types but
    contain only tiny resource-fork metadata; sending them to OCR wastes Gemini
    calls and can dominate daily sync cost on media-heavy drives.

    Some formats are explicitly excluded from RAG by policy even if Drive
    reports them as generic application/octet-stream.
    """
    clean = (name or "").strip()
    lower = clean.lower()
    return (
        _is_office_lock_file(clean)
        or clean.startswith("._")
        or clean in {".DS_Store", "Thumbs.db", "desktop.ini"}
        or lower.endswith(_DENIED_RAG_SUFFIXES)
    )


def _stored_modified_time_matches(stored: str, modified_time: str) -> bool:
    """Fast-skip watermark: skip only when the indexed chunks carry EXACTLY the
    Drive modifiedTime the listing reports.

    Equality — not the old `synced_at >= modifiedTime` freshness compare — is
    required for correctness: an edit landing between the download and the
    synced_at stamp bumps modifiedTime to a instant BEFORE synced_at (stamped at
    upsert time, minutes later on a slow file), so the >=-watermark judged the
    stale copy "fresh" and skipped that edit forever. Both values come from the
    Drive API, but parse-and-compare sidesteps the Z-vs-+00:00 suffix pitfall.
    Legacy chunks without a stored modified_time never match — they take one
    full pass (content-hash gate prevents a re-embed) which stamps the field.
    """
    if not stored or not modified_time:
        return False
    if stored == modified_time:
        return True
    try:
        s = datetime.fromisoformat(stored.replace("Z", "+00:00"))
        m = datetime.fromisoformat(modified_time.replace("Z", "+00:00"))
    except Exception:
        return False
    return s == m


def _stored_metadata_matches(existing: dict[str, Any], folder_id: str, drive_id: str) -> bool:
    """Verify the indexed chunks already carry the requested folder_id / drive_id
    before we let the modifiedTime gate fast-skip.

    Without this guard, a file first indexed via sync_all_drives (no drive_id)
    or sync_folder (folder_id = Shared-Drive root, not its real subfolder)
    would be silently skipped by sync_shared_drive even though its metadata
    is wrong for per-drive purge — the file would survive deletion forever
    because list_doc_ids_by_drive() can't find it. A mismatch forces a full
    re-sync so the metadata is corrected on the next embed.

    Empty caller hints mean 'don't care' — matches anything in the store.
    `existing` is the dict returned by VectorStore.get_doc_metadata().
    """
    if drive_id and existing.get("drive_id", "") != drive_id:
        return False
    if folder_id and existing.get("folder_id", "") != folder_id:
        return False
    return True


_LISTING_META_REQUIRED_KEYS = ("id", "name", "mimeType", "modifiedTime")
_GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps"


def _listing_meta_usable(meta: dict[str, Any] | None) -> bool:
    """listing 傳來的檔案 dict 欄位齊全時，_prepare_file 直接用它、免每檔再付
    一發 files().get（20k 檔的夜跑 = 20k 個多餘 RPC）。

    Google 原生檔（Docs/Sheets/Slides…）的 API 回應永遠沒有 size 欄；其他檔案
    缺 size 視為欄位不齊（舊 caller / 縮減 fields 的 listing）→ 退回 per-file
    GET，否則 too_large 下載閘會拿到 0 而靜默失效。
    """
    if not isinstance(meta, dict):
        return False
    if any(not meta.get(k) for k in _LISTING_META_REQUIRED_KEYS):
        return False
    mime = str(meta.get("mimeType", ""))
    if "size" not in meta and not mime.startswith(_GOOGLE_APPS_MIME_PREFIX):
        return False
    return True


def _prefetch_doc_metadata(store, file_ids: list[str]) -> tuple[dict[str, dict[str, Any]], bool]:
    """Best-effort bulk metadata lookup.

    Production VectorStore implements this; many tests and small fake stores
    only implement get_doc_metadata. Return (metadata, used_prefetch), so
    callers can avoid changing sync_file's call signature in those fakes while
    still skipping per-file metadata reads in production.
    """
    fn = getattr(store, "bulk_get_doc_metadata", None)
    class_fn = getattr(type(store), "bulk_get_doc_metadata", None)
    if type(store).__module__ == "unittest.mock" and not callable(class_fn):
        return {}, False
    if not callable(fn):
        return {}, False
    try:
        result = fn(file_ids)
    except Exception as exc:
        print(f"[rag_sync] metadata prefetch 失敗，改走逐檔查詢: {exc}", flush=True)
        return {}, False
    if not isinstance(result, dict):
        return {}, False
    return result, True


def _delete_removed_docs(store, removed: set[str]) -> None:
    if not removed:
        return
    # MagicMock fakes expose arbitrary attributes; check the class so tests and
    # simple fakes without the bulk method keep the old per-doc call contract.
    if callable(getattr(type(store), "bulk_delete_by_doc_ids", None)):
        store.bulk_delete_by_doc_ids(list(removed))
        return
    for doc_id in removed:
        store.delete_by_doc_id(doc_id)


def _purge_absent_docs(
    store, label: str, indexed_ids: set[str], current_ids: set[str],
    listed_count: int, listing_complete: bool = True,
) -> set[str]:
    """刪「已索引但不在當前 Drive 清單」的 doc，帶兩道防呆，回實際刪除的 set。

    (1) listing_complete=False（Drive 標 incompleteSearch）→ 完全不刪。
    (2) 即使標完整，Drive 偶爾短回清單（2026-06-28 倉庫：列 1,098/實際 1,790）。若要刪的
        佔已索引比例 > _PURGE_MAX_FRACTION 且絕對量 ≥ _PURGE_MIN_ABS → 判可疑、跳過、只記
        警告（檔在 Drive、下輪正常列檔自癒）。寧可留少量過時項，也不誤刪大量現存資料。
    """
    if not listing_complete:
        return set()
    removed = indexed_ids - current_ids
    if (
        indexed_ids
        and len(removed) >= _PURGE_MIN_ABS
        and len(removed) > _PURGE_MAX_FRACTION * len(indexed_ids)
    ):
        print(
            f"[rag_sync] {label}: ⚠️ purge 跳過 — 要刪 {len(removed)}/{len(indexed_ids)} 已索引檔"
            f"（>{int(_PURGE_MAX_FRACTION * 100)}%），疑似 Drive 短回清單（listed {listed_count}），"
            f"保留資料、待下輪自癒。",
            flush=True,
        )
        return set()
    _delete_removed_docs(store, removed)
    return removed


def _sync_file_with_optional_prefetch(
    file_id: str,
    *,
    folder_id: str = "",
    drive_id: str = "",
    modified_time: str = "",
    prefetched: dict[str, dict[str, Any]],
    used_prefetch: bool,
    listing_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "folder_id": folder_id,
        "modified_time": modified_time,
    }
    if drive_id:
        kwargs["drive_id"] = drive_id
    if used_prefetch:
        kwargs["prefetched_metadata"] = prefetched.get(file_id, {})
    # 只在欄位齊全時才傳（同 prefetched_metadata 的作法）：既有測試/呼叫端
    # 的 sync_file fake 不必都認得這個 kwarg。
    if _listing_meta_usable(listing_meta):
        kwargs["listing_meta"] = listing_meta
    return sync_file(file_id, **kwargs)


# ── Drive helpers ────────────────────────────────────────────────────

def _export_file_text(service, file_id: str, mime_type: str) -> str | None:
    export_mime = _EXPORTABLE.get(mime_type)
    if export_mime:
        resp = _execute_drive_request(
            lambda: service.files().export(fileId=file_id, mimeType=export_mime),
            f"Drive export {file_id}",
        )
        if isinstance(resp, bytes):
            return resp.decode("utf-8", errors="replace")
        return str(resp)
    if mime_type in _DIRECT_TEXT:
        resp = _execute_drive_request(
            lambda: service.files().get_media(fileId=file_id, supportsAllDrives=True),
            f"Drive get_media {file_id}",
        )
        if isinstance(resp, bytes):
            return _extract_text_by_mime(mime_type, resp)
        return str(resp)
    if mime_type in _binary_extractable_mimes():
        resp = _execute_drive_request(
            lambda: service.files().get_media(fileId=file_id, supportsAllDrives=True),
            f"Drive get_media {file_id}",
        )
        if not isinstance(resp, bytes):
            raise RuntimeError(f"get_media returned non-bytes for {mime_type}")
        return _extract_binary_text(mime_type, resp)
    return None


def _download_google_native(service, file_id: str, export_binary_mime: str) -> tuple[str, Any]:
    """Export a Google-native file into an extractable form (download only).

    Prefers the Office **binary** export (keeps images for _extract_pptx's
    embedded OCR); on any export failure — most often Drive's 10 MB export
    cap on picture-heavy decks — falls back to text/plain (text only, no
    images). Returns ("binary", bytes) for the pool binary extractor, or
    ("text", str) to use verbatim. Extraction is left to the caller so the
    image-OCR-bearing binary path can run in the SIGSEGV-isolated pool.
    """
    try:
        resp = _execute_drive_request(
            lambda: service.files().export(fileId=file_id, mimeType=export_binary_mime),
            f"Drive export {file_id}",
        )
        if not isinstance(resp, bytes):
            raise RuntimeError(f"export returned non-bytes for {export_binary_mime}")
        return ("binary", resp)
    except Exception as exc:
        _log.warning(
            "Drive export %s as %s failed (%s); falling back to text/plain",
            file_id,
            export_binary_mime,
            _summarize_exception(exc),
        )
    resp = _execute_drive_request(
        lambda: service.files().export(fileId=file_id, mimeType="text/plain"),
        f"Drive export {file_id} (text fallback)",
    )
    if isinstance(resp, bytes):
        return ("text", resp.decode("utf-8", errors="replace"))
    return ("text", str(resp))


def _export_file_text_for_sync(service, file_id: str, mime_type: str) -> str | None:
    export_binary_mime = _EXPORTABLE_BINARY.get(mime_type)
    if export_binary_mime:
        # Google 原生簡報：下載/匯出帶 wall-clock 護欄（thread timeout），但抽取
        # 走 pool——binary extract 含圖片 OCR（Gemini/grpc 非 thread-safe），在
        # worker thread 跑會有 orphan-thread SIGSEGV 風險（見下方 get_media 分支）。
        kind, payload = _run_with_timeout(
            _DRIVE_FILE_TEXT_TIMEOUT_S,
            f"Drive export {file_id}",
            lambda: _download_google_native(service, file_id, export_binary_mime),
        )
        if kind == "text":
            return payload
        return _run_binary_extract_with_timeout(
            export_binary_mime, payload, f"Drive file text {file_id}"
        )

    if mime_type not in _binary_extractable_mimes():
        return _run_with_timeout(
            _DRIVE_FILE_TEXT_TIMEOUT_S,
            f"Drive file text {file_id}",
            lambda: _export_file_text(service, file_id, mime_type),
        )

    resp = _execute_drive_request(
        lambda: service.files().get_media(fileId=file_id, supportsAllDrives=True),
        f"Drive get_media {file_id}",
    )
    if not isinstance(resp, bytes):
        raise RuntimeError(f"get_media returned non-bytes for {mime_type}")
    # Image/media extraction is I/O-bound (Gemini API call) but runs inside
    # non-thread-safe native libs (google.genai/grpc). It used to run on a
    # thread with a wall-clock timeout — but a thread can't be killed, so a
    # timed-out Gemini call left an orphan thread mutating those C objects
    # alongside the main thread → SIGSEGV (2026-06-02). Route it through the
    # persistent worker instead: a wedged call is killed with the worker, and
    # the one-time interpreter/import cost is amortised across all files.
    if mime_type in _IMAGE_MIMES:
        return _EXTRACT_POOL.run(
            "image", mime_type, resp, f"Drive image extract {file_id}",
            _DRIVE_FILE_TEXT_TIMEOUT_S,
        )
    if mime_type in _CONVERTIBLE_IMAGE_MIMES:
        # Gemini rejects bmp/tiff/x-icon/psd directly; convert→JPEG→OCR all
        # inside the pool worker so the PIL conversion AND the Gemini call stay
        # SIGSEGV-isolated, same as the image op.
        return _EXTRACT_POOL.run(
            "convertible_image", mime_type, resp,
            f"Drive convertible image extract {file_id}",
            _DRIVE_FILE_TEXT_TIMEOUT_S,
        )
    if mime_type in _MEDIA_MIMES:
        if _media_bytes_mismatch(resp, mime_type):
            # Drive 說是 media、bytes 說不是 → mime 標錯了。上傳給 Gemini 必回
            # 400 INVALID_ARGUMENT（每晚重演、檔案永遠進不了索引）。改走 strings
            # fallback 把文字撈出來 —— 那正是這種檔真正該走的路。見 _MEDIA_MAGIC。
            print(
                f"[rag_sync] {file_id}: Drive 標 {mime_type} 但 magic bytes 不符，"
                "改走文字抽取（mime 誤標）",
                flush=True,
            )
            return _extract_textlike_binary(resp)
        return _EXTRACT_POOL.run(
            "media", mime_type, resp, f"Drive media extract {file_id}",
            _DRIVE_FILE_TEXT_TIMEOUT_S,
        )
    return _run_binary_extract_with_timeout(mime_type, resp, f"Drive file text {file_id}")


def _list_folder(service, folder_id: str) -> tuple[list[dict[str, Any]], bool]:
    """List processable files directly under a folder.

    Returns (items, listing_complete) — listing_complete flips False when the
    Drive API signals incompleteSearch on any page, so sync_folder's purge can
    refuse to delete off a partial listing (same contract as _list_drive_files).
    """
    mime_filter = " or ".join(f"mimeType='{m}'" for m in _drive_listing_mimes())
    q = f"('{folder_id}' in parents) and ({mime_filter}) and trashed=false"
    items: list[dict[str, Any]] = []
    listing_complete = True
    page_token = None
    while True:
        kwargs: dict[str, Any] = dict(
            q=q,
            pageSize=1000,
            fields="nextPageToken, incompleteSearch, files(id, name, mimeType, modifiedTime, size)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        )
        if page_token:
            kwargs["pageToken"] = page_token
        result = _execute_drive_request(
            lambda: service.files().list(**kwargs),
            f"Drive list folder {folder_id}",
        )
        if result.get("incompleteSearch"):
            listing_complete = False
        items.extend(f for f in result.get("files", []) if not _is_drive_junk_file(f.get("name", "")))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return items, listing_complete


def _list_folder_with_children(service, folder_id: str) -> tuple[list[dict[str, Any]], bool]:
    """List processable files directly under a folder, plus child folders.

    Returns (items, listing_complete) — see _list_folder.
    """
    mime_filter = " or ".join(f"mimeType='{m}'" for m in _drive_listing_mimes())
    q = (
        f"('{folder_id}' in parents) and "
        f"(({mime_filter}) or mimeType='{_FOLDER_MIME}') and trashed=false"
    )
    items: list[dict[str, Any]] = []
    listing_complete = True
    page_token = None
    while True:
        kwargs: dict[str, Any] = dict(
            q=q,
            pageSize=1000,
            fields="nextPageToken, incompleteSearch, files(id, name, mimeType, modifiedTime, parents, size)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        )
        if page_token:
            kwargs["pageToken"] = page_token
        result = _execute_drive_request(
            lambda: service.files().list(**kwargs),
            f"Drive list folder {folder_id}",
        )
        if result.get("incompleteSearch"):
            listing_complete = False
        items.extend(f for f in result.get("files", []) if not _is_drive_junk_file(f.get("name", "")))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return items, listing_complete


def _list_folder_recursive(service, folder_id: str) -> tuple[list[dict[str, Any]], bool]:
    """List processable files under a plain Drive folder and all descendants.

    Returns (files, listing_complete) — False if ANY per-folder listing was
    incomplete, since a missing subtree must block the root-scoped purge.
    """
    files: list[dict[str, Any]] = []
    listing_complete = True
    seen_files: set[str] = set()
    seen_folders: set[str] = set()
    queue = [folder_id]

    while queue:
        current = queue.pop(0)
        if current in seen_folders:
            continue
        seen_folders.add(current)
        items, complete = _list_folder_with_children(service, current)
        if not complete:
            listing_complete = False
        for item in items:
            item_id = item.get("id", "")
            if item.get("mimeType") == _FOLDER_MIME:
                if item_id and item_id not in seen_folders:
                    queue.append(item_id)
                continue
            if item_id and item_id not in seen_files:
                seen_files.add(item_id)
                files.append(item)
    return files, listing_complete


# ── public API ───────────────────────────────────────────────────────

@dataclass
class _FilePlan:
    """_prepare_file 的決議：純讀/網路/CPU 算出「回什麼 + commit 要做哪些寫」，
    把所有 ChromaDB 寫與 skip-marker 寫延後到 _commit_file（主執行緒、單一
    writer）。這是 Drive 夜跑平行化的基礎切分（Phase 0：序列不變、行為等價，
    為日後把 _prepare_file 丟進 worker 鋪路）。"""
    file_id: str
    result: dict[str, Any]
    title: str = ""
    delete_doc: bool = False
    skip_marker: dict[str, Any] | None = None
    set_metadata: dict[str, Any] | None = None
    upsert: tuple[list, list, list, int] | None = None


def _prepare_file(
    file_id: str,
    folder_id: str = "",
    drive_id: str = "",
    modified_time: str = "",
    prefetched_metadata: dict[str, Any] | None = None,
    *,
    store: Any,
    service: Any = None,
    listing_meta: dict[str, Any] | None = None,
) -> _FilePlan:
    """sync_file 的讀/網路/CPU 半部：fast-skip 判斷、metadata GET、下載、抽取、
    content-hash、dedup 讀、chunk、組 metadata，回 _FilePlan。**不寫**任何
    ChromaDB / skip-marker（那些由 _commit_file 做）。暫時性抽取錯誤照舊 raise，
    交給 caller 記 retryable error（平行時由 _safe_prepare_file 包起來）。

    service：平行抓取時 caller 傳入 per-worker Drive service（googleapiclient
    transport 非 thread-safe）；None 則 fallback 共用 get_service（序列路徑、
    行為同 Phase 0）。

    listing_meta：caller 已握有的 listing 檔案 dict；欄位齊全
    （_listing_meta_usable）時直接當 metadata 用、跳過 per-file files().get。
    None / 欄位不齊 → 照舊 GET（直呼 sync_file 的路徑不變）。"""
    if prefetched_metadata is not None:
        existing = prefetched_metadata or {
            "synced_at": "", "folder_id": "", "drive_id": "",
            "content_hash": "", "title": "", "sync_complete": True,
        }
    else:
        # One ChromaDB round-trip for everything the fast-skip path needs;
        # individual getters were three separate queries each invoked per file.
        existing = store.get_doc_metadata(file_id)

    access_fields = metadata_access_fields(
        "drive",
        file_id=file_id,
        folder_id=folder_id or str(existing.get("folder_id") or ""),
        drive_id=drive_id or str(existing.get("drive_id") or ""),
    )

    # Require content_hash as a migration marker — legacy chunks indexed
    # before R2/R3 (no '[title]' prefix in chunk text, no hash in metadata)
    # would otherwise stay frozen in the old format forever once their stored
    # modified_time matches the listing. Forcing one re-embed per legacy doc
    # backfills both the title prefix and the hash; subsequent runs fast-skip
    # cheaply.
    if (
        modified_time
        and existing["content_hash"]
        and _sync_complete_allows_skip(existing)
        and _stored_modified_time_matches(
            str(existing.get("modified_time") or ""), modified_time
        )
        and _stored_metadata_matches(existing, folder_id, drive_id)
        and metadata_access_matches(existing, access_fields)
        and contextualize.ctx_ver_satisfied(existing)
    ):
        return _FilePlan(
            file_id,
            {"file_id": file_id, "skipped": True, "reason": "unchanged"},
        )

    skip_marker = _get_matching_skip_marker(
        file_id,
        modified_time=modified_time,
        folder_id=folder_id,
        drive_id=drive_id,
    )
    if skip_marker:
        return _FilePlan(file_id, {
            "file_id": file_id,
            "title": skip_marker.get("title", ""),
            "skipped": True,
            "reason": skip_marker.get("reason", "previously_skipped"),
        })

    # Preserve existing folder_id when re-syncing a file directly (no folder context).
    # Without this, a direct sync_file() call would strip folder_id from metadata,
    # causing sync_folder()'s purge diff to miss this file on its next run.
    if not folder_id:
        folder_id = existing["folder_id"]

    if service is None:
        from agent_core.google_auth import get_service
        service = get_service("drive", "v3")
    if _listing_meta_usable(listing_meta):
        # listing 的欄位已含 GET 會要的一切（id/name/mimeType/modifiedTime/
        # parents/size）→ 直接用，省掉每檔一發 files().get。
        meta = listing_meta
    else:
        meta = _execute_drive_request(
            lambda: service.files().get(
                fileId=file_id,
                fields="id, name, mimeType, modifiedTime, parents, size",
                supportsAllDrives=True,
            ),
            f"Drive metadata {file_id}",
        )

    # First-time sync via file_id with no folder context: derive folder_id from Drive parent.
    if not folder_id:
        parents = meta.get("parents", [])
        folder_id = parents[0] if parents else ""
    access_fields = metadata_access_fields(
        "drive",
        file_id=file_id,
        folder_id=folder_id,
        drive_id=drive_id,
    )

    meta_name = meta.get("name") or ""
    meta_mime = meta.get("mimeType") or ""
    if _is_drive_junk_file(meta_name):
        return _FilePlan(
            file_id,
            {"file_id": file_id, "title": meta_name, "skipped": True, "reason": "ignored filename"},
            title=meta_name,
            delete_doc=True,
            skip_marker={
                "reason": "ignored filename",
                "modified_time": meta.get("modifiedTime", "") or modified_time,
                "folder_id": folder_id,
                "drive_id": drive_id,
                "title": meta_name,
                "extra": {"mime_type": meta_mime},
            },
        )

    if _is_drive_ignored_file_type(meta_name, meta_mime):
        return _FilePlan(
            file_id,
            {"file_id": file_id, "title": meta_name, "skipped": True, "reason": "ignored file_type"},
            title=meta_name,
            delete_doc=True,
            skip_marker={
                "reason": "ignored file_type",
                "modified_time": meta.get("modifiedTime", "") or modified_time,
                "folder_id": folder_id,
                "drive_id": drive_id,
                "title": meta_name,
                "extra": {"mime_type": meta_mime},
            },
        )

    if _is_meeting_folder(folder_id) and not meta_mime.startswith("video/"):
        # 會議資料夾只要影片的中文語音轉稿；Meet 自動生成的英文 Transcript /
        # Gemini 筆記（google-apps.document）是雜訊，跳過並記 skip-marker +
        # delete_doc，免得夜跑一再重抽那些無用的英文摘要。
        return _FilePlan(
            file_id,
            {"file_id": file_id, "title": meta_name, "skipped": True,
             "reason": "meeting_folder_non_video"},
            title=meta_name,
            delete_doc=True,
            skip_marker={
                "reason": "meeting_folder_non_video",
                "modified_time": meta.get("modifiedTime", "") or modified_time,
                "folder_id": folder_id,
                "drive_id": drive_id,
                "title": meta_name,
                "extra": {"mime_type": meta_mime},
            },
        )

    quota_msg = get_embedding_hard_quota_message() or _get_image_hard_quota_message()
    if quota_msg:
        return _FilePlan(file_id, {
            "file_id": file_id,
            "title": meta_name,
            "skipped": True,
            "reason": f"embedding_unavailable: {quota_msg}",
        })

    download_size = _download_size_bytes(meta)
    if meta_mime in _IMAGE_MIMES or meta_mime in _CONVERTIBLE_IMAGE_MIMES:
        image_stop_msg = _get_image_stop_message()
        if image_stop_msg:
            return _FilePlan(file_id, {
                "file_id": file_id,
                "title": meta.get("name", ""),
                "skipped": True,
                "reason": f"image_ocr_unavailable: {image_stop_msg}",
            })
    # 會議影片走本地 ASR（零 API 成本），不受 Gemini media 每日預算斷路器限制。
    if meta_mime in _MEDIA_MIMES and not (
        _is_meeting_folder(folder_id) and meta_mime.startswith("video/")
    ):
        media_stop_msg = _get_media_stop_message()
        if media_stop_msg:
            return _FilePlan(file_id, {
                "file_id": file_id,
                "title": meta.get("name", ""),
                "skipped": True,
                "reason": f"media_transcription_unavailable: {media_stop_msg}",
            })

    max_download_bytes = _download_limit_bytes_for_mime(meta.get("mimeType", ""))
    if (
        max_download_bytes > 0
        and download_size > max_download_bytes
        and meta.get("mimeType") not in _EXPORTABLE
        and meta.get("mimeType") not in _EXPORTABLE_BINARY
    ):
        reason = f"too_large: {download_size} bytes > {max_download_bytes}"
        print(
            f"[rag_sync] Drive file {file_id} {meta.get('name', '')!r}: "
            f"skip too_large {download_size} bytes",
            flush=True,
        )
        return _FilePlan(
            file_id,
            {"file_id": file_id, "title": meta.get("name", ""), "skipped": True, "reason": reason},
            skip_marker={
                "reason": reason,
                "modified_time": meta.get("modifiedTime", "") or modified_time,
                "folder_id": folder_id,
                "drive_id": drive_id,
                "title": meta.get("name", ""),
                "extra": {
                    "size_bytes": download_size,
                    "limit_bytes": max_download_bytes,
                    "mime_type": meta.get("mimeType", ""),
                },
            },
        )

    try:
        print(
            f"[rag_sync] Drive file {file_id} {meta.get('name', '')!r}: "
            f"download/extract mime={meta.get('mimeType', '')} size={download_size or 'unknown'}",
            flush=True,
        )
        if _is_meeting_folder(folder_id) and meta_mime.startswith("video/"):
            text = _extract_meeting_transcript(service, file_id, meta_mime)
        else:
            text = _export_file_text_for_sync(service, file_id, meta["mimeType"])
    except _ExtractTooLargeError as exc:
        # Too large to extract safely — record a skip marker so we don't
        # re-attempt (and risk the SIGSEGV) every sync. Mirrors the download
        # too_large path above.
        print(
            f"[rag_sync] Drive file {file_id} {meta.get('name', '')!r}: "
            f"skip extract_too_large {exc}",
            flush=True,
        )
        return _FilePlan(
            file_id,
            {"file_id": file_id, "title": meta.get("name", ""), "skipped": True,
             "reason": f"extract_too_large: {exc}"},
            skip_marker={
                "reason": f"extract_too_large: {exc}",
                "modified_time": meta.get("modifiedTime", "") or modified_time,
                "folder_id": folder_id,
                "drive_id": drive_id,
                "title": meta.get("name", ""),
                "extra": {
                    "size_bytes": download_size,
                    "limit_bytes": _DRIVE_EXTRACT_MAX_BYTES,
                    "mime_type": meta.get("mimeType", ""),
                },
            },
        )
    except TimeoutError as exc:
        print(
            f"[rag_sync] Drive file {file_id} {meta.get('name', '')!r}: timeout {exc}",
            flush=True,
        )
        return _FilePlan(file_id, {
            "file_id": file_id,
            "title": meta.get("name", ""),
            "skipped": True,
            "reason": f"timeout: {exc}",
        })
    except GeminiHardQuotaError as exc:
        return _FilePlan(file_id, {
            "file_id": file_id,
            "title": meta.get("name", ""),
            "skipped": True,
            "reason": f"embedding_unavailable: {exc}",
        })
    except Exception as exc:
        # Permanent, file-content extraction failures (encrypted/corrupt PDFs,
        # decompression bombs, corrupt zip-based Office files) fail identically
        # on every future sync. Record a skip marker so the daemon stops
        # re-downloading + re-extracting them each run; a re-uploaded/decrypted
        # copy bumps modifiedTime and is retried. Transient failures
        # (network/API) are re-raised so the caller records a retryable `error`.
        if not _is_permanent_extract_error(exc):
            raise
        reason = f"extract_error: {_permanent_extract_error_signature(exc)}"
        print(
            f"[rag_sync] Drive file {file_id} {meta.get('name', '')!r}: skip {reason}",
            flush=True,
        )
        return _FilePlan(
            file_id,
            {"file_id": file_id, "title": meta.get("name", ""), "skipped": True, "reason": reason},
            skip_marker={
                "reason": reason,
                "modified_time": meta.get("modifiedTime", "") or modified_time,
                "folder_id": folder_id,
                "drive_id": drive_id,
                "title": meta.get("name", ""),
                "extra": {"mime_type": meta.get("mimeType", "")},
            },
        )
    if text is None:
        return _FilePlan(
            file_id,
            {"file_id": file_id, "skipped": True, "reason": "unsupported mime_type"},
            delete_doc=True,
            skip_marker={
                "reason": "unsupported mime_type",
                "modified_time": meta.get("modifiedTime", "") or modified_time,
                "folder_id": folder_id,
                "drive_id": drive_id,
                "title": meta.get("name", ""),
                "extra": {"mime_type": meta.get("mimeType", "")},
            },
        )

    # Content-hash gate. Drive's modifiedTime bumps on every metadata edit
    # (sharing tweaks, label changes, even thumbnail regen) — without this,
    # those events trigger a full re-embed of every chunk in the doc.
    # When the bytes are identical, refresh synced_at and bail out without
    # re-embedding. Requires the metadata-match guard to also pass, otherwise
    # we'd skip when drive_id/folder_id need rewriting (migration case).
    #
    # Title also has to match — _chunk_text bakes the filename into every
    # chunk's text as "[title] …", so a rename without content change still
    # demands a re-embed to refresh the prefix and metadata.title.
    now = datetime.now(timezone.utc).isoformat()
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if (
        existing["content_hash"]
        and existing["content_hash"] == content_hash
        and existing["title"] == meta.get("name", "")
        and _sync_complete_allows_skip(existing)
        and _stored_metadata_matches(existing, folder_id, drive_id)
        and contextualize.ctx_ver_satisfied(existing)
    ):
        # 連同最新 access 欄位一起回寫（metadata-only、免重 embed）：改
        # rag_access.json 後，內容未變的檔走到這裡若只 bump synced_at，
        # 上面 fast-skip 的 metadata_access_matches 每晚都不過 → 每晚重
        # 下載、而且新 ACL 永遠寫不進 chunk metadata。
        refresh_fields: dict[str, Any] = {"synced_at": now, **access_fields}
        doc_modified = meta.get("modifiedTime", "") or modified_time
        if doc_modified:
            refresh_fields["modified_time"] = doc_modified
        return _FilePlan(
            file_id,
            {"file_id": file_id, "skipped": True, "reason": "content_unchanged"},
            set_metadata=refresh_fields,
        )

    # Same-scope dedup gate. Drive copies the same file into many folders /
    # Shared Drives, each with its own file_id (== doc_id). Re-embedding a
    # byte-identical copy that lives in the SAME (drive_id, folder_id) is pure
    # waste, so skip it. A copy in a *different* folder/drive is NOT deduped:
    # search_drive_docs scopes on drive_id/folder_id, so collapsing cross-scope
    # copies would make scoped queries miss a file the user still has in Drive.
    # When skipping, record a duplicate_content marker so the next run
    # fast-skips before even downloading. The marker self-clears (see
    # _skip_marker_matches) if the canonical copy later disappears, so the
    # content can never be orphaned out of the index.
    find_dup = getattr(store, "find_duplicate_doc_id", None)
    canonical = (
        find_dup(content_hash, file_id, drive_id, folder_id)
        if callable(find_dup) else None
    )
    # Contract: find_duplicate_doc_id returns a doc_id str or None. The isinstance
    # guard also keeps MagicMock-based fakes (which auto-return truthy mocks) from
    # tripping the gate in tests that don't configure this method.
    if isinstance(canonical, str) and canonical:
        # Drop any stale chunks this file carried from a previous (non-dup)
        # version — it becomes a pure pointer to the canonical copy.
        return _FilePlan(
            file_id,
            {
                "file_id": file_id,
                "title": meta.get("name", ""),
                "skipped": True,
                "reason": "duplicate_content",
                "canonical_doc_id": canonical,
            },
            delete_doc=True,
            skip_marker={
                "reason": "duplicate_content",
                "modified_time": meta.get("modifiedTime", "") or modified_time,
                "folder_id": folder_id,
                "drive_id": drive_id,
                "title": meta.get("name", ""),
                "extra": {
                    "canonical_doc_id": canonical,
                    "content_hash": content_hash,
                    "mime_type": meta_mime,
                },
            },
        )

    ctx = contextualize.gen_doc_context(
        text, meta.get("name", ""), meta.get("mimeType", ""), "drive"
    )
    ctx_ver = contextualize.CONTEXTUAL_VER if ctx else 0
    chunks = _chunk_text(text, title=meta.get("name", ""), context=ctx)
    chunks, source_chunk_count = _cap_chunks_for_index(
        chunks,
        file_id,
        meta.get("name", ""),
    )
    if not chunks:
        empty_extra: dict[str, Any] = {"mime_type": meta_mime}
        # OCR 開著仍抽不出字（印章/相片型掃描 PDF）：蓋 ocr_attempted 戳記，
        # _skip_marker_matches 才不會把這顆 marker 再當「待 OCR 重試」放行、
        # 每晚重下載重 OCR。判斷用 _marker_is_pdf + _pdf_ocr_capable 同款謂詞，
        # 與放行端對齊——環境跑不動 OCR（沒真的試過）就不蓋，日後修好仍會重試。
        if (
            _PDF_OCR_ENABLED
            and _marker_is_pdf({"mime_type": meta_mime, "title": meta_name})
            and _pdf_ocr_capable()
        ):
            empty_extra["ocr_attempted"] = True
        return _FilePlan(
            file_id,
            {"file_id": file_id, "skipped": True, "reason": "empty_text"},
            delete_doc=True,
            skip_marker={
                "reason": "empty_text",
                "modified_time": meta.get("modifiedTime", "") or modified_time,
                "folder_id": folder_id,
                "drive_id": drive_id,
                "title": meta.get("name", ""),
                "extra": empty_extra,
            },
        )

    truncated = source_chunk_count > len(chunks)
    ids, docs, metas = [], [], []
    for i, chunk in enumerate(chunks):
        ids.append(f"{file_id}__c{i}")
        docs.append(chunk)
        chunk_meta: dict[str, Any] = {
            "doc_id": file_id,
            "title": meta["name"],
            "mime_type": meta["mimeType"],
            "chunk_index": i,
            "synced_at": now,
            "content_hash": content_hash,
            "sync_complete": False,
            "ctx_ver": ctx_ver,
            **access_fields,
        }
        # 文件本身的日期（非同步日）— 檢索端靠它做「同權威取最新」與
        # 「資料截止」判斷；缺了它模型只看得到 synced_at 會誤判新舊。
        doc_modified = meta.get("modifiedTime", "") or modified_time
        if doc_modified:
            chunk_meta["modified_time"] = doc_modified
        if folder_id:
            chunk_meta["folder_id"] = folder_id
        if drive_id:
            chunk_meta["drive_id"] = drive_id
        if truncated:
            chunk_meta["truncated"] = True
            chunk_meta["source_chunk_count"] = source_chunk_count
        metas.append(chunk_meta)
    result = {"file_id": file_id, "title": meta["name"], "chunks": len(chunks)}
    if truncated:
        result["truncated"] = True
        result["source_chunks"] = source_chunk_count
    return _FilePlan(
        file_id,
        result,
        title=meta["name"],
        upsert=(ids, docs, metas, len(chunks)),
    )


def _commit_file(store: Any, plan: _FilePlan) -> dict[str, Any]:
    """sync_file 的寫半部：主執行緒、ChromaDB 單一 writer。依 _FilePlan 執行所有
    store 寫與 skip-marker 寫，回 summary dict。順序對齊原 sync_file（delete →
    set_metadata → skip-marker；index 路徑 upsert → delete_stale → mark_complete
    → clear_marker，中途 hard-quota 則回 skip）。"""
    if plan.delete_doc:
        store.delete_by_doc_id(plan.file_id)
    if plan.set_metadata:
        store.set_doc_metadata_fields(plan.file_id, plan.set_metadata)
    if plan.skip_marker is not None:
        _record_skip_marker(plan.file_id, **plan.skip_marker)
    if plan.upsert is not None:
        ids, docs, metas, chunk_count = plan.upsert
        try:
            store.upsert_batch(ids, docs, metas)
        except GeminiHardQuotaError as exc:
            return {
                "file_id": plan.file_id,
                "title": plan.title,
                "skipped": True,
                "reason": f"embedding_unavailable: {exc}",
            }
        store.delete_stale_chunks(plan.file_id, chunk_count)
        mark_complete = getattr(store, "mark_doc_sync_complete", None)
        if callable(mark_complete):
            mark_complete(plan.file_id)
        _clear_skip_marker(plan.file_id)
    return plan.result


def sync_file(
    file_id: str,
    folder_id: str = "",
    drive_id: str = "",
    modified_time: str = "",
    prefetched_metadata: dict[str, Any] | None = None,
    listing_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ingest or re-sync a single Drive file. Returns summary dict.

    If `modified_time` is supplied (callers from sync_folder / sync_shared_drive
    / sync_all_drives have it from the listing) and the indexed chunks carry
    exactly that Drive modifiedTime, return immediately without calling Drive
    or Gemini — this is what keeps daily syncs cheap on a 20k-file index.

    If `prefetched_metadata` is supplied (callers can bulk-load via
    VectorStore.bulk_get_doc_metadata to skip the per-file get), use it as
    `existing` instead of issuing a per-call ChromaDB query. Empty-dict means
    "we checked and the doc isn't indexed"; None means "no prefetch happened,
    fall through to the single-doc query".

    If `listing_meta` is supplied and field-complete (id/name/mimeType/
    modifiedTime + size for binary files), the per-file Drive files().get is
    skipped too. Direct calls without it keep the current GET behaviour.

    序列流程 = _prepare_file（讀/網路/CPU）→ _commit_file（單一 writer 寫）。
    這個切分是 Drive 夜跑平行化的基礎；目前仍序列呼叫、行為與舊版等價。
    """
    store = get_store(_COLLECTION)
    plan = _prepare_file(
        file_id,
        folder_id=folder_id,
        drive_id=drive_id,
        modified_time=modified_time,
        prefetched_metadata=prefetched_metadata,
        store=store,
        listing_meta=listing_meta,
    )
    return _commit_file(store, plan)


def _safe_prepare_file(
    f: dict[str, Any],
    *,
    folder_id: str,
    drive_id: str,
    prefetched: dict[str, dict[str, Any]],
    used_prefetch: bool,
    service: Any,
    store: Any,
) -> tuple:
    """worker-safe 包 _prepare_file：算 prefetched_metadata + 跑 prepare，吞所有例外
    （含暫時性抽取錯誤）成 (f, None, exc)，**不 raise**，讓平行 worker 不炸整批。
    成功回 (f, plan, None)。所有 store 寫仍由 _commit_prepared 在主執行緒做。"""
    try:
        prefetched_metadata = prefetched.get(f["id"], {}) if used_prefetch else None
        plan = _prepare_file(
            f["id"],
            folder_id=folder_id,
            drive_id=drive_id,
            modified_time=f.get("modifiedTime", ""),
            prefetched_metadata=prefetched_metadata,
            store=store,
            service=service,
            listing_meta=f,
        )
        return (f, plan, None)
    except Exception as exc:  # noqa: BLE001  worker 不可炸整批
        return (f, None, exc)


def _commit_prepared(store: Any, prepared: tuple) -> dict[str, Any]:
    """主執行緒：把 _safe_prepare_file 的結果落地。prepare 失敗 → error result；
    否則 _commit_file（ChromaDB 單一 writer）；commit 失敗也收成 error result。
    語義對齊 Phase 0 orchestrator 迴圈的 try/except（涵蓋 prepare+commit）。"""
    f, plan, err = prepared
    if err is not None:
        return {"file_id": f["id"], "title": f.get("name", ""), "skipped": True,
                "reason": f"error: {err}"}
    try:
        return _commit_file(store, plan)
    except Exception as exc:  # noqa: BLE001
        return {"file_id": f["id"], "title": f.get("name", ""), "skipped": True,
                "reason": f"error: {exc}"}


# rag 心跳寫入節流：_process_file_list 每處理一檔呼叫 _pulse_rag_heartbeat，但
# 實際寫檔最多每 N 秒一次，避免逐檔 IO 風暴（心跳只需比 watchdog 門檻密即可）。
_RAG_HB_MIN_INTERVAL_S = _env_int("RAG_HEARTBEAT_MIN_INTERVAL_S", 10, min_value=1)
_last_rag_hb_mono = 0.0


def _pulse_rag_heartbeat() -> None:
    """rag_sync 逐檔推進的心跳，供 daemon_watchdog 區分「慢但健康」與真 wedge。

    每處理一檔呼叫；節流到最多每 _RAG_HB_MIN_INTERVAL_S 秒才真寫一次。best-effort，
    寫失敗絕不中斷 sync。只在主執行緒的 _process_file_list 迴圈呼叫（_ExtractPool
    子程序不碰這條路徑），故 module-level 節流狀態單執行緒、安全。"""
    global _last_rag_hb_mono
    mono = time.monotonic()
    if mono - _last_rag_hb_mono < _RAG_HB_MIN_INTERVAL_S:
        return
    _last_rag_hb_mono = mono
    try:
        from agent_core.daemon_watchdog import write_rag_heartbeat
        write_rag_heartbeat()
    except Exception:
        pass


def _process_file_list(
    files: list[dict[str, Any]],
    *,
    folder_id_fn,
    drive_id: str = "",
    prefetched: dict[str, dict[str, Any]],
    used_prefetch: bool,
    progress_label: str = "",
    progress_every: int = 50,
    service_builder=None,
    fetch_workers: int | None = None,
) -> list[dict[str, Any]]:
    """跑檔案清單過 _prepare_file → _commit_file，序列或（gated）平行。

    平行（RED_DRIVE_FETCH_WORKERS>1 或顯式 fetch_workers>1）：worker 執行緒各持
    自己的 Drive service（service_builder，transport 非 thread-safe）跑 prepare；
    **所有 ChromaDB 寫（_commit_prepared）只在主執行緒**，維持單一 writer（多 writer
    = HNSW 腐壞/SIGSEGV）。ex.map 保序消費讓 fetch/抽取與 commit 重疊。
    預設 fetch_workers=1 → 純序列、行為與 Phase 0 完全相同。
    folder_id_fn(f) 算每檔 folder_id（sync_folder 用固定值，其餘從 parents）。
    整輪包在單一 _defer_skip_state_writes()（skip-state defer depth 須維持單一）。"""
    store = get_store(_COLLECTION)
    total = len(files)
    if fetch_workers is None:
        fetch_workers = _env_int("RED_DRIVE_FETCH_WORKERS", 1)
    if fetch_workers > 1 and service_builder is None:
        from agent_core.google_auth import build_drive_service_uncached
        service_builder = build_drive_service_uncached

    def _emit(idx: int) -> None:
        # 每檔跳一次心跳（內部節流）— 讓 watchdog 看得到「還在逐檔推進」，
        # 即使 progress log 每 50 檔才印一行。
        _pulse_rag_heartbeat()
        if progress_label and (idx == 1 or idx % progress_every == 0):
            print(f"[rag_sync] {progress_label}: processing {idx}/{total}", flush=True)

    results: list[dict[str, Any]] = []
    with _defer_skip_state_writes():
        if service_builder is not None and fetch_workers > 1:
            import concurrent.futures
            import threading as _threading
            _tls = _threading.local()

            def _prep(item):
                idx, f = item
                svc = getattr(_tls, "svc", None)
                if svc is None:
                    svc = service_builder()
                    _tls.svc = svc
                return idx, _safe_prepare_file(
                    f, folder_id=folder_id_fn(f), drive_id=drive_id,
                    prefetched=prefetched, used_prefetch=used_prefetch,
                    service=svc, store=store)

            with concurrent.futures.ThreadPoolExecutor(max_workers=fetch_workers) as ex:
                for idx, prepared in ex.map(_prep, enumerate(files, 1)):
                    _emit(idx)
                    results.append(_commit_prepared(store, prepared))
        else:
            # 序列退路：走原 _sync_file_with_optional_prefetch（= sync_file），與
            # Phase 0 完全相同的呼叫路徑（used_prefetch 時才加 prefetched_metadata
            # kwarg、以及既有測試對 drive_sync.sync_file 的 mock 點）。
            for idx, f in enumerate(files, 1):
                _emit(idx)
                try:
                    r = _sync_file_with_optional_prefetch(
                        f["id"], folder_id=folder_id_fn(f), drive_id=drive_id,
                        modified_time=f.get("modifiedTime", ""),
                        prefetched=prefetched, used_prefetch=used_prefetch,
                        listing_meta=f)
                except Exception as exc:  # noqa: BLE001
                    r = {"file_id": f["id"], "title": f.get("name", ""),
                         "skipped": True, "reason": f"error: {exc}"}
                results.append(r)
    return results


def sync_folder(folder_id: str, *, recursive: bool = False) -> dict[str, Any]:
    """Ingest supported files in a Drive folder. Returns summary.

    recursive=True includes supported files in child folders. Descendant files
    are stamped with the root folder_id so root-scoped purge/search includes
    the full tree.
    """
    from agent_core.google_auth import get_service
    service = get_service("drive", "v3")
    files, listing_complete = (
        _list_folder_recursive(service, folder_id) if recursive
        else _list_folder(service, folder_id)
    )
    mode = " recursively" if recursive else ""
    print(
        f"[rag_sync] Drive folder {folder_id}: listed {len(files)} files{mode} "
        f"complete={listing_complete}",
        flush=True,
    )
    current_ids = {f["id"] for f in files}

    store = get_store(_COLLECTION)
    removed = _purge_absent_docs(
        store, f"Drive folder {folder_id}",
        store.list_doc_ids_by_folder(folder_id), current_ids, len(files),
        listing_complete,
    )

    # Bulk-prefetch metadata for every file in one paginated sweep — replaces
    # ~N per-file get_doc_metadata round-trips with O(N/_PAGE_LIMIT) calls.
    prefetched, used_prefetch = _prefetch_doc_metadata(store, [f["id"] for f in files])

    results = _process_file_list(
        files,
        folder_id_fn=lambda f, fid=folder_id: fid,
        prefetched=prefetched,
        used_prefetch=used_prefetch,
        progress_label=f"Drive folder {folder_id}",
        progress_every=50,
    )
    synced = [r for r in results if not r.get("skipped")]
    skipped = [r for r in results if r.get("skipped")]
    return {
        "total": len(files),
        "synced": len(synced),
        "skipped": len(skipped),
        "purged": len(removed),
        "listing_complete": listing_complete,
        "details": results,
    }


def is_shared_drive_id(s: str) -> bool:
    """Heuristic: Shared Drive root IDs start with '0A' and are ~19 chars.
    Regular folder/file IDs start with '1' and are 33+ chars. The '0A' prefix
    is undocumented but stable across all observable Shared Drives."""
    s = s.strip()
    return s.startswith("0A") and len(s) <= 25


def _list_drive_files(service, drive_id: str) -> tuple[list[dict[str, Any]], bool]:
    """List ALL processable files within a single Shared Drive.

    Uses corpora='drive' + driveId to enumerate the entire Shared Drive without
    a parents constraint, so files in any subfolder are included.

    Returns (items, listing_complete) — listing_complete is False if the Drive
    API signals incompleteSearch on any page; callers must skip purge when False.
    """
    mime_filter = " or ".join(f"mimeType='{m}'" for m in _drive_listing_mimes())
    q = f"({mime_filter}) and trashed=false"
    items: list[dict[str, Any]] = []
    listing_complete = True
    page_token = None
    while True:
        kwargs: dict[str, Any] = dict(
            q=q,
            pageSize=1000,
            fields="nextPageToken, incompleteSearch, files(id, name, mimeType, modifiedTime, parents, size)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            corpora="drive",
            driveId=drive_id,
        )
        if page_token:
            kwargs["pageToken"] = page_token
        result = _execute_drive_request(
            lambda: service.files().list(**kwargs),
            f"Drive list shared drive {drive_id}",
        )
        if result.get("incompleteSearch"):
            listing_complete = False
        items.extend(f for f in result.get("files", []) if not _is_drive_junk_file(f.get("name", "")))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return items, listing_complete


def sync_shared_drive(drive_id: str) -> dict[str, Any]:
    """Ingest or mark every processable file in a Shared Drive.

    Each chunk is stamped with both folder_id (immediate parent) and drive_id
    (Shared Drive root), so per-drive purge can find them later.

    Purge: indexed files absent from the Drive listing are deleted from ChromaDB,
    BUT only when listing_complete is True — a partial result must never trigger
    deletes.
    """
    from agent_core.google_auth import get_service
    service = get_service("drive", "v3")
    files, listing_complete = _list_drive_files(service, drive_id)
    print(
        f"[rag_sync] SharedDrive {drive_id}: listed {len(files)} files "
        f"complete={listing_complete}",
        flush=True,
    )
    current_ids = {f["id"] for f in files}

    store = get_store(_COLLECTION)
    removed: set[str] = set()
    if listing_complete:
        removed = _purge_absent_docs(
            store, f"SharedDrive {drive_id}",
            store.list_doc_ids_by_drive(drive_id), current_ids, len(files),
        )

    prefetched, used_prefetch = _prefetch_doc_metadata(store, [f["id"] for f in files])

    results = _process_file_list(
        files,
        folder_id_fn=lambda f: f["parents"][0] if f.get("parents") else "",
        drive_id=drive_id,
        prefetched=prefetched,
        used_prefetch=used_prefetch,
        progress_label=f"SharedDrive {drive_id}",
        progress_every=50,
    )
    synced  = [r for r in results if not r.get("skipped")]
    skipped = [r for r in results if r.get("skipped")]
    return {
        "drive_id":         drive_id,
        "total":            len(files),
        "synced":           len(synced),
        "skipped":          len(skipped),
        "purged":           len(removed),
        "listing_complete": listing_complete,
        "details":          results,
    }


def _list_all_files(service) -> tuple[list[dict[str, Any]], bool]:
    """List ALL processable files across all accessible drives.

    Returns (items, listing_complete) where listing_complete is False if the
    Drive API signalled incompleteSearch on any page — meaning some files may
    be missing from the result set and the caller must NOT purge indexed entries
    that are absent from the returned list.
    """
    mime_filter = " or ".join(f"mimeType='{m}'" for m in _drive_listing_mimes())
    q = f"({mime_filter}) and trashed=false"
    items: list[dict[str, Any]] = []
    listing_complete = True
    page_token = None
    while True:
        kwargs: dict[str, Any] = dict(
            q=q,
            pageSize=1000,
            # Request incompleteSearch so we can detect partial results.
            fields="nextPageToken, incompleteSearch, files(id, name, mimeType, modifiedTime, parents, size)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            corpora="allDrives",
        )
        if page_token:
            kwargs["pageToken"] = page_token
        result = _execute_drive_request(
            lambda: service.files().list(**kwargs),
            "Drive list all drives",
        )
        if result.get("incompleteSearch"):
            listing_complete = False
        items.extend(f for f in result.get("files", []) if not _is_drive_junk_file(f.get("name", "")))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return items, listing_complete


def sync_all_drives() -> dict[str, Any]:
    """Ingest ALL supported files accessible from this account across all drives.

    Compared with sync_folder(), this function does not restrict to a specific
    folder — it fetches every supported file visible to the service account.

    Purge: indexed files absent from the Drive listing are deleted from ChromaDB
    (trashed / removed), BUT only when the listing is complete.  When the Drive
    API returns incompleteSearch=true on any page the purge step is skipped
    entirely — a partial listing must never delete valid indexed data.

    Returns the same summary dict as sync_folder() plus listing_complete: bool.
    """
    from agent_core.google_auth import get_service
    service = get_service("drive", "v3")
    files, listing_complete = _list_all_files(service)
    print(
        f"[rag_sync] allDrives: listed {len(files)} files complete={listing_complete}",
        flush=True,
    )
    current_ids = {f["id"] for f in files}

    store = get_store(_COLLECTION)
    # Only purge when the listing is provably complete; a partial result set
    # must not be used to conclude a file has been deleted from Drive. The
    # ratio gate inside _purge_absent_docs additionally refuses mass deletes
    # when Drive short-returns a "complete" listing (2026-06-28 倉庫事故的同型
    # 入口 — 之前這裡是裸刪，繞過了那道防呆).
    removed: set[str] = set()
    if listing_complete:
        removed = _purge_absent_docs(
            store, "allDrives", store.list_doc_ids(), current_ids,
            len(files), listing_complete,
        )

    prefetched, used_prefetch = _prefetch_doc_metadata(store, [f["id"] for f in files])

    # folder_id 從 Drive parents 推導，per-folder purge 才找得到。
    results = _process_file_list(
        files,
        folder_id_fn=lambda f: f["parents"][0] if f.get("parents") else "",
        prefetched=prefetched,
        used_prefetch=used_prefetch,
        progress_label="allDrives",
        progress_every=100,
    )

    synced  = [r for r in results if not r.get("skipped")]
    skipped = [r for r in results if r.get("skipped")]
    return {
        "total":            len(files),
        "synced":           len(synced),
        "skipped":          len(skipped),
        "purged":           len(removed),
        "listing_complete": listing_complete,
    }


def sync_status() -> dict[str, Any]:
    store = get_store(_COLLECTION)
    return {"collection": _COLLECTION, "total_chunks": store.count()}
