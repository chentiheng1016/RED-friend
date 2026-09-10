"""ERP workflow executor via Gemini Computer Use + Playwright.

Loads a structured workflow from erp_workflows/<name>.json and drives
a Chromium browser through it. The Gemini 2.5 Computer Use preview
model decides each action from screenshots; Playwright executes the
chosen action (click / type / scroll / key press) at real pixel coords.

Safety rails:
- `ERP_EXEC_ALLOW_WRITE=1` env var required for write-verb actions
  (送出/確認/儲存/刪除/新增/submit/save/delete/remove). Missing →
  the action is refused and the loop asks for confirmation.
- `max_steps` hard cap on the action loop (defaults 30).
- Headful browser so you can watch what it's doing and CTRL+C.
- `trace_dir` (optional) saves per-step screenshots for postmortem.

This module is NOT in tool_registry.BUILTIN_TOOLS yet — run it as a
standalone script first to validate:

    .venv/bin/python agent_core/erp_executor.py <workflow_name> <erp_url>

Once proven on real workflows, wire into BUILTIN_TOOLS as
`execute_erp_workflow(workflow_name, erp_base_url)` so Gemini chat can
invoke it by name.
"""
import base64
import json
import os
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright
from google.genai import types

from agent_core.gemini_client import generate_content_tracked
from agent_core.logging_and_paths import _SCRIPT_DIR

MODEL = "gemini-2.5-computer-use-preview-10-2025"
WORKFLOW_DIR = os.path.join(_SCRIPT_DIR, "erp_workflows")
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
SETTLE_MS = 500  # wait after each action before next screenshot

WRITE_VERBS = [
    "送出", "確認送出", "儲存", "存檔", "刪除", "新增", "建立", "Submit",
    "submit", "save", "delete", "remove", "insert", "create",
]


def _is_write_action(action_name: str, args: dict) -> bool:
    """Heuristic: does this action likely commit data?"""
    name_l = action_name.lower()
    if any(v in name_l for v in ("submit", "save", "delete", "create", "insert")):
        return True
    text = str(args.get("text", "") or args.get("value", "") or "")
    return any(v in text for v in WRITE_VERBS)


def _execute_action(page, action_name: str, args: dict, allow_write: bool) -> dict:
    """Translate a Gemini Computer Use function call into a Playwright op.

    Returns a dict describing the outcome (for the function_response back
    to Gemini).
    """
    if _is_write_action(action_name, args) and not allow_write:
        return {
            "status": "refused",
            "reason": "write action blocked (ERP_EXEC_ALLOW_WRITE != 1)",
            "action": action_name,
            "args": args,
        }

    try:
        if action_name in ("click_at", "click"):
            page.mouse.click(int(args["x"]), int(args["y"]))
        elif action_name in ("double_click_at", "double_click"):
            page.mouse.dblclick(int(args["x"]), int(args["y"]))
        elif action_name == "hover_at":
            page.mouse.move(int(args["x"]), int(args["y"]))
        elif action_name in ("type_text_at",):
            page.mouse.click(int(args["x"]), int(args["y"]))
            page.keyboard.type(str(args.get("text", "")))
        elif action_name in ("type_text",):
            page.keyboard.type(str(args.get("text", "")))
        elif action_name in ("key_press", "press_key"):
            page.keyboard.press(str(args["key"]))
        elif action_name in ("key_combination",):
            keys = args.get("keys") or args.get("key")
            page.keyboard.press(str(keys))
        elif action_name in ("scroll_document", "scroll"):
            dy = int(args.get("amount", 300))
            direction = args.get("direction", "down")
            if direction == "up":
                dy = -dy
            page.mouse.wheel(0, dy)
        elif action_name == "scroll_at":
            page.mouse.move(int(args["x"]), int(args["y"]))
            page.mouse.wheel(0, int(args.get("amount", 300)))
        elif action_name in ("wait_5_seconds", "wait"):
            ms = int(args.get("ms", 5000))
            page.wait_for_timeout(ms)
        elif action_name in ("navigate", "open_web_browser"):
            page.goto(str(args["url"]), wait_until="networkidle", timeout=30000)
        elif action_name == "go_back":
            page.go_back()
        elif action_name == "go_forward":
            page.go_forward()
        elif action_name == "drag_and_drop":
            page.mouse.move(int(args["x1"]), int(args["y1"]))
            page.mouse.down()
            page.mouse.move(int(args["x2"]), int(args["y2"]))
            page.mouse.up()
        else:
            return {"status": "unknown_action", "action": action_name, "args": args}
    except Exception as e:
        return {
            "status": "error",
            "action": action_name,
            "args": args,
            "error": f"{type(e).__name__}: {e}",
        }

    return {"status": "ok", "action": action_name, "args": args}


def _screenshot_part(page) -> types.Part:
    return types.Part.from_bytes(data=page.screenshot(), mime_type="image/png")


def _save_trace(trace_dir: str, step: int, label: str, page) -> None:
    if not trace_dir:
        return
    os.makedirs(trace_dir, exist_ok=True)
    path = os.path.join(trace_dir, f"step_{step:03d}_{label}.png")
    page.screenshot(path=path)


def _build_system_instruction(wf: dict, allow_write: bool) -> str:
    policy = (
        "允許執行寫入類動作（送出/儲存/新增/刪除）"
        if allow_write
        else "⚠️ 禁止執行寫入類動作——遇到送出/儲存/新增/刪除按鈕時，用純文字回覆「需要大王確認：<描述>」並停止"
    )
    return (
        f"你是操作 {wf.get('erp_name', 'ERP 系統')} 的自動化助手。\n"
        f"\n"
        f"任務：{wf.get('task_name', '執行工作流程')}\n"
        f"前置條件：{wf.get('prerequisites', '(未提供)')}\n"
        f"目標選單路徑：{wf.get('menu_path', '(未提供)')}\n"
        f"\n"
        f"完整步驟清單（從目前畫面出發，依序執行）：\n"
        f"{json.dumps(wf.get('steps', []), ensure_ascii=False, indent=2)}\n"
        f"\n"
        f"執行規則：\n"
        f"1. 每輪先觀察 screenshot 判斷目前在哪一步、頁面是否載入完畢\n"
        f"2. 每次呼叫一個 Computer Use action（click_at / type_text_at / key_press 等）\n"
        f"3. action 之間等待頁面反應（必要時用 wait_5_seconds）\n"
        f"4. {policy}\n"
        f"5. 某步失敗（元素找不到、畫面異常）→ 文字回報並停止，不要亂試\n"
        f"6. 全部步驟完成 → 文字回覆「✅ 工作流程完成」\n"
    )


def execute_erp_workflow(
    workflow_name: str,
    erp_base_url: str,
    max_steps: int = 30,
    headless: bool = False,
    trace: bool = True,
) -> str:
    """Drive a browser through erp_workflows/<name>.json via Gemini Computer Use.

    Args:
        workflow_name: stem of the JSON file under erp_workflows/
        erp_base_url: starting URL (e.g. ERP login page)
        max_steps: hard cap on action loop iterations
        headless: if True, run Chromium without UI (not recommended for first runs)
        trace: if True, save per-step screenshots to logs/erp_trace_<ts>/

    Returns:
        Multi-line trace of what happened. Also prints live.
    """
    allow_write = os.environ.get("ERP_EXEC_ALLOW_WRITE", "0") == "1"

    wf_path = os.path.join(WORKFLOW_DIR, f"{workflow_name}.json")
    if not os.path.exists(wf_path):
        return f"❌ 找不到 workflow: {wf_path}"

    with open(wf_path, "r", encoding="utf-8") as f:
        wf = json.load(f)

    trace_dir = ""
    if trace:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        trace_dir = os.path.join(_SCRIPT_DIR, "logs", f"erp_trace_{workflow_name}_{ts}")

    tool = types.Tool(computer_use=types.ComputerUse(environment="ENVIRONMENT_BROWSER"))
    cfg = types.GenerateContentConfig(
        tools=[tool],
        system_instruction=_build_system_instruction(wf, allow_write),
    )

    log_lines = [
        f"[erp_executor] workflow={workflow_name}  url={erp_base_url}",
        f"[erp_executor] allow_write={allow_write}  max_steps={max_steps}",
        f"[erp_executor] trace_dir={trace_dir or '(disabled)'}",
    ]
    for line in log_lines:
        print(line)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(viewport=DEFAULT_VIEWPORT)
        page = context.new_page()
        print(f"[erp_executor] 開啟 {erp_base_url} …")
        try:
            page.goto(erp_base_url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            log_lines.append(f"❌ 初始導航失敗：{e}")
            browser.close()
            return "\n".join(log_lines)

        _save_trace(trace_dir, 0, "start", page)

        history = [
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(text="請開始執行上面系統指令裡的 workflow。現在的畫面見附圖。"),
                    _screenshot_part(page),
                ],
            ),
        ]

        for step in range(1, max_steps + 1):
            try:
                resp = generate_content_tracked(
                    model=MODEL,
                    contents=history,
                    config=cfg,
                    caller="erp_executor.execute_erp_workflow",
                )
            except Exception as e:
                log_lines.append(f"[step {step}] ❌ Gemini 呼叫失敗：{type(e).__name__}: {e}")
                break

            cand = resp.candidates[0] if resp.candidates else None
            if cand is None:
                log_lines.append(f"[step {step}] Gemini 無 candidate，結束")
                break

            parts = list(cand.content.parts or [])
            text_chunks = [p.text for p in parts if getattr(p, "text", None)]
            function_calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

            if text_chunks:
                joined = "\n".join(text_chunks).strip()
                log_lines.append(f"[step {step}] 🤖 {joined}")
                print(log_lines[-1])
                if any(kw in joined for kw in ("✅", "完成", "需要大王確認", "失敗", "無法繼續")):
                    break

            if not function_calls:
                log_lines.append(f"[step {step}] 無 function_call 且無終止訊號，結束")
                break

            history.append(types.Content(role="model", parts=parts))

            fc_responses = []
            any_refused = False
            for fc in function_calls:
                args = dict(fc.args or {})
                outcome = _execute_action(page, fc.name, args, allow_write)
                if isinstance(outcome, dict) and outcome.get("status") == "refused":
                    any_refused = True
                log_lines.append(f"[step {step}] 🔧 {fc.name}({args}) → {outcome}")
                print(log_lines[-1])
                page.wait_for_timeout(SETTLE_MS)
                _save_trace(trace_dir, step, fc.name, page)

                fc_responses.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response={"outcome": outcome},
                    )
                )

            # Feed back: function responses + fresh screenshot for next turn
            fc_responses.append(_screenshot_part(page))
            history.append(types.Content(role="user", parts=fc_responses))

            # Early exit if any write was refused — surfaces to user cleanly
            # （旗標在上面迴圈內由 outcome.status 設；fc_responses 已是 Part 無法 introspect）
            if any_refused:
                break

        log_lines.append(f"[erp_executor] 迴圈結束（step={step}/{max_steps}）")
        _save_trace(trace_dir, 999, "final", page)
        browser.close()

    return "\n".join(log_lines)


def _main():
    if len(sys.argv) < 3:
        print("usage: python agent_core/erp_executor.py <workflow_name> <erp_url> [max_steps]")
        sys.exit(1)
    wf = sys.argv[1]
    url = sys.argv[2]
    max_steps = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    out = execute_erp_workflow(wf, url, max_steps=max_steps)
    print("\n" + "=" * 60)
    print(out)


if __name__ == "__main__":
    _main()
