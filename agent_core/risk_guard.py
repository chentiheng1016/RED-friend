"""Risk guard — content-level（args 級）高風險偵測。

問題：
  既有 tier 系統是「整個 tool 都某級」— 但 `run_shell("ls /tmp")` 跟
  `run_shell("rm -rf ~/")` 風險差異極大，tier 抓不到這層差異。
  攻擊情境：LLM 被 prompt-injection 後，DANGEROUS tool 已過 +雙確認，
  但 args 是「rm -rf ~」這種 catastrophic 內容，沒人擋。

設計：
  純偵測層 — 不執行、不阻擋（policy_engine 拿這個結果決定怎處理）。
  輸入 (tool_name, kwargs)，輸出 RiskAssessment：
    {score: 0-100, signals: [...], category: "ok"/"medium"/"high"/"critical"}

  Score 對應：
    0-29   ok        無明顯風險
    30-59  medium    可疑但不致命（warn + audit 即可）
    60-89  high      高風險（建議升級確認 / 多一道 hook）
    90-100 critical  破壞性（policy_engine 拒絕執行）

Patterns（per-tool，可擴展）：
  run_shell：rm -rf 根目錄 / fork bomb / dd raw device / mkfs / curl|sh
  manage_files：wildcard / home 根目錄 / 系統路徑
  send_gmail / reply_gmail：bulk recipients (>10)
  browser_eval：javascript:/data: URLs、document.cookie 存取
  delete_*：wildcard ID、整批刪
  ERP submit：金額 > 閾值（暫只放 hint，後續可接 ERP API）

設計取捨：
  - 不在這裡執行 fix（不 sanitize args）— 給 policy_engine 決定
  - 不依賴 LLM 二次判斷 — pure regex/dict lookup，可重現可測試
  - signals 是 list，方便 audit「為什麼算 70 分」
  - 寬嚴可調 — 沒抓到不算失敗（false negative tolerable，false positive 才煩）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ────────────────────────────────────────────────────────────────────
# Risk levels
# ────────────────────────────────────────────────────────────────────
RISK_OK = "ok"           # 0-29
RISK_MEDIUM = "medium"   # 30-59
RISK_HIGH = "high"       # 60-89
RISK_CRITICAL = "critical"  # 90-100


def _level_of(score: int) -> str:
    if score >= 90:
        return RISK_CRITICAL
    if score >= 60:
        return RISK_HIGH
    if score >= 30:
        return RISK_MEDIUM
    return RISK_OK


@dataclass
class RiskSignal:
    """單一個觸發的 risk 訊號。"""
    score: int
    pattern: str         # 短描述（給 audit 看）
    matched: str = ""    # 實際 match 到的 substring（過 redact 後）
    field: str = ""      # 是哪個 kwarg 觸發的


@dataclass
class RiskAssessment:
    score: int                              # 0-100，多 signal 取最高（不加總，避免雜訊膨脹）
    level: str                              # ok / medium / high / critical
    signals: list[RiskSignal] = field(default_factory=list)
    tool: str = ""

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "level": self.level,
            "tool": self.tool,
            "signals": [
                {"score": s.score, "pattern": s.pattern,
                 "matched": s.matched[:80], "field": s.field}
                for s in self.signals
            ],
        }


# ────────────────────────────────────────────────────────────────────
# Pattern library — per tool
# 每個 entry: (compiled regex, score, 描述)
# ────────────────────────────────────────────────────────────────────
_SHELL_PATTERNS: list[tuple[re.Pattern, int, str]] = [
    # ── critical (90+) ──
    (re.compile(r"rm\s+(-rf?|-fr?|-r\s+-f|-f\s+-r)\s+(/|\$HOME|~)(/?\*?)(\s|$)"),
     95, "rm -rf 根目錄/home"),
    (re.compile(r"rm\s+(-rf?|-fr?)\s+/\*"), 95, "rm -rf /*"),
    # dd 寫入 raw disk = 不可逆破壞（讀 /dev/zero 進 disk）
    (re.compile(r"\bof=/dev/(sd|hd|nvme|disk)[a-z0-9]+"), 95,
     "dd 寫入 raw disk device"),
    (re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
     99, "fork bomb (:(){:|:&};:)"),
    (re.compile(r"\bmkfs\.[a-z0-9]+"), 95, "mkfs 格式化檔案系統"),
    (re.compile(r"shutdown\s+(now|-h\s+now|-r\s+now)"), 90, "shutdown 立即關機"),
    (re.compile(r">/dev/sd[a-z]"), 90, "覆寫 raw disk"),
    # ── high (60-89) ──
    (re.compile(r"chmod\s+-R\s+777"), 70, "遞迴 chmod 777"),
    (re.compile(r"sudo\s+rm\s+-r"), 75, "sudo + rm -r"),
    (re.compile(r"(curl|wget)\s+\S+\s*\|\s*(bash|sh|zsh)"), 80,
     "curl/wget pipe to shell"),
    (re.compile(r"\beval\s+\$\("), 70, "shell eval"),
    (re.compile(r"history\s+-c"), 65, "清掉 shell history"),
    (re.compile(r"unset\s+(HOME|PATH|USER)"), 60, "unset 系統環境變數"),
    # ── medium (30-59) ──
    (re.compile(r"\bsudo\b"), 35, "使用 sudo"),
    (re.compile(r"rm\s+-r"), 40, "rm -r 遞迴刪"),
    (re.compile(r"\bnohup\b"), 30, "nohup 背景執行"),
    (re.compile(r">/dev/null\s+2>&1\s*&"), 30, "背景執行隱藏 output"),
    (re.compile(r"^\s*kill\s+-9\b"), 35, "kill -9 強殺"),
    (re.compile(r"\bcrontab\s+-r\b"), 50, "刪除 crontab"),
]

_FILE_PATTERNS: list[tuple[re.Pattern, int, str]] = [
    (re.compile(r"^\s*[/~]\s*$"), 90, "操作根目錄 / home 本身"),
    (re.compile(r"^/Users/[^/]+/?$"), 80, "操作 user home root"),
    (re.compile(r"^/(System|Library|usr|bin|sbin|etc|var|private)(/|$)"),
     85, "操作系統路徑"),
    (re.compile(r"\*\s*$"), 60, "wildcard 結尾"),
    (re.compile(r"\.\."), 70, "path traversal 樣式"),
]

_BROWSER_EVAL_PATTERNS: list[tuple[re.Pattern, int, str]] = [
    (re.compile(r"document\.cookie"), 70, "存取 cookie"),
    (re.compile(r"localStorage\.(getItem|setItem|clear)"), 65,
     "存取 localStorage"),
    (re.compile(r"javascript:", re.I), 80, "javascript: URL"),
    (re.compile(r"data:text/html", re.I), 70, "data: URL"),
    (re.compile(r"window\.(location|open)\s*=", re.I), 60, "改 location"),
    (re.compile(r"XMLHttpRequest|fetch\s*\("), 50, "JS 發送 HTTP"),
    (re.compile(r"\beval\s*\("), 75, "JS eval"),
    (re.compile(r"new\s+Function\s*\("), 65, "JS new Function"),
]

# Global pattern index by tool name
_TOOL_PATTERNS: dict[str, list[tuple[re.Pattern, int, str]]] = {
    "run_shell": _SHELL_PATTERNS,
    # MCP filesystem tools 也走 file 規則
    "manage_files": _FILE_PATTERNS,
    "write_file": _FILE_PATTERNS,
    "mcp_filesystem_write_file": _FILE_PATTERNS,
    "mcp_filesystem_edit_file": _FILE_PATTERNS,
    "mcp_filesystem_move_file": _FILE_PATTERNS,
    # Browser
    "browser_eval": _BROWSER_EVAL_PATTERNS,
}


# ────────────────────────────────────────────────────────────────────
# Bulk recipient detection（email / telegram tools）
# ────────────────────────────────────────────────────────────────────
_BULK_RECIPIENT_THRESHOLD = 10


def _count_recipients(value: Any) -> int:
    """to 欄位可能是 'a@b, c@d' string 或 list；數有幾個收件人。"""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str):
        # 估算 — comma / semicolon / newline 分割
        parts = re.split(r"[,;\n]+", value.strip())
        return sum(1 for p in parts if p.strip())
    return 0


# ────────────────────────────────────────────────────────────────────
# Main entry
# ────────────────────────────────────────────────────────────────────
def scan_for_risk(tool_name: str, kwargs: dict | None = None) -> RiskAssessment:
    """偵測 tool call 的 args-level 風險。

    Args:
        tool_name: 工具名（用來查 _TOOL_PATTERNS）
        kwargs: tool 呼叫的 kwargs

    Returns:
        RiskAssessment — 含 score / level / signals
    """
    kwargs = kwargs or {}
    signals: list[RiskSignal] = []
    max_score = 0

    # 1) 從 _TOOL_PATTERNS 跑該 tool 對應的 patterns
    patterns = _TOOL_PATTERNS.get(tool_name, [])
    if patterns:
        # 對所有 string-like kwarg value 掃
        for field_name, value in kwargs.items():
            if not isinstance(value, str):
                continue
            for pat, score, desc in patterns:
                m = pat.search(value)
                if m:
                    signals.append(RiskSignal(
                        score=score, pattern=desc,
                        matched=m.group(0)[:80], field=field_name,
                    ))
                    max_score = max(max_score, score)

    # 2) Bulk recipient detection（per-tool 自帶邏輯）
    if tool_name in ("send_gmail", "reply_gmail", "create_draft",
                     "telegram_push"):
        for field_name in ("to", "cc", "bcc", "recipients"):
            if field_name in kwargs:
                n = _count_recipients(kwargs[field_name])
                if n >= 50:
                    signals.append(RiskSignal(
                        score=85, pattern=f"bulk recipient ({n} 個)",
                        matched=f"{n} recipients",
                        field=field_name,
                    ))
                    max_score = max(max_score, 85)
                elif n >= _BULK_RECIPIENT_THRESHOLD:
                    signals.append(RiskSignal(
                        score=55, pattern=f"多收件人 ({n} 個 ≥ {_BULK_RECIPIENT_THRESHOLD})",
                        matched=f"{n} recipients",
                        field=field_name,
                    ))
                    max_score = max(max_score, 55)

    # 3) Wildcard / batch detection（各種 batch_ / delete_）
    for field_name, value in kwargs.items():
        if not isinstance(value, str):
            continue
        if value.strip() in ("*", ".*", "all", "ALL"):
            signals.append(RiskSignal(
                score=70, pattern="wildcard / all 樣式",
                matched=value.strip(), field=field_name,
            ))
            max_score = max(max_score, 70)

    return RiskAssessment(
        score=max_score,
        level=_level_of(max_score),
        signals=signals,
        tool=tool_name,
    )


# ────────────────────────────────────────────────────────────────────
# Decorator helper — 給特定 tool 強制做 risk check
# ────────────────────────────────────────────────────────────────────
def guard_risk(min_block_score: int = 90):
    """Decorator：執行前先 scan，超過 min_block_score 直接拒。

    通常 wrap_sensitive_tool 會自動接 risk_guard，這個 decorator 是給
    沒走 sensitive wrap 路徑的 tool（極少見）做手動加固。
    """
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            assessment = scan_for_risk(fn.__name__, kwargs)
            if assessment.score >= min_block_score:
                from agent_core.tool_result import ToolResult, ErrorCode
                top = assessment.signals[0] if assessment.signals else None
                return ToolResult.failure(
                    f"risk_guard 拒絕執行 `{fn.__name__}` — risk_score={assessment.score}",
                    error_code=ErrorCode.PERMISSION_DENIED,
                    recoverable=False,
                    suggested_fix=(
                        f"args 含「{top.pattern}」"
                        if top else "args 包含高風險樣式"
                    ),
                )
            return fn(*args, **kwargs)
        return wrapper
    return deco


# ────────────────────────────────────────────────────────────────────
# Public LLM-facing tool
# ────────────────────────────────────────────────────────────────────
def risk_assessment(tool_name: str, kwargs_json: str = "") -> str:
    """🟢 看某個 tool call 的 risk_score（給 LLM 自我檢查 / 大王 audit）。

    Args:
        tool_name: 工具名
        kwargs_json: kwargs 的 JSON 字串（給 LLM 用）

    Returns:
        formatted text — score / level / 觸發 signal
    """
    if not tool_name:
        return "❌ tool_name 必填"
    import json
    try:
        kwargs = json.loads(kwargs_json) if kwargs_json else {}
        if not isinstance(kwargs, dict):
            return "❌ kwargs_json 必須是 dict 樣式 JSON"
    except json.JSONDecodeError as e:
        return f"❌ kwargs_json 不是合法 JSON：{e}"
    a = scan_for_risk(tool_name, kwargs)
    icon = {RISK_OK: "🟢", RISK_MEDIUM: "🟡", RISK_HIGH: "🟠",
            RISK_CRITICAL: "🔴"}.get(a.level, "?")
    out = [
        f"{icon} risk_assessment: {tool_name}",
        f"   score: {a.score}/100  level: {a.level}",
    ]
    if a.signals:
        out.append("   觸發 signals:")
        for s in a.signals[:5]:
            out.append(f"     [{s.score:3d}] {s.pattern}  "
                       f"(field={s.field}: {s.matched[:40]})")
    else:
        out.append("   ✅ 沒抓到風險樣式")
    return "\n".join(out)
