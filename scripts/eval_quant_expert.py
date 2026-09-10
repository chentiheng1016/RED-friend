#!/usr/bin/env python3
"""小紅量化/經濟專家行為 live-eval — 問規格裡的經典題、啟發式檢查答案。

⚠️ 這會打 **LIVE Gemini**（花配額、非決定性），所以刻意**不進 unittest 套件**。
它是給人手動 spot-check 用的：抓 persona/模型改版後的行為漂移，以及確認部署後
小紅「真的」會像專家一樣推理（這是離線單元測試驗不到的那一塊）。

⚠️ 啟發式檢查靠關鍵字命中，LLM 用詞會變 —— ⚠️ 不代表答錯，請人工複看該題答案。

用法（用前台同款模型 flash，最貼近大王實際體驗）：
  AGENT_DAEMON_MODE=1 RED_GEMINI_MODEL=gemini-flash-latest \\
      .venv/bin/python scripts/eval_quant_expert.py
  ... scripts/eval_quant_expert.py --mode quant          # 量化深度模式
  ... scripts/eval_quant_expert.py --only break_even,pvalue
"""
import argparse
import datetime
import os
import sys

# 讓 `import agent` 找得到 repo 根（script 在 scripts/ 底下）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 規格裡的經典題 + 啟發式檢查。
#   check = (說明, kind, needles)
#     kind "any"  → 答案含任一 needle 即 ✅（該答的有答）
#     kind "none" → 答案不含任何 needle 才 ✅（不該說的沒說）
CASES = [
    {
        "id": "calculus_optimization",
        "q": "某產品利潤函數 P(q) = -2q^2 + 100q - 300（q 為產量）。求利潤最大的 q，並說明為何是極大值。",
        "checks": [
            ("答出 q = 25", "any", ["25"]),
            ("檢查二階/凹性確認極大", "any", ["二階", "P''", 'P"', "< 0", "<0", "極大", "凹", "concav", "maximum", "second"]),
        ],
    },
    {
        "id": "pvalue",
        "q": "請解釋 p 值 0.03 是什麼意思。",
        "checks": [
            # 只用正面框架檢查。「沒誤述成虛無為真的機率」這種反面 substring 檢查不可靠——
            # 好答案常會「明講 p 值不是虛無為真的機率」來破除迷思，反而命中禁詞 → 假陽性。
            ("正確框架：若虛無為真、看到至少這麼極端的機率", "any",
             ["至少這麼極端", "一樣極端", "更極端", "更誇張", "若虛無", "假設虛無", "虛無假設為真",
              "根本沒效果", "沒有效果", "沒差", "前提下", "as extreme", "null is true", "null hypothesis"]),
        ],
    },
    {
        "id": "elasticity",
        "q": "純粹從經濟學原理（不用查我的實際數據）：把鞋子售價調高，公司總營收會變多還是變少？請說明這取決於什麼。",
        "checks": [
            ("用需求彈性回答", "any", ["彈性", "elastic"]),
            ("分情況而非一口斷定", "any", ["缺乏彈性", "有彈性", "inelastic", "取決", "要看", "視", "如果需求"]),
        ],
    },
    {
        "id": "break_even",
        "q": "固定成本 80000 元、單價 120 元、單位變動成本 75 元，損益兩平要賣幾雙鞋？",
        "checks": [
            ("貢獻邊際 = 45", "any", ["45"]),
            ("損益兩平 ≈ 1778 雙", "any", ["1778", "1,778", "1777", "1,777"]),
        ],
    },
    {
        "id": "correlation_causation",
        "q": "我發現廣告花費和銷量高度正相關，是不是代表多打廣告一定會讓銷量上升？",
        "checks": [
            ("點出相關 ≠ 因果", "any",
             ["相關不等於因果", "相關 ≠ 因果", "相關≠因果", "不代表因果", "不等於因果", "未必", "不一定", "不能據此推論因果", "反向"]),
        ],
    },
    {
        "id": "missing_assumptions",
        "q": "先不用查我的實際數據——純粹從經濟學與成本結構來看，『降價衝銷量』要在什麼條件下才划算？你會需要先知道哪些資訊？",
        "checks": [
            ("先問關鍵變數而非直接 yes/no", "any",
             ["彈性", "貢獻邊際", "邊際成本", "競品", "競爭", "產能", "現金流", "缺", "取決", "要看", "假設", "目標"]),
        ],
    },
]


def _load_quant_tools():
    """Load skills/quant_tools.py by path (skills/ isn't an importable package)."""
    import importlib.util
    from agent_core import path_safety
    p = os.path.join(path_safety._REPO_ROOT, "skills", "quant_tools.py")
    spec = importlib.util.spec_from_file_location("quant_tools_eval", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return list(m.SKILL_TOOLS)


def build_eval_config(mode):
    """Assemble (tools, persona) for the eval.

    Deliberately does NOT `import agent` — that boots Gmail/Drive/Calendar/chroma
    (slow + flaky for a quick eval). The quant questions only need the quant
    calculators + run_python_code, so we wire just those onto the real persona.
    A fresh chat is built per question (see `ask`) so questions stay independent.
    """
    # Match production: install the AFC unknown-tool guard so that if 小紅 reaches
    # for a tool this minimal eval didn't wire (her persona references many Drive /
    # inbox tools), the SDK returns an error stub instead of crashing with KeyError.
    try:
        from agent_core.genai_afc_guard import install_afc_unknown_tool_guard
        install_afc_unknown_tool_guard()
    except Exception:  # noqa: BLE001
        pass
    from agent_core.persona import build_persona_text
    persona = build_persona_text("（live-eval：本次無啟動記憶）")
    if mode and mode != "normal":
        from agent_core.persona_profiles import persona_for
        persona = persona + persona_for(mode)
    tools = _load_quant_tools()
    try:
        from agent_core.shell_python_web import run_python_code
        tools.append(run_python_code)
    except Exception:  # noqa: BLE001 — run_python_code is optional for the eval
        pass
    return tools, persona


_TRANSIENT = ("503", "504", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "429", "high demand")


def ask(config, question, retries=3):
    """Build a fresh chat for this question and send it; retry transient 5xx/429."""
    import time
    from agent_core.persona import build_chat
    tools, persona = config
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = f"[系統時間：{ts}] {question}"
    for attempt in range(retries):
        try:
            resp = build_chat(tools, persona).send_message(msg)
            return (getattr(resp, "text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1 or not any(t in str(e) for t in _TRANSIENT):
                raise
            time.sleep(1.5 * (attempt + 1))  # 1.5s, 3s — ride out brief 503 spikes
    return ""  # unreachable


def grade(answer, checks):
    rows = []
    for label, kind, needles in checks:
        hit = any(n in answer for n in needles)
        rows.append((label, hit if kind == "any" else not hit))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="normal", help="normal | quant | sales ...")
    ap.add_argument("--only", default="", help="逗號分隔 case id 子集")
    args = ap.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    cases = [c for c in CASES if not only or c["id"] in only]

    model = os.environ.get("RED_GEMINI_MODEL", "(預設)")
    print(f"== 小紅量化專家 live-eval ｜ mode={args.mode} ｜ model={model} ｜ {len(cases)} 題 ==\n")
    try:
        config = build_eval_config(args.mode)
    except Exception as e:  # noqa: BLE001 — manual tool, surface any startup failure
        print(f"❌ 無法組裝 persona/tools：{type(e).__name__}: {e}")
        return 2

    total = passed = 0
    flagged = []
    for c in cases:
        print(f"── [{c['id']}] {c['q']}")
        try:
            ans = ask(config, c["q"])
        except Exception as e:  # noqa: BLE001
            print(f"   ❌ 查詢失敗：{type(e).__name__}: {e}\n")
            flagged.append(c["id"])
            continue
        if not ans:
            print("   ⚠️  （空回應 — 可能 tool call 未收斂）")
            flagged.append(c["id"])
        rows = grade(ans, c["checks"])
        for label, ok in rows:
            total += 1
            passed += 1 if ok else 0
            print(f"   {'✅' if ok else '⚠️ '} {label}")
        if not all(ok for _, ok in rows):
            flagged.append(c["id"])
        excerpt = " ".join(ans.split())
        print(f"   答：{excerpt[:300] + ('…' if len(excerpt) > 300 else '')}\n")

    print(f"== 啟發式檢查通過 {passed}/{total} ｜ 需人工複看：{', '.join(dict.fromkeys(flagged)) or '無'} ==")
    print("（⚠️ = 關鍵字沒命中，不一定是錯，請人工確認該題答案。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
