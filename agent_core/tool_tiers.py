"""Tool permission tiers — 4 級工具分級 + 頻道×等級確認矩陣。

之前所有工具只有「sensitive」二元分類（V4 確認門 0/1）。107 個 sensitive
裡面實際上有：
  - send_gmail（一次寄信）
  - delete_erp_workflow（刪掉整個 workflow，影響大）
  - set_vault_secret（改 API key，攻擊者拿到=毀滅性）

全部用同樣 90s `+確認` token 不合理 — 寄錯信跟改錯 vault 不該同等門檻。

新分級：

  Safe       查詢 / 摘要 / 讀取 — 永遠可呼叫，無門
  Confirm    寄信 / 上傳 / 建行事曆 — 90s `+確認` token（M5 one-shot）
  Dangerous  刪除 / 批次改 / 任意 code-exec / 子代理委派 — token + 警示前綴 + 強制 audit
  Locked     金鑰 / vault / 系統 toggle / persona 修改 / 永久狀態寫入
              — Telegram 頻道一律拒絕，請大王到 Mac 直接 REPL

頻道差異：

  Telegram   single-factor，所有等級都需 token；Locked 直接拒
  voice      Whisper 可能誤聽，Dangerous 也拒（請改 Telegram）
  daemon     在 add_scheduled_task 時已過大王 +確認，後續執行視為已授權
              （Locked 仍拒 — daemon 不該動 vault / persona）
  REPL       大王 親手打鍵盤，無 gate（信任 physical access）

每加新 sensitive tool，這裡要分級。pattern + override 設計讓多數情況靠
heuristic 自動歸類，不用一個個列。
"""
from __future__ import annotations

import functools
import re
from typing import Any


# ────────────────────────────────────────────────────────────────────
# Tier constants（string，方便序列化 / log）
# ────────────────────────────────────────────────────────────────────
TIER_SAFE = "safe"
TIER_CONFIRM = "confirm"
TIER_DANGEROUS = "dangerous"
TIER_LOCKED = "locked"

_TIER_ORDER = {
    TIER_SAFE: 0,
    TIER_CONFIRM: 1,
    TIER_DANGEROUS: 2,
    TIER_LOCKED: 3,
}

_TIER_ICON = {
    TIER_SAFE: "🟢",
    TIER_CONFIRM: "🟡",
    TIER_DANGEROUS: "🔴",
    TIER_LOCKED: "🔒",
}

_TIER_DESC = {
    TIER_SAFE: "查詢 / 讀取 — 無確認需要",
    TIER_CONFIRM: "一次性外送 — 90s `+確認` token，one-shot revoke",
    TIER_DANGEROUS: "刪除 / 批次改 / 任意 code-exec — token + 警示 + audit",
    TIER_LOCKED: "金鑰 / vault / 系統 toggle / persona — Telegram 拒絕；REPL only",
}


# ────────────────────────────────────────────────────────────────────
# Channel constants
# ────────────────────────────────────────────────────────────────────
CHANNEL_TELEGRAM = "telegram"
CHANNEL_VOICE = "voice"
CHANNEL_DAEMON = "daemon"
CHANNEL_REPL = "repl"


# ────────────────────────────────────────────────────────────────────
# Explicit overrides — 最權威，pattern 抓不到的 / pattern 誤判的都列這裡
# ────────────────────────────────────────────────────────────────────
_TIER_OVERRIDES: dict[str, str] = {
    # ── LOCKED — security state / agent persona / persistent injection ──
    # Vault — keychain operations
    "set_vault_secret": TIER_LOCKED,
    "delete_vault_secret": TIER_LOCKED,
    "prune_vault_log": TIER_LOCKED,
    # Safety toggle 自身（攻擊者過 +確認 後若可關 dry-run，破解整個系統）
    "enable_dry_run_mode": TIER_LOCKED,
    "disable_dry_run_mode": TIER_LOCKED,
    # Code loading — reload_skills 載入新檔，learn_skill_from_video 寫 .draft
    "reload_skills": TIER_LOCKED,
    "learn_skill_from_video": TIER_LOCKED,
    # Persona / behavior 永久注入 — round 7 C7-1 教訓
    "learn_behavior": TIER_LOCKED,
    "forget_behavior": TIER_LOCKED,
    # 記憶治理：手動 archive/revive behavior_policy 規則 — 跟 learn/forget 同級
    "resolve_conflict": TIER_LOCKED,
    # Memory writes — round 8 M8-4: prompt-injection 永久存活
    "save_memory": TIER_LOCKED,
    "remember": TIER_LOCKED,
    "forget_memory": TIER_LOCKED,
    # 主動確認式學習 — remember（LOCKED）的窄化版：強制 owner_only、長度
    # 上限、sanitize、audit、budget 限流。大王口頭確認後的推論事實才寫入。
    "confirm_inferred_fact": TIER_CONFIRM,
    # 知識卡人工核可 — uncertain 卡進搜尋卡庫的唯一通道（confirm_inferred_fact
    # 的影片知識版）：單卡放行/退回、內容早在抽取時 sanitize、ledger 留痕。
    # CONFIRM 讓大王在 Telegram 審完待核卡當場處理。
    "resolve_skill_card": TIER_CONFIRM,

    # 反思撤銷 — 刪除機器歸納的洞察（原始文件不受影響、之後可重新歸納），
    # 破壞性遠低於 forget_behavior/forget_memory（LOCKED），CONFIRM 即可。
    # 名字刻意不用 delete_ 前綴（那會撞 ^delete_ pattern 變 DANGEROUS）。
    "revoke_reflection": TIER_CONFIRM,

    # 糾正固化 — learn_behavior 的刻意窄化版。CONFIRM 跟 send_gmail 同級，
    # 讓大王在 Telegram 糾正完能當場固化規則；完整版 learn_behavior 維持
    # LOCKED 只准 REPL。誠實的風險邊界（+確認 token 不綁工具/args，gate
    # 訊息只給工具名，args 由 LLM 轉述、被注入的 LLM 不可信）：真正的防線
    # 是「這條通道能造成的最壞結果」被鎖死 —— 強制 owner_only（只污染大王
    # 自己的 persona，部門 bot 不受影響）、只能 supersede 同為 owner_only
    # 的規則（不能停用 all/department 級跨部門規則，見 _write_behavior_rule
    # 的 restrict_supersede_to）、長度上限、sanitize、audit、tool_budget
    # 限流、治理報告可見 + REPL 可撤銷。SECURITY.md §2.11 有完整分析。
    "remember_correction_rule": TIER_CONFIRM,
    # 嘜頭 OCR 糾正回饋 — 寫 lexicon（影響後續辨識輸出）+ 存訓練樣本；
    # 與 remember_correction_rule 同級：CONFIRM 走 +確認、owner-only
    "record_box_ocr_correction": TIER_CONFIRM,
    # ASR injection — round 8 M8-1
    "correct_mistake": TIER_LOCKED,
    "delete_correction": TIER_LOCKED,
    # Voiceprint = auth state
    # ERP workflows = high-impact business automation
    "learn_erp_from_video": TIER_LOCKED,
    "learn_erp_from_drive_folder": TIER_LOCKED,
    "delete_erp_workflow": TIER_LOCKED,
    "merge_erp_workflows": TIER_LOCKED,
    # 任意唯讀 SQL 直打生產 ERP：要 +確認（也在 tg_auth._SENSITIVE_TOOLS）。
    "run_erp_readonly_sql": TIER_CONFIRM,
    # QC master = long-lived 比對基準，改錯影響後續所有 inspect
    "set_qc_master": TIER_LOCKED,
    # Budget reset — 若 LLM 被 inject 可呼叫此把 budget 歸零繞過上限
    "reset_budget": TIER_LOCKED,

    # ── Queue tools ──
    # submit_task：DANGEROUS — queue 在 daemon channel 跑，等於繞過 +確認
    # （wrap_sensitive_tool channel='daemon' 對 CONFIRM 是 "allow"）
    # 所以 submit 階段必須卡到最嚴格 — DANGEROUS 雙確認
    "submit_task": TIER_DANGEROUS,
    # requeue_dead_letter：重跑失敗任務 — 可能重複副作用，視同 DANGEROUS
    "requeue_dead_letter": TIER_DANGEROUS,

    # ── DANGEROUS — destructive / mass / code-exec / sub-agent ──
    "run_shell": TIER_DANGEROUS,
    "run_python_code": TIER_DANGEROUS,
    "delegate_to_sub_agent": TIER_DANGEROUS,
    "delegate_to_sub_agents_parallel": TIER_DANGEROUS,
    "manage_files": TIER_DANGEROUS,        # mkdir/delete/move/copy 都從這出
    "write_file": TIER_DANGEROUS,          # 任意路徑寫
    "email_lake_rebuild": TIER_DANGEROUS,
    "prune_old_runs": TIER_DANGEROUS,      # 攻擊者刪 audit 痕跡
    "batch_extract_quotes_from_parquet": TIER_DANGEROUS,
    "mcp_filesystem_write_file": TIER_DANGEROUS,
    "mcp_filesystem_edit_file": TIER_DANGEROUS,
    "mcp_filesystem_move_file": TIER_DANGEROUS,
    # delete_recorded_demo — 刪 demo 影片，沒那麼破壞但有 deletion 性質
    "delete_recorded_demo": TIER_DANGEROUS,
    "delete_tracked_sample": TIER_DANGEROUS,
    "close_sample": TIER_DANGEROUS,
    # sample status / calendar update — 修改既有資料
    "update_sample_status": TIER_DANGEROUS,
    # cancel_task：取消 queued/running task — 攻擊者可能用來取消正在跑的
    # security 任務（e.g. log rotation / health check）；不可逆、與
    # submit_task 對稱，需要 +雙確認 門檻。
    "cancel_task": TIER_DANGEROUS,

    # ── Task memory（承諾記憶）/ 行事曆記事 — 大王 2026-06-16 指示免確認 ──
    # 「記下來 / 提醒我 / 設行事曆」是高頻、低破壞、且都寫進大王自己帳號的記事
    # 動作；每次都要 +確認 反而擋住小紅的核心用途。新增與更新類降 SAFE 免確認。
    # 仍守的：刪除類（delete_*，下面 DANGEROUS）、寄信 / run_shell / 金鑰等。
    # 註：這些工具仍留在 tg_auth._SENSITIVE_TOOLS（子代理 deny-by-default 不變），
    #     只是 tier 降 SAFE → Telegram 走 "allow" 不再要 token。
    "add_task": TIER_SAFE,
    "update_task_status": TIER_SAFE,
    "complete_task": TIER_SAFE,
    "link_to_email": TIER_SAFE,
    "link_to_calendar": TIER_SAFE,
    "set_task_reminder": TIER_SAFE,
    # delete_task：DANGEROUS — 刪 commitment 是高風險記憶遺失，需雙確認（不放行）
    "delete_task": TIER_DANGEROUS,
    # recurring reminder 寫入 — 同 add_task 級
    "set_recurring_reminder": TIER_SAFE,
    "clear_recurring_reminder": TIER_SAFE,
    # 產品照搜尋 — 唯讀（以款號/鞋型關鍵字 或 參考圖 找關聯舊款），無破壞性
    "search_product_photos": TIER_SAFE,
    # 鞋圖抓取傳 Telegram — 唯讀下載 Drive 圖檔到本地暫存區＋回傳 [[TG_PHOTO]]
    # 標記（實際傳送在 daemon 回覆路徑、只回當前對話），無破壞性
    "fetch_shoe_photos": TIER_SAFE,
    # 樣單生成概念圖 — 會花 Gemini 生圖費，與 generate_image/edit_image 一致用 CONFIRM 擋意外花費
    "generate_product_concept": TIER_CONFIRM,
    # 樣品照一鍵改款生成（內部調 generate_product_concept 生圖、同樣花 Gemini 費）
    "generate_from_sample": TIER_CONFIRM,
    # 本機 LoRA 生圖（免費但慢 ~1-2 分鐘、吃記憶體可能 OOM chroma，CONFIRM 防誤觸發）
    "generate_jf_shoe": TIER_CONFIRM,
    # 樣品單解析（只讀 Excel/PDF + LLM 提取款號規格、不生圖，SAFE 免確認）
    "parse_sample_order": TIER_SAFE,
    # 上傳文件挖圖（規格單 PDF / 客人 Excel 母表 → 產品圖 → 嵌進 tracking log
    # Excel）。會寫檔，所以顯式列出而不是靠 default SAFE：讀入限縮在 Telegram
    # 上傳目錄、寫出限縮在 var/data/doc_images/（per-color 分艙），不花錢、
    # 不對外送，SAFE。員工白名單只收 SAFE，這兩顆進 _COMMON_TOOLS 就靠這條。
    "extract_uploaded_pdf_images": TIER_SAFE,
    "extract_uploaded_excel_images": TIER_SAFE,
    # 上傳文件的完整讀取（整張 Excel/CSV、PDF 逐頁原文）。純讀不寫、不呼叫
    # LLM、不對外送；讀入跟上面兩顆共用同一道上傳目錄閘（同一批檔案，差別
    # 只在拿回全文而不是圖）。SAFE 是為了進 _COMMON_TOOLS —— 員工白名單只
    # 收 SAFE，這兩顆進不去就等於 2026-08-17 UserAng 案的原狀：員工通道沒有
    # 任何「把這張表讀完」的工具，只能拿抽圖結果與 LLM 摘要拼，漏列漏欄。
    "read_uploaded_table": TIER_SAFE,
    "read_uploaded_pdf_text": TIER_SAFE,
    # 樣品單線稿驅動生圖（抽手繪線稿 + nano-banana 渲染、付費）—— SAFE 是刻意的：
    # 2026-08-03 UserC 案，開發部員工丟樣品單 xlsx 問「自動生成鞋圖」，小紅答
    # 「系統尚無此功能」。真因之一是這顆 CONFIRM：員工 session 只收 SAFE
    #（+確認 token 不綁身分，放 CONFIRM 進去等於讓員工自己確認自己），連
    # filter_tools_for_non_owner 那層都先擋掉了。CONFIRM 原本擋的是「意外花費」，
    # 改由 sample_order._consume_render_quota 的**每日張數硬上限**接手（分色計數、
    # RED_ORDER_RENDER_DAILY_MAX 可調）—— 上限比確認閘更擋得住迴圈亂打。
    # 同組的 generate_product_concept / generate_from_sample / generate_jf_shoe
    # 沒有這層上限，維持 CONFIRM。
    "generate_from_order": TIER_SAFE,
    # auto-link helper — 改 task linked_email_threads
    "link_last_sent_email_to_task": TIER_SAFE,
    # 行事曆記事：建立 / 更新自己的行事曆 → SAFE（刪除走 ^delete_ pattern = DANGEROUS）。
    # 兩者沒 override 時：create 落 _SENSITIVE_TOOLS fallback=CONFIRM、update 撞
    # ^update_ pattern=DANGEROUS — 都要顯式 override 才降得下來。
    "create_calendar_event": TIER_SAFE,
    "update_calendar_event": TIER_SAFE,

    # ── Work mode 切換 ──
    # 攻擊者切到 dev mode 會解鎖更多 tool 行為（dev 不 narrow），要 +確認
    "set_work_mode": TIER_CONFIRM,
    "exit_work_mode": TIER_CONFIRM,

    # ── Telegram 檔案傳送 ──
    # 寄檔案 = 對外 egress，可能被 LLM 騙著傳機密檔（雖然 path_safety 擋多數路徑）
    # CONFIRM tier 跟 send_gmail 同級
    "telegram_send_file": TIER_CONFIRM,
    "telegram_send_photo": TIER_CONFIRM,
    "telegram_send_attachment": TIER_CONFIRM,

    # ── Vision RPA Engine ──
    # fill_form 啟動自主 UI 控制 loop — 一次 +雙確認 授權整個任務
    # 內部跑 max_steps 步 click/type/scroll，跟 run_shell 同等高風險
    "fill_form": TIER_DANGEROUS,
    # list / show 是 read-only audit
    "list_rpa_runs": TIER_SAFE,
    "show_rpa_run": TIER_SAFE,

    # ── IDP（文件理解 / 自動填表）──
    "read_document": TIER_SAFE,            # 純讀
    "extract_document_fields": TIER_SAFE,  # 讀 + LLM 分析，無寫
    "fill_document": TIER_CONFIRM,         # 寫新檔（限 var/data/idp_outputs/）
    "auto_fill_document": TIER_CONFIRM,    # 寫新檔 + 用 recall（會看記憶）

    # ── YouTube ──
    # 下載/轉檔會寫入 ~/Downloads，但不需要任意 shell/code-exec；一般確認即可。
    "download_youtube_audio": TIER_CONFIRM,
    "download_youtube_video": TIER_CONFIRM,
    "download_online_video": TIER_CONFIRM,
    "adjust_audio_pitch": TIER_CONFIRM,

    # ── Media security / DRM engineering ──
    # 解析與命令生成是 read-only；產生 key material / 打包會產生秘密或寫檔，需確認。
    "media_security_blueprint": TIER_SAFE,
    "inspect_iso_bmff": TIER_SAFE,
    "parse_pssh_box": TIER_SAFE,
    "build_ffmpeg_cenc_command": TIER_SAFE,
    "analyze_drm_manifest": TIER_SAFE,
    "assess_drm_request_safety": TIER_SAFE,
    "drm_chain_of_trust_model": TIER_SAFE,
    "build_eme_player_template": TIER_SAFE,
    "generate_cenc_key_material": TIER_CONFIRM,
    "simulate_license_challenge": TIER_CONFIRM,
    "package_hls_aes128": TIER_CONFIRM,
    "download_hls_with_n_m3u8dl": TIER_CONFIRM,
    "download_hls_with_ffmpeg_copy": TIER_CONFIRM,
    "download_hls_with_ytdlp": TIER_CONFIRM,

    # ── alert_pusher 工具（observability）──
    "alert_push_status": TIER_SAFE,    # 純讀 push state
    "push_alerts_now": TIER_SAFE,      # 觸發 push（內部走 telegram_push 已 V4 保護）

    # ── audit log 搜尋（episodic memory access）──
    "find_past_actions": TIER_SAFE,    # 純讀 audit log，無副作用

    # ── Owner session 主控台（遠端控制活躍對話 session）──
    # list 純讀（owner-only 由 telegram_actor_scope.OWNER_PRIVATE_READ_TOOLS
    # 把關）；pause/resume/reset 會改 session 控制狀態 → CONFIRM，且列入
    # tg_auth._SENSITIVE_TOOLS 使非 owner session build 時整顆移除。
    "list_sessions": TIER_SAFE,
    "pause_session": TIER_CONFIRM,
    "resume_session": TIER_CONFIRM,
    "reset_session": TIER_CONFIRM,
    # broadcast 對外發訊（egress）→ 與 send_gmail 同級 CONFIRM，且列入
    # _SENSITIVE_TOOLS 使非 owner 移除。只能發給登記在案的活躍 session。
    "broadcast_message": TIER_CONFIRM,

    # ── CONFIRM — 其他 sensitive 預設值（明列以便 audit 看清楚）──
    # 大多數 send_* / create_* / write_external 都 default 落這層，靠 pattern 不需 override

    # ── 額外：明確標 SAFE 的（即使有副作用但極輕量 / read-mostly）──
    "list_dry_run_log": TIER_SAFE,
    "dry_run_status": TIER_SAFE,
    "last_dry_run_log": TIER_SAFE,
}


# ────────────────────────────────────────────────────────────────────
# Pattern rules — 抓 _SENSITIVE_TOOLS 中沒列在 _TIER_OVERRIDES 的
# ────────────────────────────────────────────────────────────────────
_PATTERN_RULES: list[tuple[re.Pattern, str]] = [
    # Deletion of any kind → DANGEROUS（除非明列在 LOCKED）
    (re.compile(r"^delete_"),       TIER_DANGEROUS),
    (re.compile(r"^forget_"),       TIER_DANGEROUS),
    (re.compile(r"_rebuild$"),      TIER_DANGEROUS),
    (re.compile(r"^prune_"),        TIER_DANGEROUS),
    (re.compile(r"^batch_"),        TIER_DANGEROUS),
    # Update existing record（calendar / event / 等）→ DANGEROUS
    (re.compile(r"^update_"),       TIER_DANGEROUS),
    # 沒列名的 sensitive default = CONFIRM（會在 fallback 處理）
]


@functools.lru_cache(maxsize=None)
def _static_tier(tool_name: str) -> str | None:
    """override / pattern 部分的 tier 查詢。只依賴模組級常量
    （_TIER_OVERRIDES / _PATTERN_RULES，皆不在 runtime 變動），可安全
    lru_cache。回 None 表示要落到 sensitive fallback 判斷。"""
    if tool_name in _TIER_OVERRIDES:
        return _TIER_OVERRIDES[tool_name]
    for pat, tier in _PATTERN_RULES:
        if pat.search(tool_name):
            return tier
    return None


@functools.lru_cache(maxsize=1)
def _sensitive_tools_frozen() -> frozenset:
    """tg_auth._SENSITIVE_TOOLS 的快照。import 失敗會 raise —— lru_cache
    不快取例外，所以下一次呼叫會重試 import，「fail-open 被永久固化」
    的舊 bug（get_tier 整顆被 cache、import 失敗回 SAFE 也一起被記住）
    不會再發生。"""
    from agent_core.tg_auth import _SENSITIVE_TOOLS
    return frozenset(_SENSITIVE_TOOLS)


def get_tier(tool_name: str) -> str:
    """回傳工具的 tier。優先序：override > pattern > sensitive-default-CONFIRM > SAFE。

    每個工具呼叫鏈會查 tier 2-3 次（evaluate_policy / filter_tools_for_telegram
    都打到這裡），常量部分（override / pattern）走 lru_cache；tg_auth 的
    sensitive 名單查詢**不在**被 cache 的函式裡 —— 舊版把整顆 get_tier 包
    lru_cache，tg_auth import 失敗時的 SAFE fallback 會被永久快取（fail-open
    固化）。import 失敗時偏 fail-closed：當 CONFIRM 處理、且不進 cache，
    下次呼叫重試。"""
    if not tool_name:
        return TIER_SAFE
    tier = _static_tier(tool_name)
    if tier is not None:
        return tier
    try:
        sensitive = _sensitive_tools_frozen()
    except Exception:
        # tg_auth import 失敗（早期啟動 / 循環 import 窗口）：此時無從判斷
        # 名單，寧可 fail-closed 回 CONFIRM，也不要 fail-open 放行 SAFE。
        return TIER_CONFIRM
    return TIER_CONFIRM if tool_name in sensitive else TIER_SAFE


def tier_at_least(tool_name: str, tier: str) -> bool:
    """tool_name 的 tier 是否 ≥ given tier。
    例：tier_at_least('run_shell', TIER_CONFIRM) = True。"""
    return _TIER_ORDER.get(get_tier(tool_name), 0) >= _TIER_ORDER.get(tier, 0)


def is_locked(tool_name: str) -> bool:
    return get_tier(tool_name) == TIER_LOCKED


def is_dangerous(tool_name: str) -> bool:
    return get_tier(tool_name) == TIER_DANGEROUS


# ────────────────────────────────────────────────────────────────────
# Channel × Tier → action matrix
# ────────────────────────────────────────────────────────────────────
# Action codes:
#   "allow"     → 直接放行
#   "token"     → 走 +確認 90s window（M5 one-shot revoke）
#   "token+warn"→ 走 token gate，但回應前加 🔴 警示 + 強制 audit screen
#   "refuse"    → 拒絕，給訊息建議改用其他頻道
_CHANNEL_RULES: dict[str, dict[str, str]] = {
    CHANNEL_TELEGRAM: {
        TIER_SAFE:      "allow",
        TIER_CONFIRM:   "token",
        TIER_DANGEROUS: "token+warn",
        TIER_LOCKED:    "refuse",
    },
    CHANNEL_VOICE: {
        TIER_SAFE:      "allow",
        TIER_CONFIRM:   "token",
        TIER_DANGEROUS: "refuse",   # voice 太容易誤聽，dangerous 一律改 Telegram
        TIER_LOCKED:    "refuse",
    },
    CHANNEL_DAEMON: {
        TIER_SAFE:      "allow",
        TIER_CONFIRM:   "allow",    # 在 add_scheduled_task 時已 pre-authorized
        TIER_DANGEROUS: "allow",    # 同上 — 但 audit 會記
        TIER_LOCKED:    "refuse",   # daemon 不該動 vault / persona / 系統 toggle
    },
    CHANNEL_REPL: {
        # 大王 親手在 Mac terminal 打字 — 信任 physical access，無 gate
        TIER_SAFE:      "allow",
        TIER_CONFIRM:   "allow",
        TIER_DANGEROUS: "allow",
        TIER_LOCKED:    "allow",
    },
}


def get_check_method(channel: str, tier: str) -> str:
    """回 'allow' / 'token' / 'token+warn' / 'refuse'。

    未知 channel 一律保守當 telegram，未知 tier 一律當 LOCKED。"""
    rules = _CHANNEL_RULES.get(channel, _CHANNEL_RULES[CHANNEL_TELEGRAM])
    return rules.get(tier, "refuse")


def refusal_message(channel: str, tool_name: str) -> str:
    """為 refused 工具產生人類可讀的拒絕訊息 + 建議。"""
    tier = get_tier(tool_name)
    icon = _TIER_ICON[tier]
    if tier == TIER_LOCKED:
        return (
            f"{icon} 工具 `{tool_name}` 被分類為 LOCKED — {channel} 頻道禁止呼叫。\n"
            f"   原因：{_TIER_DESC[tier]}\n"
            f"   建議：請大王到 Mac 親自打開 REPL 執行；攻擊者就算拿到 Telegram\n"
            f"   也碰不到 vault / 系統 toggle / persona 等核心狀態。"
        )
    if tier == TIER_DANGEROUS and channel == CHANNEL_VOICE:
        return (
            f"{icon} 工具 `{tool_name}` 被分類為 DANGEROUS — voice 頻道禁止\n"
            f"   （Whisper 誤聽風險）。請大王改在 Telegram 對話 +確認。"
        )
    return (
        f"{icon} 工具 `{tool_name}` 在此 channel ({channel}) 不允許執行。\n"
        f"   tier = {tier}（{_TIER_DESC[tier]}）"
    )


# ────────────────────────────────────────────────────────────────────
# Audit / introspection helpers
# ────────────────────────────────────────────────────────────────────
def all_tools_by_tier() -> dict[str, list[str]]:
    """掃 tools_list，回 dict[tier → sorted tool names]。給 dashboard / CLI 用。"""
    try:
        from agent_core.tool_registry import tools_list
    except Exception:
        return {}
    buckets: dict[str, list[str]] = {
        TIER_SAFE: [], TIER_CONFIRM: [], TIER_DANGEROUS: [], TIER_LOCKED: [],
    }
    for t in tools_list:
        name = getattr(t, "__name__", "")
        if not name:
            continue
        buckets[get_tier(name)].append(name)
    for k in buckets:
        buckets[k].sort()
    return buckets


def list_tools_by_tier(tier: str = "") -> str:
    """🔍 列出工具的權限分級（safe / confirm / dangerous / locked）。

    Args:
        tier: 'safe' / 'confirm' / 'dangerous' / 'locked' 任一個 → 只列該級。
              空字串 = 全部 4 級摘要。

    Returns:
        formatted text — 給 LLM / 大王看「哪些工具屬哪一級」。
    """
    buckets = all_tools_by_tier()
    if not buckets:
        return "（無法讀 tools_list）"
    if tier:
        tier_norm = tier.strip().lower()
        if tier_norm not in buckets:
            return (f"❌ 未知 tier '{tier}'。可選：safe / confirm / dangerous / locked")
        names = buckets[tier_norm]
        icon = _TIER_ICON[tier_norm]
        out = [
            f"{icon} {tier_norm.upper()} tier — {_TIER_DESC[tier_norm]}",
            f"   共 {len(names)} 個工具：",
        ]
        for n in names:
            out.append(f"     {n}")
        return "\n".join(out)
    # All 4
    out = ["🔍 工具權限分級總覽"]
    out.append("─" * 60)
    total = 0
    for t in (TIER_SAFE, TIER_CONFIRM, TIER_DANGEROUS, TIER_LOCKED):
        names = buckets[t]
        icon = _TIER_ICON[t]
        n = len(names)
        total += n
        out.append(f"  {icon} {t:10s} {n:4d} 個   {_TIER_DESC[t]}")
    out.append("─" * 60)
    out.append(f"  總計 {total} 個工具")
    out.append("")
    out.append("頻道行為：")
    for ch in (CHANNEL_TELEGRAM, CHANNEL_VOICE, CHANNEL_DAEMON, CHANNEL_REPL):
        rules = _CHANNEL_RULES[ch]
        line = f"  {ch:10s} "
        for t in (TIER_SAFE, TIER_CONFIRM, TIER_DANGEROUS, TIER_LOCKED):
            line += f"{t}={rules[t]:10s} "
        out.append(line)
    out.append("")
    out.append("💡 看單一 tier 細節：list_tools_by_tier('locked')")
    return "\n".join(out)
