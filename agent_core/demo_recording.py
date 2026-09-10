"""Demo recording → auto-generated skill.

Uses Playwright codegen
to capture a user's browser interaction, then asks Gemini to refactor the
raw recording into a parameterized, reusable skill that's written to
skills/recorded_<name>.py.

reload_skills still lives in agent.py (it mutates agent's tools_list +
_chat_state). finalize_demo_recording and delete_recorded_demo lazy-
import it inside the function body to avoid load-time cycles.
"""
import os
import re
import sys
import json
import subprocess
from datetime import datetime

from agent_core.browser import _BrowserSession
from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate
from agent_core.logging_and_paths import logger, _SCRIPT_DIR
from agent_core.skills import _SKILLS_DIR

_RECORDING_DIR = os.path.join(_SCRIPT_DIR, "recordings")
_RECORDED_SKILL_PREFIX = "recorded_"


def _ensure_recording_dir():
    os.makedirs(_RECORDING_DIR, exist_ok=True)


def _sanitize_task_name(name: str) -> str:
    return re.sub(r"[^\w\-\.]", "_", (name or "").strip())[:80]


def _recording_state_path(task_name: str) -> str:
    return os.path.join(_RECORDING_DIR, f"{task_name}.meta.json")


def _recording_raw_py_path(task_name: str) -> str:
    return os.path.join(_RECORDING_DIR, f"{task_name}_raw.py")


def _recording_storage_path(task_name: str) -> str:
    return os.path.join(_RECORDING_DIR, f"{task_name}_storage.json")


def start_demo_recording(task_name: str, start_url: str = ""):
    """開始錄製大王的操作示範。
    - task_name：任務名稱（例如「建樣品單」、「ERP 查庫存」）
    - start_url：要錄的網站起始 URL（例如 ERP 登入頁；可留空從空白頁開始）

    用 Playwright codegen 啟動一個獨立 Chromium。大王在視窗裡
    **實際操作一次**要自動化的流程，每個點擊、填寫都會被**精準記錄**（含 CSS selector）。
    做完**關閉 Chromium 視窗**、跟小紅說「完成錄製 <task_name>」即可。"""
    task = _sanitize_task_name(task_name)
    if not task:
        return "錯誤：task_name 不能空、不能只有特殊字元"
    _ensure_recording_dir()

    try:
        _BrowserSession.close()
    except Exception as _e:
        logger.debug("關 main browser 失敗（應無礙）：%s", _e)

    meta_path = _recording_state_path(task)
    raw_py = _recording_raw_py_path(task)
    storage = _recording_storage_path(task)

    if os.path.exists(raw_py):
        try:
            os.remove(raw_py)
        except Exception:
            logger.debug("silent ignore in broad except")

    cmd = [
        sys.executable, "-m", "playwright", "codegen",
        "--target", "python",
        "-b", "chromium",
        "-o", raw_py,
        "--save-storage", storage,
    ]
    if os.path.exists(storage):
        cmd.extend(["--load-storage", storage])
    if start_url and start_url.strip():
        cmd.append(start_url.strip())

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return f"啟動 codegen 失敗：{type(e).__name__}: {e}"

    meta = {
        "task_name": task,
        "start_url": start_url.strip(),
        "pid": proc.pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "raw_py": raw_py,
        "storage": storage,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return (
        f"🎬 開始錄製「{task}」（PID={proc.pid}）\n\n"
        "現在請大王：\n"
        "  1. 等 Chromium 視窗跳出來（幾秒內）\n"
        "  2. 在瀏覽器裡**完整操作一次**要自動化的流程\n"
        "     （點擊、填表、送出都會被精準記錄含 CSS selector）\n"
        "  3. 做完之後**關閉 Chromium 視窗**\n"
        "  4. 跟小紅說「完成錄製 " + task + "」\n"
        "     → 小紅會讀取錄製內容、用 Gemini 參數化，自動產出可執行 skill\n\n"
        "錄製檔將存在：" + raw_py
    )


def _parameterize_with_gemini(raw_code: str, task_name: str) -> dict:
    """把 codegen 輸出的 Python 交給 Gemini 做參數化 + 清理，產出可重複使用的 skill code。
    回傳 {"skill_code": str, "function_name": str, "params": [...], "doc": str} 或 {"error": ...}"""
    prompt = (
        "以下是 Playwright codegen 從大王實際操作錄製產出的 Python 程式碼。\n"
        f"任務名稱：「{task_name}」\n\n"
        "請分析並把它改寫成一個**可重複使用的 Python 函式**，要求：\n\n"
        "1. **識別哪些 fill() 值是『資料參數』**（例如客戶代號、數量、料號這些每次都不同的值），\n"
        "   把它們提升成函式參數。哪些是**固定動作**（選單、按鈕點擊）保留原樣。\n"
        "2. **識別登入資訊**：如果有 login 步驟帶明顯的 username/password，把它們也變成參數 "
        "   但加 docstring 提醒不要硬編。\n"
        "3. **不要用 sync_playwright() 建立新 browser**，改成接收 page 參數（或從既有 session 拿）。\n"
        "   具體：函式第一行 `from agent import _BrowserSession` ，`page = _BrowserSession._ensure()`\n"
        "4. **函式名稱**用英數底線，對應 task_name 的邏輯名（例：erp_create_sample_order）\n"
        "5. **docstring** 用繁中說明每個參數與回傳值\n"
        "6. 最後加 `SKILL_TOOLS = [函式名]` 讓小紅的 skill loader 自動註冊\n\n"
        "回傳**嚴格 JSON 格式**（不要 markdown 圍欄）：\n"
        "{\n"
        '  "function_name": "...",\n'
        '  "params": ["customer", "qty", ...],\n'
        '  "doc": "函式功能一句話描述",\n'
        '  "skill_code": "完整的 .py 檔內容（含 docstring + 函式定義 + SKILL_TOOLS）"\n'
        "}\n\n"
        "原始錄製程式碼：\n```python\n"
        + raw_code + "\n```\n"
    )
    try:
        resp = _gemini_generate(model=GEMINI_MODEL, contents=[prompt])
        text = (resp.text or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {"error": "Gemini 沒回 JSON", "raw": text[:500]}
        d = json.loads(m.group(0))
        return d
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def finalize_demo_recording(task_name: str, parameterize: bool = True):
    """完成錄製：讀 codegen 輸出、用 Gemini 參數化、產出可執行 skill。
    - task_name：對應 start_demo_recording 的名稱
    - parameterize：True 則讓 Gemini 分析並參數化；False 則直接用原始錄製（單純重播）

    大王關閉 Chromium 視窗後呼叫此工具，小紅會：
      1. 讀取 codegen 寫的 Python 檔
      2. 用 Gemini 分析哪些是『每次變動的值』→ 提升成函式參數
      3. 產出一個 .py skill 檔到 skills/recorded_<name>.py
      4. reload_skills 讓新 skill 立即可用
    """
    # reload_skills 住在 agent_core.tool_registry（lazy-import 避免 import 環）。
    from agent_core.tool_registry import reload_skills

    task = _sanitize_task_name(task_name)
    meta_path = _recording_state_path(task)
    if not os.path.exists(meta_path):
        return f"找不到錄製 metadata：{meta_path}。先用 start_demo_recording 開始錄？"
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    raw_py = meta.get("raw_py", _recording_raw_py_path(task))
    if not os.path.exists(raw_py):
        return (f"找不到錄製檔：{raw_py}\n"
                "可能大王還沒關 Chromium 視窗（codegen 會在關視窗時才存檔）。")
    try:
        with open(raw_py, "r", encoding="utf-8") as f:
            raw_code = f.read()
    except Exception as e:
        return f"讀錄製檔失敗：{e}"

    if len(raw_code.strip()) < 100:
        return f"錄製檔太短（{len(raw_code)} 字），可能大王沒操作任何動作就關了視窗。"

    skill_path = os.path.join(_SKILLS_DIR, f"{_RECORDED_SKILL_PREFIX}{task}.py")

    if not parameterize:
        fn_name = f"run_{task}"
        wrapped = (
            f'"""錄製於 {meta.get("started_at", "?")}：{task}。\n'
            f'由大王示範錄製、未經 Gemini 參數化（直接重播）。"""\n\n'
            f'def {fn_name}():\n'
            f'    """直接重播大王的錄製操作（無參數）。"""\n'
            f'    from playwright.sync_api import sync_playwright\n'
            + "\n".join("    " + line for line in raw_code.splitlines() if line.strip())
            + f'\n\nSKILL_TOOLS = [{fn_name}]\n'
        )
        skill_code = wrapped
        fn_info = {"function_name": fn_name, "params": [], "doc": f"重播 {task}"}
    else:
        print(f"[錄製] 用 Gemini 參數化（分析 {len(raw_code)} 字元程式碼）...")
        result = _parameterize_with_gemini(raw_code, task)
        if "error" in result:
            return f"❌ Gemini 參數化失敗：{result['error']}\n原始錄製仍在 {raw_py}，可手動改。"
        skill_code = result.get("skill_code", "")
        if not skill_code or "def " not in skill_code or "SKILL_TOOLS" not in skill_code:
            return f"❌ Gemini 回的 skill_code 格式不對。raw JSON keys: {list(result.keys())}"
        fn_info = result

    try:
        import ast as _ast
        _ast.parse(skill_code)
    except SyntaxError as e:
        return f"❌ 產出的 skill 語法錯誤：{e}\n原始錄製仍在 {raw_py}，可手動改。"

    try:
        with open(skill_path, "w", encoding="utf-8") as f:
            f.write(skill_code)
    except Exception as e:
        return f"寫入 skill 檔失敗：{e}"

    try:
        reload_skills()
    except Exception as _e:
        logger.debug("reload_skills 失敗（不影響 skill 已存檔）：%s", _e)

    fn_name = fn_info.get("function_name", "?")
    params = fn_info.get("params", [])
    return (
        f"✅ 錄製完成並產出 skill！\n"
        f"   函式名：{fn_name}\n"
        f"   參數：{', '.join(params) if params else '（無）'}\n"
        f"   說明：{fn_info.get('doc', 'N/A')}\n"
        f"   檔案：{skill_path}\n"
        f"   已 reload_skills 生效，小紅現在可以直接呼叫 {fn_name}(...)\n\n"
        f"   原始錄製保留於：{raw_py}（之後要手動微調可以編輯）"
    )


def list_recorded_demos():
    """列出所有已錄製的示範。"""
    _ensure_recording_dir()
    metas = [f for f in os.listdir(_RECORDING_DIR) if f.endswith(".meta.json")]
    if not metas:
        return "目前沒有任何錄製示範。用 start_demo_recording('任務名') 開始錄一個。"
    lines = [f"共 {len(metas)} 份錄製："]
    for m in sorted(metas):
        path = os.path.join(_RECORDING_DIR, m)
        try:
            with open(path, "r", encoding="utf-8") as _f:
                d = json.load(_f)
            task = d.get("task_name", "?")
            when = d.get("started_at", "?")[:16]
            raw = d.get("raw_py", "")
            done = "✅" if os.path.exists(raw) else "⏳"
            size = os.path.getsize(raw) if os.path.exists(raw) else 0
            lines.append(f"  {done} {task}  錄於 {when}  原始檔 {size} bytes")
            skill = os.path.join(_SKILLS_DIR, f"{_RECORDED_SKILL_PREFIX}{task}.py")
            if os.path.exists(skill):
                lines.append(f"       → skill 已產出：{os.path.basename(skill)}")
        except Exception:
            lines.append(f"  ⚠️ {m} 讀取失敗")
    return "\n".join(lines)


def delete_recorded_demo(task_name: str):
    """刪除一個錄製示範（原始檔 + 產出的 skill 都清掉）。"""
    from agent_core.tool_registry import reload_skills
    task = _sanitize_task_name(task_name)
    removed = []
    for p in [_recording_state_path(task), _recording_raw_py_path(task),
              _recording_storage_path(task),
              os.path.join(_SKILLS_DIR, f"{_RECORDED_SKILL_PREFIX}{task}.py")]:
        if os.path.exists(p):
            try:
                os.remove(p)
                removed.append(os.path.basename(p))
            except Exception as _e:
                logger.debug("刪除 %s 失敗：%s", p, _e)
    if not removed:
        return f"找不到「{task}」相關檔案"
    try:
        reload_skills()
    except Exception:
        logger.debug("silent ignore in broad except")
    return f"✅ 已刪除「{task}」：{', '.join(removed)}"
