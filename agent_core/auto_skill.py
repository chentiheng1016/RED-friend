"""Auto-skill：大王錄一段螢幕影片 → Gemini 看 → 自動產出 skill 檔。

跟 erp.learn_erp_from_video 不同之處：
  - 那個產生 JSON 工作流程（erp_workflows/*.json），只能被 erp_executor 跑
  - 這個產生真正的 Python skill 檔（skills/learned_*.py.draft），可以
    透過 reload_skills 變成小紅能直接呼叫的 tool

安全：
  - 產出的檔案副檔名故意是 .py.draft（不是 .py），loader 不會吃到
  - 大王要自己先審過 code，改名才會生效
  - 避免 Gemini 亂生 rm -rf / 之類危險操作直接被 agent 執行

使用流程（大王自己的工作流程）：
  1. quicktime / screencap 錄螢幕做某件事（例如「幫我在公司 ERP 填這張表」）
  2. 告訴小紅：「用 learn_skill_from_video 把這段影片轉成 skill，叫 erp_form_fill」
  3. 小紅：
      - 上傳影片到 Gemini
      - 請 Gemini 分析步驟並產 Python skill code
      - 寫 skills/learned_erp_form_fill.py.draft
  4. 大王檢查 .draft 內容 OK → mv 成 .py → 對小紅說「重新載入 skill」
  5. 以後大王說「幫我填 ERP 表」→ 新 skill 自動跑
"""
from __future__ import annotations

import ast
import json
import os
import re
from datetime import datetime

from agent_core.gemini_client import _gemini_generate, GEMINI_MODEL, _get_gemini_client, _wait_for_file_ready, upload_file
from agent_core.logging_and_paths import logger, _SCRIPT_DIR


_SKILLS_DIR = os.path.join(_SCRIPT_DIR, "skills")
_LEARNED_PREFIX = "learned_"


# 小紅手上有的 GUI automation building-block tool（給 Gemini 挑）
_AVAILABLE_TOOLS_HINT = """可用的內建 tool（生 skill 時優先用這些）：
  - click_screen(x: int, y: int)             # 在指定座標點擊
  - type_text(text: str)                     # 鍵盤輸入一段字
  - press_keys(keys: str)                    # 按組合鍵，例如 "cmd+a"、"return"、"tab"
  - scroll_screen(direction: str, clicks: int = 3)  # "up"/"down"/"left"/"right"
  - open_application(name: str)              # 開 App（跨平台）
  - close_application(name: str)
  - open_url(url: str)                       # 預設瀏覽器開網址
  - read_mac_clipboard()                     # 讀剪貼簿內容
  - set_system_volume(percent: int)
  - show_notification(title: str, message: str)
  - run_shell(cmd: str)                      # 短指令（30s 上限）

也可 `time.sleep(seconds)` 等畫面載入。

🚫 不要用：
  - 任何帶 sudo / rm -rf / >>/etc/ 的 shell
  - os.system / subprocess.Popen（用 run_shell 包）
  - 未 import 的第三方 lib（除非明確說明要加）
"""


_SKILL_GEN_PROMPT = """你是 macOS GUI automation 專家。

看完這段螢幕錄影後，產出一個**可直接執行的 Python skill 檔**。

任務名稱：__TASK_NAME__
使用者補充說明：__DESCRIPTION__

要求：
1. 檔案結構：
   ```python
   \"\"\"docstring：這個 skill 做什麼\"\"\"

   import time
   from agent_core.input_devices import click_screen, type_text, press_keys, scroll_screen
   from agent_core.apps import open_application, open_url
   # 視需要 import 其他 agent_core 模組

   def TASK_FN_NAME(param1: str, param2: str = "default"):
       \"\"\"清楚 docstring — Gemini 會用這個判斷何時呼叫。
       Args:
           param1: 說明
           param2: 說明
       \"\"\"
       # 步驟實作
       ...
       return "完成結果摘要"

   SKILL_TOOLS = [TASK_FN_NAME]
   ```

2. 參數化規則：
   - 影片中**每次都會變**的值（URL、表單欄位的輸入字串、檔名等）→ 變成函式參數
   - 固定操作（點某個按鈕的位置、固定的等待）→ 寫死在函式裡
   - 函式名用 snake_case，貼近任務（如 `fill_order_form`、`export_monthly_report`）

3. 每個點擊／輸入之間加合理的 `time.sleep`（例如 0.5s 到 2s），讓畫面跟得上。

4. 如果影片有打字，優先用 `type_text(...)`；快捷鍵用 `press_keys("cmd+s")`；
   滑鼠座標從影片判讀（螢幕解析度當做標準座標）。

5. 最後 return 一段可讀的字串說完成了什麼。

__TOOLS_HINT__

只輸出一份完整 Python 檔（不用 markdown fence、不解釋、不要前言）。直接從 docstring 開始。
"""


def _extract_python_code(text: str) -> str:
    """Gemini 回應中挑出 Python code。支援 markdown fence 也支援裸 code。"""
    text = text.strip()
    m = re.search(r"```(?:python)?\s*\n(.+?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text  # 假設就是純 code


def _validate_skill_code(code: str) -> str | None:
    """檢查 code：1) 語法合法 2) 有 SKILL_TOOLS 3) 沒明顯危險操作。
    回 None 表示 OK；回字串表示 error message。"""
    try:
        ast.parse(code)
    except SyntaxError as e:
        return f"語法錯誤：{e}"
    if "SKILL_TOOLS" not in code:
        return "缺 SKILL_TOOLS 宣告"
    # 檢危險字串
    bad_patterns = [
        ("rm -rf", "含 rm -rf"),
        ("sudo ", "含 sudo"),
        ("os.system(", "用 os.system（應該改 run_shell）"),
        ("subprocess.Popen", "用 subprocess.Popen（應該改 run_shell）"),
        ("__import__(", "用 __import__（靜態分析不到的 import）"),
        ("eval(", "用 eval"),
        ("exec(", "用 exec"),
    ]
    for pat, reason in bad_patterns:
        if pat in code:
            return f"code 含危險字串：{reason}"
    return None


def _sanitize_task_name(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", (name or "").strip())
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe or "unnamed"


def learn_skill_from_video(video_path: str, skill_name: str,
                          description: str = "") -> str:
    """看一段螢幕錄影，用 Gemini 產生一個可重複使用的 skill 檔草稿。

    產出的檔副檔名是 **.py.draft**（故意的安全設計），大王檢查過內容 OK 後
    手動改成 .py 再呼叫 reload_skills 才會生效。

    Args:
        video_path: 影片本機路徑（.mov / .mp4 / .m4v 等）。最大 2GB，最長 1 小時。
        skill_name: 要存成 skills/learned_<skill_name>.py.draft；snake_case 最好。
        description: 可選，對這個 skill 的補充說明（例如「幫我在 ERP 下採購單」）。
                     會放進 docstring 裡、也會幫助 Gemini 了解意圖。

    Returns:
        產出的 draft 檔案路徑、驗證結果、預覽前 30 行；以及啟用指令。
    """
    if not os.path.isfile(video_path):
        return f"❌ 找不到影片：{video_path}"

    safe_name = _sanitize_task_name(skill_name)
    draft_path = os.path.join(_SKILLS_DIR, f"{_LEARNED_PREFIX}{safe_name}.py.draft")

    size_mb = os.path.getsize(video_path) / 1024 / 1024
    if size_mb > 2000:
        return f"❌ 影片太大 ({size_mb:.0f} MB > 2 GB 上限)"

    # ---- 上傳 Gemini + 分析 ----
    client = _get_gemini_client()
    uploaded = None
    try:
        print(f"[auto-skill] 上傳影片（{size_mb:.0f} MB）到 Gemini...")
        uploaded = upload_file(client, video_path)
        uploaded = _wait_for_file_ready(uploaded)
        print("[auto-skill] 分析中...")

        prompt = (_SKILL_GEN_PROMPT
                  .replace("__TASK_NAME__", safe_name)
                  .replace("__DESCRIPTION__", (description or "（未提供）")[:500])
                  .replace("__TOOLS_HINT__", _AVAILABLE_TOOLS_HINT))
        resp = _gemini_generate(model=GEMINI_MODEL, contents=[uploaded, prompt])
        code = _extract_python_code((resp.text or "").strip())
    except Exception as e:
        return f"❌ Gemini 分析失敗：{type(e).__name__}: {e}"
    finally:
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass

    if not code or len(code) < 100:
        return f"❌ Gemini 產的 code 太短（{len(code)} 字）：{code[:300]}"

    # ---- 靜態驗證 ----
    err = _validate_skill_code(code)
    if err:
        # 還是把 draft 寫下來給大王看（他可以手動修）
        os.makedirs(_SKILLS_DIR, exist_ok=True)
        ts = datetime.now().isoformat(timespec="seconds")
        nl = "\n"
        header = (
            '"""⚠️ 這個 draft 驗證不通過：' + err + nl +
            '請人工修正後再改成 .py。' + nl +
            '原始 prompt: ' + safe_name + nl +
            '生成時間: ' + ts + nl +
            '"""' + nl + nl
        )
        with open(draft_path, "w", encoding="utf-8") as f:
            f.write(header + code)
        return (f"⚠️ Gemini 產出的 code 驗證失敗：{err}\n"
                f"   未經驗證的 draft 仍寫到：{draft_path}（手改後再啟用）")

    # ---- 寫 draft ----
    os.makedirs(_SKILLS_DIR, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    desc_safe = description or "（未提供）"
    nl = "\n"
    banner = (
        '"""自動學習的 skill — 生成時間 ' + ts + nl +
        '原始任務：' + safe_name + nl +
        '補充說明：' + desc_safe + nl + nl +
        '⚠️ 這是 .py.draft — skill loader 不會吃到。' + nl +
        '大王檢查過內容 OK 後：' + nl +
        '  1. mv ' + draft_path + ' ' + draft_path[:-6] + '   # 拿掉 .draft' + nl +
        '  2. 對小紅說「重新載入 skill」' + nl +
        '"""' + nl + nl
    )
    # 如果 Gemini 的 code 裡已經有 """docstring"""（大多數情況），我們的 banner 重複了，
    # 乾脆只保留原 code，不加 banner（但加個註解）。
    if code.lstrip().startswith('"""'):
        final_code = (
            "# 自動學習的 skill — 生成時間 " + ts + nl +
            "# 原始任務：" + safe_name + nl +
            "# .py.draft：檢查過後 mv 掉 .draft 並 reload_skills 才會生效" + nl + nl +
            code
        )
    else:
        final_code = banner + code

    try:
        with open(draft_path, "w", encoding="utf-8") as f:
            f.write(final_code)
    except Exception as e:
        return f"❌ 寫 draft 失敗：{e}"

    preview = "\n".join(final_code.splitlines()[:30])
    return (
        f"✅ 自動學習完成！\n"
        f"   draft 檔：{draft_path}\n"
        f"   大小：{os.path.getsize(draft_path):,} bytes\n\n"
        f"--- 前 30 行預覽 ---\n"
        f"{preview}\n"
        f"...\n\n"
        f"🛡️ 安全設計：副檔名 .py.draft，loader 不會吃到。\n"
        f"   大王確認內容無誤後：\n"
        f"     mv \"{draft_path}\" \"{draft_path[:-6]}\"\n"
        f"   再對小紅說「重新載入 skill」就會生效。"
    )


def list_learned_skills() -> str:
    """列 skills/learned_*.py 跟 .py.draft，看有哪些自動學習的 skill 已啟用 / 待審。"""
    if not os.path.isdir(_SKILLS_DIR):
        return "skills 資料夾不存在"
    entries = []
    for fn in sorted(os.listdir(_SKILLS_DIR)):
        if not fn.startswith(_LEARNED_PREFIX):
            continue
        path = os.path.join(_SKILLS_DIR, fn)
        size = os.path.getsize(path)
        mtime = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
        if fn.endswith(".py"):
            entries.append(f"  ✅ {fn}  ({size:,} bytes, {mtime})  — 已啟用")
        elif fn.endswith(".py.draft"):
            entries.append(f"  ⚠️ {fn}  ({size:,} bytes, {mtime})  — 待審核")
    if not entries:
        return "尚未有任何 learned_* skill（沒用過 learn_skill_from_video 或還沒產出過）"
    return "📚 自動學習的 skills：\n" + "\n".join(entries)
