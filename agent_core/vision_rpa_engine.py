"""Vision RPA Engine — controlled OBSERVE/THINK/ACT loop for autonomous UI tasks.

問題（大王 用 Telegram 跟我講的）：
  V4 One-shot 確認系統適合「寄一封信」這種單一 sensitive op，但對 RPA 多步
  填表流程根本不可用 — 11 頁表格每點一下、每填一格都要重新 +確認 + +雙確認，
  大王不可能每秒 +確認一次。

解法：建一個 controlled execution engine：
  - 大王 +確認 + +雙確認 一次 → 授權整個 task scope
  - Engine 在內部跑 OBSERVE → THINK → ACT loop
  - LLM 不能亂搞（嚴格 JSON 格式 + whitelist actions）
  - 多重防呆（重複偵測 / max_steps / stuck detect / abort）
  - 全程 audit log（var/state/rpa_runs/）

跟「直接讓 LLM 連續呼叫工具」的差別：
  ❌ 直接讓 LLM 連續呼叫
     - 每步要重新 confirm（V4 限制）
     - LLM 隨意決定 action 格式
     - 沒 state 記憶 → 鬼打牆（一直點同個地方）
     - 沒 max_steps → 可能跑無限循環

  ✅ controlled engine
     - 整個 task 一次授權
     - LLM 受限於 JSON schema + action whitelist
     - 內建 state（filled_fields / last_actions / step_count）
     - max_steps + stuck detect + 重複偵測 三層防呆

授權模型：
  fill_form(task_description, max_steps=50)  ← DANGEROUS tier
  ↓
  大王收到草擬 + 預估步數，回 +確認 + +雙確認
  ↓
  engine 自己跑（不再彈確認）
  ↓
  跑完報告：步驟摘要 + screenshot trail

安全：
  - DANGEROUS tier — 啟動需 +雙確認
  - tool_budget = 5/day（不能瘋狂呼叫）
  - 每步 audit log + screenshot
  - LLM JSON 格式錯誤 → retry 1 次後 abort
  - max_steps 預設 50（可調，硬上限 200）
  - LLM 連續 3 次同一 action → stuck，abort
  - LLM 自己回 abort 也 honor

不在這個 engine 處理的（要用其他 tool）：
  - 寄信 / 刪檔 / shell command — 還是要走正常 sensitive flow
  - 跨 app 切換的複雜 workflow — engine 鎖定當前 app
  - 真實 ERP 提交（高風險）— 用既有 erp.py workflow
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Any

from agent_core.logging_and_paths import STATE_DIR


# ────────────────────────────────────────────────────────────────────
# 常數 / 限制
# ────────────────────────────────────────────────────────────────────
_MAX_STEPS_HARD_CAP = 200           # 任何 caller 都不能超過
_DEFAULT_MAX_STEPS = 50
_STUCK_THRESHOLD = 3                # 連續 N 次同 action 視為卡住
_LLM_RETRY_ON_INVALID_JSON = 1      # JSON 解析失敗最多重試 1 次
_PER_STEP_TIMEOUT_SEC = 60          # 單步 ACT 不能超過 60s
_RUN_LOG_DIR = os.path.join(STATE_DIR, "rpa_runs")

# Whitelist actions — LLM 只能回這幾個
_VALID_ACTIONS = frozenset({
    "click",       # 點某 UI 元素
    "type",        # 在 focused field 輸入文字
    "scroll",      # 捲動畫面
    "select",      # 從 dropdown / list 選項
    "finish",      # 任務完成
    "abort",       # LLM 主動放棄（看到 error / 不確定）
})

# 安全：value 不能含這些（避免 LLM 被 inject 後寫破壞性內容到表單）
_FORBIDDEN_VALUE_PATTERNS = [
    re.compile(r"<script", re.I),
    re.compile(r"javascript:", re.I),
    re.compile(r"\brm\s+-rf?", re.I),
    re.compile(r":\(\)\s*\{"),  # fork bomb 起手式
]


# ────────────────────────────────────────────────────────────────────
# State / log helpers
# ────────────────────────────────────────────────────────────────────
_log_lock = threading.Lock()


def _ensure_log_dir() -> None:
    try:
        os.makedirs(_RUN_LOG_DIR, exist_ok=True)
    except Exception:
        pass


def _new_run_id() -> str:
    return ("rpa_" + datetime.now().strftime("%Y%m%dT%H%M%S")
            + "_" + os.urandom(2).hex())


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _append_run_log(run_id: str, entry: dict) -> None:
    """每步寫一行到 var/state/rpa_runs/{run_id}.jsonl。失敗 silent。"""
    _ensure_log_dir()
    path = os.path.join(_RUN_LOG_DIR, f"{run_id}.jsonl")
    try:
        with _log_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────
# 主 Engine
# ────────────────────────────────────────────────────────────────────
class VisionRPAEngine:
    """Controlled OBSERVE/THINK/ACT loop for autonomous UI tasks.

    用法：
        engine = VisionRPAEngine(task_description, max_steps=50)
        result = engine.run()
        # result: {"ok": bool, "reason": str, "steps": int, "filled_fields": [...]}
    """

    def __init__(self, task_description: str, *,
                  max_steps: int = _DEFAULT_MAX_STEPS,
                  app_name: str = ""):
        self.task = (task_description or "").strip()
        self.max_steps = max(1, min(max_steps, _MAX_STEPS_HARD_CAP))
        self.app_name = app_name.strip()
        self.run_id = _new_run_id()
        self.state: dict[str, Any] = {
            "run_id": self.run_id,
            "task": self.task,
            "started_at": _now_iso(),
            "filled_fields": [],     # [{"field": "...", "value": "...", "at": ...}]
            "last_actions": [],       # [decision_dict, ...]
            "step_count": 0,
        }
        # log session start
        _append_run_log(self.run_id, {
            "kind": "session_start",
            "at": _now_iso(),
            "task": self.task,
            "max_steps": self.max_steps,
            "app_name": self.app_name,
        })

    # ── OBSERVE ──
    def observe(self) -> dict:
        """擷螢幕 + 用 vision LLM 分析當前 UI 狀態。

        Returns:
            {screenshot_path, ui_summary, error}
        """
        # 1. 擷螢幕
        screenshot_path = ""
        try:
            from agent_core.run_history import _capture_screen
            screenshot_path = _capture_screen(self.run_id,
                                                f"step{self.state['step_count']:03d}") or ""
        except Exception as e:
            return {"error": f"擷圖失敗：{e}"}

        # 2. analyze_screen 拿 UI 結構描述
        ui_summary = ""
        try:
            from agent_core.vision import analyze_screen
            prompt_for_vision = (
                "請描述當前螢幕上的 UI 結構（按鈕 / 輸入欄位 / 標題 / 對話框），"
                "用條列式列出可互動元素的文字標籤與大致位置（左/中/右、上/下）。"
                "不要做任何推論，只描述看到的。"
            )
            ui_summary = analyze_screen(prompt_for_vision) or ""
        except Exception as e:
            ui_summary = f"(analyze_screen 失敗：{e})"

        return {
            "screenshot_path": screenshot_path,
            "ui_summary": ui_summary,
        }

    # ── THINK ──
    def think(self, observation: dict) -> dict:
        """送 obs + state 給 Gemini Flash，要求嚴格 JSON 回應。

        失敗 / 解析錯 → retry 一次，仍失敗 → 回 abort decision。
        """
        prompt = self._build_think_prompt(observation)
        # 用現役便宜 model（gemini-2.0-flash 已 deprecate）
        # 2.5-flash-lite 在這個 codebase 已用於 rerank，cost_tracker 也認得
        for attempt in range(1 + _LLM_RETRY_ON_INVALID_JSON):
            try:
                from agent_core.gemini_client import _gemini_generate
                resp = _gemini_generate(model="gemini-2.5-flash-lite",
                                         contents=[prompt])
                text = resp.text if hasattr(resp, "text") else str(resp)
            except Exception as e:
                if attempt >= _LLM_RETRY_ON_INVALID_JSON:
                    return {"action": "abort",
                            "reason": f"think LLM 失敗：{e}",
                            "_engine_internal": True}
                continue

            # 抽 JSON
            decision = self._parse_decision(text)
            if decision is not None:
                return decision
            # 解析失敗 → retry（提示更嚴格）
            if attempt >= _LLM_RETRY_ON_INVALID_JSON:
                return {"action": "abort",
                        "reason": f"LLM 連續回非 JSON：{text[:120]}",
                        "_engine_internal": True}
        return {"action": "abort", "reason": "think 超出 retry",
                "_engine_internal": True}

    def _build_think_prompt(self, observation: dict) -> str:
        last_3 = self.state["last_actions"][-3:]
        filled_summary = ", ".join(
            f"{f['field']}={f['value']}" for f in self.state["filled_fields"]
        ) or "（尚未填任何欄位）"
        return f"""你是一個 RPA agent，正在執行 UI 自動化任務。

**任務描述**：
{self.task}

**當前 step**: {self.state['step_count'] + 1} / {self.max_steps}

**狀態**：
  目前 app: {self.app_name or '（未指定）'}
  已填欄位: {filled_summary}
  最近 3 個 action: {json.dumps(last_3, ensure_ascii=False) if last_3 else '（無）'}

**當前 UI**（vision 分析結果）：
{observation.get('ui_summary', '（沒抓到）')[:2000]}

請決定下一步要做什麼。**嚴格用 JSON 回應**（不要 markdown 包裝、不要解釋）：

{{
  "action": "click" | "type" | "scroll" | "select" | "finish" | "abort",
  "target": "<目標元素的清楚文字描述，e.g. '統一編號 輸入欄' / '送出按鈕'>",
  "value": "<只在 action=type 或 select 時填，要輸入或選擇的值>",
  "reason": "<為什麼這樣做，1 句話>"
}}

規則：
- 若任務已完成 → action=finish
- 若 UI 看起來不對勁 / 需要大王介入 → action=abort
- 若連續 2-3 個 action 沒進展，主動 abort（不要鬼打牆）
- type 之前通常要先 click 對應欄位（focus）
- 找不到元素就 scroll，不要瞎猜座標
"""

    def _parse_decision(self, text: str) -> dict | None:
        """從 LLM 回應抽 JSON + 驗證 schema。"""
        # 嘗試 strip markdown code block
        t = text.strip()
        if t.startswith("```"):
            t = re.sub(r"^```(json)?\s*", "", t)
            t = re.sub(r"\s*```$", "", t)
        # 從第一個 { 用 JSONDecoder.raw_decode 做有狀態解析，正確處理字串值內的 {}
        # （舊的非貪婪 regex \{[\s\S]*?\} 遇到 reason 等欄位含 { 會提前截斷 → parse 失敗）。
        start = t.find("{")
        if start < 0:
            return None
        try:
            d, _ = json.JSONDecoder().raw_decode(t, start)
        except json.JSONDecodeError:
            return None
        if not isinstance(d, dict):
            return None
        action = str(d.get("action") or "").strip().lower()
        if action not in _VALID_ACTIONS:
            return None
        # 補欄位（str() 強制 — LLM 可能把 value/target 回成數字/布林，直接 .strip() 會 AttributeError）
        d["action"] = action
        d["target"] = str(d.get("target") or "").strip()[:200]
        d["value"] = str(d.get("value") or "").strip()[:500]
        d["reason"] = str(d.get("reason") or "")[:200]
        # type/select/click 必須有 target；type/select 必須有 value
        if action in ("click", "type", "select") and not d["target"]:
            return None
        if action in ("type", "select") and not d["value"]:
            return None
        # 安全：value 過 _FORBIDDEN_VALUE_PATTERNS
        for pat in _FORBIDDEN_VALUE_PATTERNS:
            if pat.search(d["value"]):
                return None  # 會被視為解析失敗 → retry / abort
        return d

    # ── ACT ──
    def act(self, decision: dict) -> dict:
        """根據 decision 呼叫對應 raw tool。

        engine 用 RAW tool（不過 wrap_sensitive_tool）— 因為 fill_form
        本身已經 +雙確認，內部 actions 共用這次授權。
        """
        action = decision["action"]
        if action == "click":
            return self._do_click(decision["target"])
        if action == "type":
            return self._do_type(decision["target"], decision["value"])
        if action == "scroll":
            # target 可選：上/下/left/right。預設 down。
            return self._do_scroll(decision.get("target", "") or "down")
        if action == "select":
            # 簡化：select 等同 click（大多數 dropdown 點開後再 click 選項）
            return self._do_click(decision["target"])
        # finish / abort 由 run() 處理，不該到這
        return {"ok": False, "error": f"unexpected action: {action}"}

    def _do_click(self, target: str) -> dict:
        """用 ax_click 點目標元素。失敗 fall back ax_search_text。"""
        try:
            from agent_core.accessibility import ax_click
            # ax_click 需要 app_name；engine init 時帶入或從目前 frontmost 抓
            app = self.app_name
            if not app:
                # 沒 hint → 用 frontmost app
                try:
                    import subprocess
                    result = subprocess.run(
                        ["osascript", "-e",
                         'tell application "System Events" to get name of first process whose frontmost is true'],
                        capture_output=True, text=True, timeout=3,
                    )
                    app = result.stdout.strip()
                except Exception:
                    app = ""
            if not app:
                return {"ok": False, "error": "找不到 app_name（前景 app 沒抓到）"}
            # ax_click(app_name, title, role="")
            r = ax_click(app, target)
            return {"ok": "✅" in str(r) or "成功" in str(r),
                    "raw": str(r)[:200]}
        except Exception as e:
            return {"ok": False, "error": f"click 失敗：{type(e).__name__}: {e}"}

    def _do_type(self, target: str, value: str) -> dict:
        """先 click target focus，再 type_text 輸入 value。"""
        click_r = self._do_click(target)
        if not click_r.get("ok"):
            return {"ok": False, "error": f"focus 失敗：{click_r.get('error', click_r)}"}
        try:
            from agent_core.input_devices import type_text
            r = type_text(value, interval=0.02)
            ok = "✅" in str(r) or "成功" in str(r) or "已輸入" in str(r)
            if ok:
                # 記到 filled_fields
                self.state["filled_fields"].append({
                    "field": target, "value": value, "at": _now_iso(),
                })
            return {"ok": ok, "raw": str(r)[:200]}
        except Exception as e:
            return {"ok": False, "error": f"type 失敗：{type(e).__name__}: {e}"}

    def _do_scroll(self, direction_or_target: str) -> dict:
        """direction = up/down/left/right（也 accept 中文 上/下/左/右）。"""
        d = direction_or_target.lower().strip()
        mapping = {
            "上": "up", "下": "down", "左": "left", "右": "right",
            "up": "up", "down": "down", "left": "left", "right": "right",
        }
        direction = mapping.get(d, "down")
        try:
            from agent_core.input_devices import scroll_screen
            r = scroll_screen(direction=direction, amount=5)
            return {"ok": True, "raw": str(r)[:200]}
        except Exception as e:
            return {"ok": False, "error": f"scroll 失敗：{type(e).__name__}: {e}"}

    # ── 防呆 ──
    def is_stuck(self) -> bool:
        """連續 _STUCK_THRESHOLD 次 action 完全相同 → 視為鬼打牆。"""
        last = self.state["last_actions"]
        if len(last) < _STUCK_THRESHOLD:
            return False
        recent = last[-_STUCK_THRESHOLD:]
        # 比對 action / target / value 都一樣
        keys = ("action", "target", "value")
        first = {k: recent[0].get(k) for k in keys}
        for d in recent[1:]:
            for k in keys:
                if d.get(k) != first[k]:
                    return False
        return True

    # ── 主 loop ──
    def run(self) -> dict:
        """OBSERVE → THINK → ACT 主 loop。

        Returns:
            {ok, reason, steps, filled_fields, run_id, state}
        """
        while self.state["step_count"] < self.max_steps:
            self.state["step_count"] += 1

            # OBSERVE
            obs = self.observe()
            if obs.get("error"):
                _append_run_log(self.run_id, {
                    "kind": "abort", "at": _now_iso(),
                    "reason": f"observe 失敗：{obs['error']}",
                    "step": self.state["step_count"],
                })
                return self._summary(False, f"observe 失敗：{obs['error']}")

            # THINK
            decision = self.think(obs)

            # 防呆：stuck 檢查（在 ACT 之前，這 decision 還沒進 last_actions）
            self.state["last_actions"].append(decision)
            if self.is_stuck():
                _append_run_log(self.run_id, {
                    "kind": "abort", "at": _now_iso(),
                    "reason": f"連續 {_STUCK_THRESHOLD} 次同 action 視為卡住",
                    "step": self.state["step_count"],
                    "stuck_action": decision,
                })
                return self._summary(False, "stuck — 連續同 action 中斷")

            # finish / abort 直接結束
            if decision["action"] == "finish":
                _append_run_log(self.run_id, {
                    "kind": "finish", "at": _now_iso(),
                    "reason": decision.get("reason", ""),
                    "step": self.state["step_count"],
                })
                return self._summary(True, f"任務完成：{decision.get('reason', '')}")
            if decision["action"] == "abort":
                _append_run_log(self.run_id, {
                    "kind": "abort", "at": _now_iso(),
                    "reason": decision.get("reason", ""),
                    "step": self.state["step_count"],
                    "by": "engine_internal" if decision.get("_engine_internal") else "llm",
                })
                return self._summary(False, f"abort：{decision.get('reason', '')}")

            # ACT
            t0 = time.time()
            act_result = self.act(decision)
            elapsed = time.time() - t0

            _append_run_log(self.run_id, {
                "kind": "step", "at": _now_iso(),
                "step": self.state["step_count"],
                "decision": decision,
                "result": act_result,
                "elapsed_sec": round(elapsed, 2),
                "screenshot": obs.get("screenshot_path", ""),
            })

            # 單步超時
            if elapsed > _PER_STEP_TIMEOUT_SEC:
                return self._summary(False, f"step {self.state['step_count']} 超時 {elapsed:.1f}s")

        # max_steps 用完
        _append_run_log(self.run_id, {
            "kind": "abort", "at": _now_iso(),
            "reason": "max_steps_exceeded",
            "step": self.state["step_count"],
        })
        return self._summary(False, f"max_steps={self.max_steps} 用完")

    def _summary(self, ok: bool, reason: str) -> dict:
        self.state["ended_at"] = _now_iso()
        self.state["ok"] = ok
        self.state["final_reason"] = reason
        return {
            "ok": ok,
            "reason": reason,
            "steps": self.state["step_count"],
            "filled_fields": list(self.state["filled_fields"]),
            "run_id": self.run_id,
            "log_file": os.path.join(_RUN_LOG_DIR, f"{self.run_id}.jsonl"),
        }


# ────────────────────────────────────────────────────────────────────
# Public tool
# ────────────────────────────────────────────────────────────────────
def fill_form(task_description: str, max_steps: int = _DEFAULT_MAX_STEPS,
               app_name: str = ""):
    """🔴 自主 UI 填表 / RPA — 啟動 vision RPA engine 執行多步 UI 任務。

    DANGEROUS tier — 一次 +確認 + +雙確認 授權整個任務（max_steps 步以內）。
    內部跑 OBSERVE/THINK/ACT loop，自己決定每步該點哪、該填什麼。

    Args:
        task_description: 任務描述（給 LLM 看），e.g.
            "在當前頁面填統編 27996872、代表人 (Owner)，按下一頁"
        max_steps: 最多跑幾步（預設 50，硬上限 200）
        app_name: 目前要操作的 app 名（e.g. "Safari" / "Numbers"）；
            空字串 = engine 自動抓前景 app

    Returns:
        ToolResult.success — 任務完成
        ToolResult.failure — abort / stuck / max_steps / 失敗

    安全：
        1. 自身 DANGEROUS tier — 大王 +雙確認 後才啟動
        2. tool_budget=5/day（防 LLM 連續呼叫燒 API）
        3. max_steps 硬上限 200（即使 caller 傳更大也截）
        4. 連續 3 次同 action → stuck，自動 abort
        5. JSON parse 失敗 retry 1 次後 abort
        6. value 過 _FORBIDDEN_VALUE_PATTERNS（擋 <script>、rm -rf 等）
        7. 全程 audit log var/state/rpa_runs/{run_id}.jsonl
        8. action 限 whitelist（click/type/scroll/select/finish/abort）

    使用情境（適合）：
      - ERP 多頁表單填寫
      - 重複性 UI 操作
      - LLM 看得懂「該按哪個按鈕」的場景
    使用情境（不適合）：
      - 跨 app workflow（用 task_queue + delegate_to_sub_agent）
      - 真實提交付款 / 不可逆操作（用既有 erp.py）
      - 需要瀏覽器特殊操作（用 browser_* tools）
    """
    from agent_core.tool_result import ToolResult, ErrorCode

    # 驗 task_description
    task = (task_description or "").strip()
    if not task:
        return ToolResult.failure("task_description 必填",
                                   error_code=ErrorCode.INVALID_INPUT,
                                   recoverable=False)
    if len(task) > 2000:
        return ToolResult.failure(
            "task_description 太長（>2000 字）— 拆成多個小任務",
            error_code=ErrorCode.INVALID_INPUT, recoverable=False,
        )

    # 跑 engine
    engine = VisionRPAEngine(task, max_steps=max_steps, app_name=app_name)
    try:
        result = engine.run()
    except Exception as e:
        return ToolResult.failure(
            f"engine 例外：{type(e).__name__}: {e}",
            error_code=ErrorCode.INTERNAL, recoverable=False,
        )

    # 報告
    summary_lines = [
        f"task: {task[:80]}",
        f"steps: {result['steps']} / {engine.max_steps}",
        f"reason: {result['reason']}",
        f"run_id: {result['run_id']}",
    ]
    if result["filled_fields"]:
        summary_lines.append("已填欄位：")
        for f in result["filled_fields"]:
            summary_lines.append(f"  - {f['field']} = {f['value']}")
    summary = "\n".join(summary_lines)

    if result["ok"]:
        return ToolResult.success(
            f"✅ RPA 任務完成\n{summary}",
            data={"run_id": result["run_id"],
                  "steps": result["steps"],
                  "filled_fields": result["filled_fields"]},
            artifacts=[result["log_file"]],
        )
    return ToolResult.failure(
        f"RPA 任務未完成 — {result['reason']}\n{summary}",
        error_code=ErrorCode.INTERNAL,
        recoverable=False,
        suggested_fix=("檢查 audit log: " + result["log_file"]
                       + "；可重新跑或手動接手剩下步驟"),
    )


def list_rpa_runs(limit: int = 10) -> str:
    """🟢 列最近 N 個 RPA engine 跑過的 run（給 audit / debug 用）。"""
    if not os.path.isdir(_RUN_LOG_DIR):
        return "（尚無 RPA runs — var/state/rpa_runs/ 不存在）"
    try:
        files = sorted(
            (f for f in os.listdir(_RUN_LOG_DIR)
             if f.endswith(".jsonl") and f.startswith("rpa_")),
            reverse=True,
        )[:limit]
    except Exception as e:
        return f"❌ 讀 RPA log dir 失敗：{e}"
    if not files:
        return "（沒有 RPA runs）"
    out = [f"🤖 最近 {len(files)} 個 RPA runs"]
    out.append("─" * 60)
    for fname in files:
        run_id = fname[:-6]  # strip .jsonl
        path = os.path.join(_RUN_LOG_DIR, fname)
        # 讀首尾摘要
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if not lines:
                continue
            first = json.loads(lines[0])
            last = json.loads(lines[-1])
            task = first.get("task", "")[:50]
            kind = last.get("kind", "?")
            steps = last.get("step", 0)
            icon = {"finish": "✅", "abort": "❌"}.get(kind, "⏳")
            out.append(f"  {icon} {run_id}  steps={steps}")
            out.append(f"      {task}")
            if kind == "abort":
                out.append(f"      reason: {last.get('reason', '')[:60]}")
        except Exception:
            out.append(f"  ⚠️  {run_id}（log 損壞）")
    return "\n".join(out)


def show_rpa_run(run_id: str) -> str:
    """🟢 看單一 RPA run 的詳細 log（每步 decision + result）。"""
    if not run_id or not run_id.startswith("rpa_"):
        return "❌ run_id 必須以 'rpa_' 開頭"
    # 防 path traversal
    if "/" in run_id or ".." in run_id:
        return "❌ run_id 含非法字元"
    path = os.path.join(_RUN_LOG_DIR, f"{run_id}.jsonl")
    if not os.path.isfile(path):
        return f"❌ 找不到 run {run_id}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = [json.loads(ln) for ln in f if ln.strip()]
    except Exception as e:
        return f"❌ 讀 log 失敗：{e}"
    if not entries:
        return "  （log 為空）"
    out = [f"🤖 RPA run: {run_id}"]
    out.append("─" * 60)
    out.append(f"task: {entries[0].get('task', '')}")
    out.append(f"started: {entries[0].get('at', '')}")
    out.append(f"max_steps: {entries[0].get('max_steps', '?')}")
    out.append("")
    out.append("Steps:")
    for e in entries[1:]:
        kind = e.get("kind", "?")
        if kind == "step":
            d = e.get("decision", {})
            r = e.get("result", {})
            mark = "✅" if r.get("ok") else "⚠️"
            out.append(f"  {mark} step {e.get('step')}: {d.get('action')}({d.get('target', '')[:40]})"
                       + (f" = '{d.get('value', '')[:30]}'" if d.get('value') else ""))
            if d.get("reason"):
                out.append(f"      reason: {d['reason'][:80]}")
        elif kind in ("finish", "abort"):
            icon = "✅" if kind == "finish" else "🛑"
            out.append(f"  {icon} {kind} @ step {e.get('step', '?')}: {e.get('reason', '')[:100]}")
    return "\n".join(out)
