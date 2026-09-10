"""Unified tool result shape — observability without breaking 280 tools.

問題：現有 ~280 個 tool 都回 plain string。LLM、daemon、Telegram、dashboard
各自字串解析（"找不到" / "❌" / "失敗"）— 沒一致 schema：
  - dashboard 想分類錯誤型態（rate limit / timeout / validation / network）
    無法靠字串可靠抓
  - daemon 想知道「這 error 該不該 retry」 → 沒結構化 recoverable flag
  - Telegram 想對 fail 加 🔴 emoji + 提示「該怎麼解」 → 要硬解析訊息
  - LLM 看到 "失敗" 字串不確定要不要再呼叫一次 / 找替代方案

直接強制 280 個 tool 改回傳 dict 太破壞性：
  - LLM 訓練習慣是看自然語言 string；換成 JSON 行為會變
  - Telegram bot 直接 print 結果；JSON 給大王看不友善
  - 280 個 tool 改寫 + 修對應 280 組 test = 數週工作

折衷設計（這裡採用）：
  ToolResult 繼承 str — 文字 surface 不變（drop-in），結構化 metadata 用
  attribute 暴露。
    - 現有 callers `print(result)` / `f"...{result}"` / `if "abc" in result`
      全部維持原行為
    - 新 callers / dashboard / observability 走 result.ok / result.error_code
    - run_history.audited 偵測 ToolResult instance → 把 ok / error_code /
      recoverable / suggested_fix 寫進 index.jsonl
    - 既有 plain-string tools 不用動；audited 用 heuristic（marker 前綴 +
      exception type）retroactively 分類

兩種 shape：

  success：
    ToolResult.success(summary, data=None, warnings=[], cost={}, artifacts=[])
    str 值 = summary（讓 LLM / Telegram 看到的文字）
    .ok = True
    .data / .warnings / .cost / .artifacts 給 dashboard / programmatic

  failure：
    ToolResult.failure(message, error_code=..., recoverable=True, suggested_fix=...)
    str 值 = formatted message（含 ❌ 前綴 + suggested_fix）
    .ok = False
    .error_code / .recoverable / .suggested_fix 給 daemon retry decision

每加新 tool 鼓勵直接用 success() / failure()。Migration 不強迫，但隨手改。
"""
from __future__ import annotations

from typing import Any


# ────────────────────────────────────────────────────────────────────
# 標準 error codes — daemon / dashboard 用來分類
# ────────────────────────────────────────────────────────────────────
class ErrorCode:
    # ── Recoverable（可自動 retry / queue 排重試）──
    RATE_LIMITED = "rate_limited"           # API rate limit；wait + retry
    BUDGET_EXHAUSTED = "budget_exhausted"   # tool_budgets daily/hourly cap
    TIMEOUT = "timeout"                     # 操作超時
    NETWORK = "network"                     # 連線失敗 / DNS / 5xx
    LOCKED_BY_MUTEX = "locked_by_mutex"     # task_queue mutex 卡住
    NEEDS_CONFIRMATION = "needs_confirmation"  # 缺 +確認 / +雙確認
    RETRY_LATER = "retry_later"             # 一般 transient

    # ── Unrecoverable（重試也不會好；要人介入）──
    LOCKED_TIER = "locked_tier"             # tool 是 LOCKED tier，channel 拒
    INVALID_INPUT = "invalid_input"         # 參數壞 / 格式錯
    NOT_FOUND = "not_found"                 # resource 不存在
    PERMISSION_DENIED = "permission_denied" # auth fail（非 confirmation）
    QUOTA_EXCEEDED = "quota_exceeded"       # 帳號級 quota 滿（vs budget 是 process 級）
    INTERNAL = "internal"                   # bug / 未預期例外
    UNSUPPORTED = "unsupported"             # 不支援的操作 / 未實作

    # 是否該 retry
    _RECOVERABLE: frozenset[str] = frozenset({
        RATE_LIMITED, TIMEOUT, NETWORK, LOCKED_BY_MUTEX,
        RETRY_LATER, BUDGET_EXHAUSTED,  # budget 隔天會解
    })

    @classmethod
    def is_recoverable(cls, code: str) -> bool:
        return code in cls._RECOVERABLE


# ────────────────────────────────────────────────────────────────────
# ToolResult — drop-in str subclass with structured metadata
# ────────────────────────────────────────────────────────────────────
class ToolResult(str):
    """str subclass — text surface 不變，結構化 metadata 走 attribute。

    Don't construct directly — 用 ToolResult.success() / .failure() factory。
    """
    # Slots-like：避免 attribute typo
    ok: bool
    summary: str
    error_code: str
    message: str
    recoverable: bool
    suggested_fix: str
    data: Any
    warnings: list
    cost: dict
    artifacts: list

    def __new__(cls, text: str, *, ok: bool = True, **meta) -> "ToolResult":
        # str 內容用 text；attribute 從 meta 灌
        inst = super().__new__(cls, text)
        inst.ok = ok
        inst.summary = meta.get("summary", text if ok else "")
        inst.error_code = meta.get("error_code", "")
        inst.message = meta.get("message", "")
        inst.recoverable = meta.get("recoverable", True if ok else
                                    ErrorCode.is_recoverable(meta.get("error_code", "")))
        inst.suggested_fix = meta.get("suggested_fix", "")
        inst.data = meta.get("data")
        inst.warnings = list(meta.get("warnings") or [])
        inst.cost = dict(meta.get("cost") or {})
        inst.artifacts = list(meta.get("artifacts") or [])
        return inst

    # ── Factory ──
    @classmethod
    def success(cls, summary: str, *, data: Any = None,
                warnings: list | None = None, cost: dict | None = None,
                artifacts: list | None = None) -> "ToolResult":
        """成功結果。summary 是 LLM / Telegram 看到的字串。"""
        return cls(summary, ok=True, summary=summary,
                   data=data, warnings=warnings or [],
                   cost=cost or {}, artifacts=artifacts or [])

    @classmethod
    def failure(cls, message: str, *, error_code: str = ErrorCode.INTERNAL,
                recoverable: bool | None = None,
                suggested_fix: str = "") -> "ToolResult":
        """失敗結果。message 是 LLM / Telegram 看到的字串（已含 ❌ 前綴）。"""
        if not message.startswith("❌") and not message.startswith("⏸"):
            text = f"❌ {message}"
        else:
            text = message
        if suggested_fix:
            text = f"{text}\n   💡 {suggested_fix}"
        if recoverable is None:
            recoverable = ErrorCode.is_recoverable(error_code)
        return cls(text, ok=False,
                   message=message, error_code=error_code,
                   recoverable=recoverable, suggested_fix=suggested_fix)

    # ── Serialization ──
    def to_dict(self) -> dict:
        """轉 dict（給 audit / dashboard / JSON-serialize 用）。"""
        if self.ok:
            return {
                "ok": True,
                "summary": self.summary,
                "data": self.data,
                "warnings": self.warnings,
                "cost": self.cost,
                "artifacts": self.artifacts,
            }
        return {
            "ok": False,
            "error_code": self.error_code,
            "message": self.message,
            "recoverable": self.recoverable,
            "suggested_fix": self.suggested_fix,
        }


# ────────────────────────────────────────────────────────────────────
# 對既有 plain-string tool 做 retroactive 分類（用 marker 前綴 + 內容）
# ────────────────────────────────────────────────────────────────────
_FAILURE_PREFIXES = ("❌", "⏸", "🔒", "🚫")
_FAILURE_KEYWORDS = ("失敗", "錯誤", "error", "exception", "找不到")
_NEEDS_CONFIRM_PATTERNS = ("需要大王確認", "需要 +確認", "+雙確認", "確認執行")
_BUDGET_PATTERNS = ("已執行", "上限", "queue 已滿")
_RATE_LIMIT_PATTERNS = ("rate-limit", "rate_limit", "rate limited", "rate-limited",
                        "已被 rate-limit")
_NOT_FOUND_PATTERNS = ("找不到", "不存在")


def classify_string_result(text: str) -> tuple[bool, str]:
    """猜既有 string-returning tool 的結果是 success 還是 failure。

    Returns:
        (ok, error_code)。ok=True 時 error_code=""。
    """
    if not isinstance(text, str) or not text:
        return True, ""
    # 強訊號優先：明確的 needs-confirmation pattern（即使沒 ❌ 前綴也算 failure，
    # CONFIRM tier 的 refusal 開頭是 🟡，不在 _FAILURE_PREFIXES）
    if any(p in text for p in _NEEDS_CONFIRM_PATTERNS):
        return False, ErrorCode.NEEDS_CONFIRMATION
    head = text.lstrip()[:8]
    looks_failure = any(head.startswith(p) for p in _FAILURE_PREFIXES)
    if not looks_failure:
        # 內容含 keyword 也當 failure（弱）
        low = text.lower()[:300]
        if any(k in low for k in _FAILURE_KEYWORDS):
            looks_failure = True
    if not looks_failure:
        return True, ""
    # 細分 error code
    if any(p in text for p in _BUDGET_PATTERNS):
        return False, ErrorCode.BUDGET_EXHAUSTED
    if any(p in text.lower() for p in _RATE_LIMIT_PATTERNS):
        return False, ErrorCode.RATE_LIMITED
    if "LOCKED" in text:
        return False, ErrorCode.LOCKED_TIER
    if "timeout" in text.lower():
        return False, ErrorCode.TIMEOUT
    if any(p in text for p in _NOT_FOUND_PATTERNS):
        return False, ErrorCode.NOT_FOUND
    return False, ErrorCode.INTERNAL


def classify_exception(exc: BaseException) -> str:
    """把 Python exception 轉成 ErrorCode。"""
    name = type(exc).__name__
    msg = str(exc).lower()
    if name in ("TimeoutError", "TimeoutExpired"):
        return ErrorCode.TIMEOUT
    if name in ("ConnectionError", "ConnectionResetError",
                "ConnectionRefusedError", "URLError", "HTTPError"):
        return ErrorCode.NETWORK
    if name in ("PermissionError",):
        return ErrorCode.PERMISSION_DENIED
    if name in ("FileNotFoundError", "KeyError", "LookupError"):
        return ErrorCode.NOT_FOUND
    if name in ("ValueError", "TypeError"):
        return ErrorCode.INVALID_INPUT
    if name in ("NotImplementedError",):
        return ErrorCode.UNSUPPORTED
    # 特定 message keyword
    if "rate" in msg and "limit" in msg:
        return ErrorCode.RATE_LIMITED
    if "timeout" in msg or "timed out" in msg:
        return ErrorCode.TIMEOUT
    if "quota" in msg:
        return ErrorCode.QUOTA_EXCEEDED
    return ErrorCode.INTERNAL
