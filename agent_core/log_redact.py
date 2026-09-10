r"""Log-write redactor (V9/V12/V13/C3)。

問題：
  shell_audit.log 把每行 shell 指令原樣存檔。LLM 會生成像
    `mysql -u root -pHUNTER2 ...`
    `curl -H "Authorization: Bearer eyJxxx" ...`
    `psql postgres://user:pass@db.example.com/...`
    `PGPASSWORD=secret pg_dump ...`
    `aws --profile prod s3 sync ... # AWS_SECRET_ACCESS_KEY=...`
  這類東西。寫進 audit log 後，log 檔本身就變成新攻擊面（被偷讀 → 拿到密碼）。

  cost.jsonl 算無風險（只記 token 數 / $$）。
  runs/*.json 已經有 _redact（key-based），但 result 字串可能含 secret payload。
  print() to stdout 會被 launchd capture 到 launchd 日誌，同樣風險。

修補：所有寫入 audit / 日誌前先過 redact_log_line。
  - 30+ 種 inline-secret pattern。分類：
      shell-inline：mysql -p、--password=、PGPASSWORD=、MYSQL_PWD=、
                    AWS_*、AZURE_*、git URL with token、curl auth header、
                    postgres / mongo URL、SSH 私鑰 inline
      cloud / OAuth：GCP（AIza）、OpenAI（sk-）、Anthropic（sk-ant-）、
                    AWS (AKIA)、Slack (xox*)、GitHub PAT (ghp_/github_pat_)、
                    GitLab (glpat-)、Stripe (sk_/pk_/rk_)、JWT (eyJ.eyJ.X)
      vendor token：Twilio (AC/SK)、Heroku (UUID 形)、Discord bot、npm_
      PII：信用卡（含 () . 各分隔符）、TW ID、US SSN、IBAN
      bearer / inline pwd：Authorization: Bearer X、密碼/通行碼/口令 + 值

  也加上 V5 的 PII pattern（信用卡 / API key / 私鑰 block / IBAN 等）— citation
  共用同一份 _PATTERNS 避免 drift。

  例外：CREDIT_CARD 預設**不啟用**（RED_REDACT_CREDIT_CARD=1 才開）。
  該 pattern 的 `\b\d{13,19}\b` 會吃掉任何裸長數字 — 製鞋業務信件裡的
  16 碼 Article 貨號、貨櫃號、追蹤碼全部誤遮（2026-06-12 生管產能日報
  實際踩到）。單一 owner 部署、owner 明示郵件內文要全文可讀，故改 opt-in。

  引用：實際 entries 數見 _PATTERNS 列表，每次新增/移除請同步更新此 docstring。
"""
from __future__ import annotations

import re
from typing import Pattern

from agent_core.env_utils import env_bool


# ────────────────────────────────────────────────────────────────────
# Pattern set
# ────────────────────────────────────────────────────────────────────
# Each entry: (label, compiled regex)
# 設計原則：寧可多 redact 一些誤判，也別漏真敏感
# （唯一例外：CREDIT_CARD，見 _active_patterns）
_ALL_PATTERNS: list[tuple[str, Pattern]] = [
    # ── Shell-inline secrets ──
    # mysql / mariadb -p<pwd> —— **只抓黏住形式**。mysql 真實用法密碼必須
    # 緊貼 -p（`-p SPACE` 是「互動式問密碼」，後面接的是 db 名不是密碼）。
    # 舊版的 `\s*` 讓 `mkdir -p var/...`、`ps -p 123`、`kill -9 -p ...` 全被
    # 誤遮成 [REDACTED]，audit log 可讀性大壞。
    # 注意 \b 對 `-` 沒幫助（space → `-` 都是 non-word），用 (?:^|\s) 鎖前綴
    ("MYSQL_PASS_SHORT",
     re.compile(r"(?:^|\s)-p[^\s]{4,}")),
    # postgres / mysql 長 flag --password=...
    ("DB_PASS_LONG",
     re.compile(r"--password\s*=\s*\S+", re.IGNORECASE)),
    # PGPASSWORD=xxx env-var prefix
    ("PG_PASSWORD_ENV",
     re.compile(r"\bPGPASSWORD\s*=\s*\S+")),
    # MYSQL_PWD=xxx
    ("MYSQL_PWD_ENV",
     re.compile(r"\bMYSQL_PWD\s*=\s*\S+")),
    # AWS env-var 線上設
    ("AWS_SECRET_ENV",
     re.compile(r"\bAWS_(?:SECRET_ACCESS_KEY|SESSION_TOKEN)\s*=\s*\S+")),
    ("AWS_ACCESS_KEY_ENV",
     re.compile(r"\bAWS_ACCESS_KEY_ID\s*=\s*[A-Z0-9]{16,}")),
    # Azure
    ("AZURE_SECRET_ENV",
     re.compile(r"\bAZURE_(?:CLIENT_SECRET|STORAGE_KEY)\s*=\s*\S+")),
    # GitHub PAT in URL: https://USER:TOKEN@github.com/...
    ("GIT_URL_TOKEN",
     re.compile(r"https?://[^\s/:@]+:[^\s/@]+@[\w.\-]+")),
    # generic curl Authorization header
    ("CURL_AUTH_HEADER",
     re.compile(r"-H\s*['\"]?Authorization\s*:\s*[^'\"]+['\"]?", re.IGNORECASE)),
    # postgres URL with password
    ("PG_URL",
     re.compile(r"postgres(?:ql)?://[^\s/:]+:[^\s/@]+@[\w.\-]+")),
    # mongo URL
    ("MONGO_URL",
     re.compile(r"mongodb(?:\+srv)?://[^\s/:]+:[^\s/@]+@[\w.\-]+")),
    # ssh -i path/to/private/key — log path is fine (not secret) BUT
    # if user pasted private key inline (unlikely but possible) catch it
    ("SSH_PRIVATE_INLINE",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----")),
    # ── API keys（同 V5）──
    ("GOOGLE_API_KEY",
     re.compile(r"\bAIza[0-9A-Za-z\-_]{30,}\b")),  # 寬鬆：35-39 都常見
    # OpenAI keys: legacy `sk-…`、project `sk-proj-…`、service-account
    # `sk-svcacct-…`、admin `sk-admin-…` — 都允許後段 dash/underscore
    # （新格式如 `sk-proj-AbCd_-…` 會 fail 舊的 `[A-Za-z0-9]{20,}` 因為 dash）
    ("OPENAI_API_KEY",
     re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}\b")),
    ("ANTHROPIC_API_KEY",
     re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("AWS_ACCESS_KEY_INLINE",
     re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("SLACK_TOKEN",
     re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("GITHUB_PAT",
     re.compile(r"\bghp_[A-Za-z0-9]{30,}\b")),  # classic 36 / fine-grained 更長
    ("GITHUB_FINE_GRAINED",
     re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("GITLAB_PAT",
     re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("STRIPE_KEY",
     re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    # 補完 review LOW 列舉的 token 形狀
    ("JWT",
     re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("TWILIO_SID",
     re.compile(r"\bAC[0-9a-fA-F]{32}\b")),
    ("TWILIO_AUTH_TOKEN",
     re.compile(r"\bSK[0-9a-fA-F]{32}\b")),  # Twilio API Keys 用 SK 前綴
    ("HEROKU_API_KEY",
     re.compile(r"\b[hH][0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    ("DISCORD_BOT_TOKEN",
     re.compile(r"\b[MN][A-Za-z0-9]{23}\.[A-Za-z0-9]{6}\.[A-Za-z0-9_\-]{27,}\b")),
    # Telegram bot token：<8-10 位 bot_id>:<35 字元 auth>。requests 例外訊息會把它
    # 以 /bot<id>:<token>/sendMessage 形式帶出（故不加前綴 \b，才抓得到 URL 內的）。
    ("TELEGRAM_BOT_TOKEN",
     re.compile(r"\d{8,10}:[A-Za-z0-9_-]{30,}")),
    ("NPM_TOKEN",
     re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")),
    # ── PII（同 V5）──
    # Credit card：4-19 位數字，分隔符可為空 / 空格 / dash / dot / 括號。
    # 允許每兩個數字之間最多 2 個分隔符（涵蓋 `)` + 空白 = `) ` 的常見組合）。
    # 用 negative look-around 鎖頭尾，避免吃到 IBAN（GB29NWBK60161331...）等
    # 字母+數字混合字串中的 digit run。
    ("CREDIT_CARD",
     re.compile(r"(?<![A-Za-z\d])(?:\d[ \-.()]{0,2}){13,19}\d(?![A-Za-z\d])"
                r"|\b\d{13,19}\b")),
    ("TW_ID",
     re.compile(r"\b[A-Z][12]\d{8}\b")),
    ("US_SSN",
     re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("IBAN",
     re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{12,30}\b")),
    # ── Inline password / generic token ──
    # `password: xxx` / `password = xxx` / 「密碼是 xxx」/「password is xxx」
    # 寬鬆：password 後可接 is / are / 是 / 為 等聯繫詞、或 :/=、或單純空白，再接 4+ 字元值
    # 注意：CJK 字之間沒有 \b（兩邊都是 word char），所以中文版不能加 \b
    ("INLINE_PASSWORD",
     re.compile(r"(?i)(?:\b(?:password|passwd|pwd)\b|密碼|通行碼|口令)"
                r"\s*(?:is|are|為|是)?\s*[:：=]?\s*\S{4,}")),
    # Y6 補丁（review round 6）：dict / JSON repr 形式 — `'password': 'xxx'`
    # 因為 key 與 value 都被引號包，原 INLINE_PASSWORD 的「password [: ] value」
    # 連續 \S 期望會在第一個 `'` 後遇到 `:` 跟空白即斷掉。這裡顯式處理。
    ("INLINE_PASSWORD_DICT",
     re.compile(r"""(?ix)
        (?:^|[\s,{(\[])             # 前邊界：行首 / 空白 / dict 開頭 / 逗號
        ['"](?:password|passwd|pwd|api[_\-]?key|secret|token|credential)['"]
        \s*[:=]\s*
        ['"][^'"]{4,}['"]            # 引號包的 value，至少 4 字元
        """)),
    # `token: xxx`
    ("INLINE_TOKEN",
     re.compile(r"(?i)\b(?:token|api[_-]?key)\s*[:：=]\s*\S{8,}")),
    # 裸 Bearer / Authorization（即使不在 -H 裡，也算敏感）
    ("BEARER_TOKEN",
     re.compile(r"(?i)\b(?:Authorization|Bearer)\s*[:=]?\s*[A-Za-z0-9._\-]{20,}")),
]

def _active_patterns(redact_credit_card: bool) -> list[tuple[str, Pattern]]:
    """回傳實際生效的 pattern 名單。

    CREDIT_CARD 是唯一條件式 pattern，預設不啟用：`\\b\\d{13,19}\\b` 對「任何
    裸長數字」開火，業務信件裡的 16 碼 Article 貨號 / 貨櫃號 / 物流追蹤碼全
    會被誤遮成 [REDACTED:CREDIT_CARD]（2026-06-12 生管產能日報實際誤傷）。
    單一 owner 部署、owner 明示郵件內文要全文可讀；多人/部門化部署要重開
    時設 RED_REDACT_CREDIT_CARD=1（daemon 重啟生效）。
    """
    if redact_credit_card:
        return list(_ALL_PATTERNS)
    return [(label, pat) for label, pat in _ALL_PATTERNS if label != "CREDIT_CARD"]


# 在 import 時定下名單（而不是各呼叫端自查 env）：citation 直接 import
# _PATTERNS 也同步生效。
_PATTERNS = _active_patterns(env_bool("RED_REDACT_CREDIT_CARD", False))


def redact_log_line(text: str) -> str:
    """掃 text 中的 inline secrets / PII，找到的用 [REDACTED:LABEL] 取代。

    用於 audit log 寫檔前、stdout print 前。
    """
    if not text:
        return text
    out = text
    for label, pat in _PATTERNS:
        out = pat.sub(f"[REDACTED:{label}]", out)
    return out


def has_secret(text: str) -> bool:
    """快速判斷：text 含 inline secret 嗎？回 bool。"""
    if not text:
        return False
    return any(pat.search(text) for _, pat in _PATTERNS)


# ────────────────────────────────────────────────────────────────────
# C3 — logging.Filter（review 找到 V13 只擋 print，logger.* 全溜走）
# ────────────────────────────────────────────────────────────────────
import logging as _logging


class _RedactFilter(_logging.Filter):
    """Filter 掛在 root logger / handler 上，每筆 log record 過濾後再送出。

    覆蓋 record.msg + record.args（兩者用來組最終訊息）。stderr handler 寫
    出去前已 redact。
    """

    def filter(self, record: _logging.LogRecord) -> bool:  # noqa: D401
        try:
            if isinstance(record.msg, str):
                record.msg = redact_log_line(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    # 具名格式 logger.x('%(k)s', {...})：redact values、保持 dict，
                    # 否則 getMessage() 的 msg % args 會因 args 不再是 mapping 而 TypeError。
                    record.args = {
                        k: redact_log_line(v) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                else:
                    record.args = tuple(
                        redact_log_line(a) if isinstance(a, str) else a
                        for a in record.args
                    )
        except Exception:
            # 萬一 redact 自己壞掉也別把 log 系統打死
            pass
        return True


_FILTER_INSTALLED = False


def install_logging_filter() -> bool:
    """把 _RedactFilter 掛到 root logger + 已存在的所有 handler。

    Round 6 Y8 強化：之前只在 root logger 上。child logger 自己加 handler
    時（任何 module 寫 `logger.addHandler(...)`），那 handler 不會經 root filter。
    現在改成：root logger + 所有 existing child loggers + 所有 handler 全掛。

    Idempotent — 多次呼叫只會重複 add 嘗試，但有 if 防重。
    """
    global _FILTER_INSTALLED
    if _FILTER_INSTALLED:
        return False
    flt = _RedactFilter()
    root = _logging.getLogger()
    root.addFilter(flt)
    for h in root.handlers:
        if not any(isinstance(f, _RedactFilter) for f in h.filters):
            h.addFilter(flt)
    # Y8: 遞迴掃所有已存在的 child logger（logging 模組以平面 dict 存）
    manager = _logging.Logger.manager
    for name, child in list(manager.loggerDict.items()):
        if not isinstance(child, _logging.Logger):  # PlaceHolder 可能也存在
            continue
        if not any(isinstance(f, _RedactFilter) for f in child.filters):
            child.addFilter(flt)
        for h in child.handlers:
            if not any(isinstance(f, _RedactFilter) for f in h.filters):
                h.addFilter(flt)
    _FILTER_INSTALLED = True
    return True


# Y8 + round 7 LOW：Logger.addHandler / addFilter / removeFilter 的 monkey-patch。
# 之後任何新建的 Logger 加 handler 或調整 filter 都不會繞過 redact。
_orig_logger_addHandler = _logging.Logger.addHandler
_orig_logger_addFilter = _logging.Logger.addFilter
_orig_logger_removeFilter = _logging.Logger.removeFilter


def _addHandler_with_redact(self, hdlr):
    out = _orig_logger_addHandler(self, hdlr)
    try:
        if _FILTER_INSTALLED and not any(
            isinstance(f, _RedactFilter) for f in hdlr.filters
        ):
            hdlr.addFilter(_RedactFilter())
    except Exception:
        pass
    return out


def _addFilter_with_redact(self, flt):
    """如果 module 加 filter，確保 RedactFilter 仍掛在前面。"""
    out = _orig_logger_addFilter(self, flt)
    try:
        if _FILTER_INSTALLED and not any(
            isinstance(f, _RedactFilter) for f in self.filters
        ):
            _orig_logger_addFilter(self, _RedactFilter())
    except Exception:
        pass
    return out


def _removeFilter_with_redact_keep(self, flt):
    """避免 module 不小心把 RedactFilter remove 掉。"""
    if isinstance(flt, _RedactFilter) and _FILTER_INSTALLED:
        # 拒絕：這是安全 filter，不允許 module 端移除
        return
    return _orig_logger_removeFilter(self, flt)


_logging.Logger.addHandler = _addHandler_with_redact
_logging.Logger.addFilter = _addFilter_with_redact
_logging.Logger.removeFilter = _removeFilter_with_redact_keep


# Auto-install on import — 只要任何 module 從這裡匯入 redact_log_line，
# logger 就同時被加上保護。
install_logging_filter()
