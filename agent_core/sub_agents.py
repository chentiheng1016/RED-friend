"""Sub-agent：讓小紅把複雜任務拆給專門子代理處理。

Why: 一個 Gemini chat 被塞太多步驟會發生：
  - Context token 爆炸（每次 call 帶整段對話歷史）
  - 混亂分心（中途的 tool result 會干擾最終判斷）
  - 無法並行（一次只能跑一個任務）

Solution: `delegate_to_sub_agent(goal, context, allow_tools)` 開一個獨立 Gemini
chat session 跑給定目標，完成後回報摘要給主代理，主代理再繼續。

T8 擴充：`delegate_to_sub_agents_parallel(goals, ...)` 同時派多個子代理並行
跑，等全部回來再合併結果。適合「對這 10 個客戶各做一次 X」這種扇出工作。

架構選擇：
  - 同一個 Python process，同一個 Gemini client（不開新 subprocess）
  - 新 chat session = 新 context window（fresh start，不看主代理對話史）
  - 工具預設繼承主代理全部 tools（可用 allow_tools 限縮）
  - 子代理的 persona 是任務導向、簡短（相對主代理的 persona 短很多）
  - 固定 max_turns 上限避免無限 loop
  - 子代理結果純文字回 → 主代理的 context 只增加一個 tool response
  - 並行用 ThreadPoolExecutor（Gemini SDK 是 thread-safe）

⚠️ 目前設計：**子代理不能再 delegate**（避免遞迴炸）。第 1 層就好。
⚠️ 並行時 Gemini rate limit 會綁住；max_parallel 預設 3 比較安全。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from agent_core.gemini_client import _gemini_generate, GEMINI_MODEL, _get_gemini_client
from agent_core.logging_and_paths import logger


SUB_AGENT_MAX_TURNS = 15
SUB_AGENT_MAX_GOAL_LEN = 2000
SUB_AGENT_MAX_CONTEXT_LEN = 20000
SUB_AGENT_PARALLEL_CAP = 8      # 硬上限，再多就不合理（Gemini rate limit）
SUB_AGENT_PARALLEL_DEFAULT = 3  # 預設 3 個並行，穩定不撞配額


def _build_sub_persona(goal: str, context: str) -> str:
    """子代理的 system instruction — 任務導向、少廢話。

    Round 8 L8-5：goal / context 可由主代理傳入 — 若主代理已被 prompt-injection
    污染（例如剛讀完含 injection 的 email），它傳的 context 會把 injection 直接
    放進 sub-agent 的 system_instruction。defense-in-depth：兩參數都 sanitize_for_llm。
    """
    try:
        from agent_core.prompt_injection import sanitize_for_llm
        goal = sanitize_for_llm(goal or "")
        context = sanitize_for_llm(context or "")
    except Exception:
        pass
    base = f"""你是「小紅」的子代理（sub-agent）。你只有這一個任務：

【目標】
{goal}

"""
    if context.strip():
        base += f"""【主代理給你的 context】
{context}

"""
    base += """【規則】
1. 專注完成目標，**不要閒聊、不要說明你要做什麼、直接做**。
2. 可以使用你手上的所有 tool。如果目標需要多步，自己規劃順序。
3. 遇到障礙先試 1-2 次排除（例如 tool 回錯誤 → 改參數再試），真的卡住就回報現況與障礙。
4. **完成時輸出最終結果**（純文字，可以是摘要、清單、檔案路徑等）。
5. **不要呼叫 delegate_to_sub_agent**（你就是子代理，不再遞迴）。
6. **不要講人話開頭**（「好的」「我來幫您」「以下是結果」）— 直接給內容。主代理會接手解讀。

開始。
"""
    return base


def _pick_tools_for_sub_agent(allow_tools: list[str] | None = None, exclude_self: bool = True):
    """從主代理 tools_list 挑給子代理的 tool subset。

    - allow_tools=None：全部（但剔除 delegate_to_sub_agent）
    - allow_tools=['foo','bar']：只有這些
    - exclude_self=True：永遠剔除 delegate_to_sub_agent 避免遞迴

    ⚠️ 安全（review round 5 找到的 bypass）：
      delegate_to_sub_agent 自身 IS sensitive，被 V4 確認門攔（M5 one-shot 用掉
      就 revoke）。但 sub-agent 起來後拉的 tools_list 是 **UNWRAPPED** —
      sub-agent 的 LLM 直接 call run_shell / send_gmail 等不會再過 V4 gate。
      於是攻擊鏈：`+確認 delegate_to_sub_agent goal="rm -rf ~"`
        → wrap_sensitive_tool 攔 delegate → 通過 → revoke
        → sub-agent 啟動，工具未包，run_shell 暢通。

    修法（**deny by default**）：sub-agent 預設**完全濾除 _SENSITIVE_TOOLS**。
      若主代理確實要 sub-agent 寄信、跑 shell 等，必須在 `allow_tools=[...]`
      明確列出。LLM 要這樣動就需要 deliberate intent，不會在攻擊時自動發生。
    """
    from agent_core.tool_registry import tools_list
    from agent_core.tg_auth import _SENSITIVE_TOOLS

    NEVER_IN_SUBAGENT = {"delegate_to_sub_agent", "delegate_to_sub_agents_parallel"}
    subset = []
    for t in tools_list:
        name = t.__name__
        if exclude_self and name in NEVER_IN_SUBAGENT:
            continue
        # explicit allow_tools 是 opt-in：列了就允許（含 sensitive）
        if allow_tools is not None:
            if name in allow_tools:
                subset.append(t)
            continue
        # 預設模式：sensitive 整批不給 sub-agent
        if name in _SENSITIVE_TOOLS:
            continue
        subset.append(t)
    return subset


def delegate_to_sub_agent(goal: str, context: str = "",
                          allow_tools: list[str] = None,
                          max_turns: int = SUB_AGENT_MAX_TURNS) -> str:
    """把一個明確目標交給子代理獨立完成，回傳子代理的最終結果。

    適用情境：
      - 任務需要超過 5 步驟（不然主代理 context 會爆）
      - 要跑時間長、不希望污染主對話的背景研究（例如「幫我把 10 年 email
        翻一遍找所有 PFAS 相關的往來」）
      - 想 parallel 跑多件事（每件開一個 sub-agent）
      - 角色切換（例如主代理是「助理」，子代理是「程式工程師 / 會計師」）

    Args:
        goal: 要給子代理完成的目標（中英文都可，越明確越好，支援多行）。
              範例：「把 ~/Downloads 下所有 xlsx 合併成一份、按客戶排序。」
        context: 給子代理的背景資訊（例如已知資料、之前查到的東西）。
                 太長會被截到前 20k 字元。空字串 = 沒額外 context。
        allow_tools: 可選，限制子代理只能用這些 tool（list of str）。
                     預設 None = 繼承全部主代理 tools。
        max_turns: 最多幾輪對話（預設 15）。子代理超過就強制結束回當下結果。

    Returns:
        子代理的最終輸出（通常是完成報告或答案）。
    """
    if not goal or not goal.strip():
        return "❌ goal 不能空"
    if len(goal) > SUB_AGENT_MAX_GOAL_LEN:
        return f"❌ goal 太長（{len(goal)} 字），上限 {SUB_AGENT_MAX_GOAL_LEN}"
    if context and len(context) > SUB_AGENT_MAX_CONTEXT_LEN:
        context = context[:SUB_AGENT_MAX_CONTEXT_LEN] + "\n...(context 被截斷)"

    tools = _pick_tools_for_sub_agent(allow_tools=allow_tools)
    persona = _build_sub_persona(goal, context)

    logger.info(
        "sub-agent 啟動 (goal=%s..., tools=%d, max_turns=%d)",
        goal[:80], len(tools), max_turns,
    )

    # 開獨立 chat session —— 一次把 tools / persona / AFC 上限全塞進 create()
    # ⚠️ 注意：不要在 send_message() 再傳 config，那會覆蓋整份 chat 層 config
    # （把 tools 跟 system_instruction 弄丟）。AFC 上限也寫在這裡。
    from google.genai import types as T
    client = _get_gemini_client()
    chat = client.chats.create(
        model=GEMINI_MODEL,
        config=T.GenerateContentConfig(
            tools=tools,
            system_instruction=persona,
            automatic_function_calling=T.AutomaticFunctionCallingConfig(
                maximum_remote_calls=max_turns,
            ),
        ),
    )

    # 讓子代理自動跑：直接送「開始執行」的信號（系統 prompt 已經交代目標）
    t0 = time.time()
    try:
        resp = chat.send_message("開始。")
    except Exception as e:
        logger.warning("sub-agent 執行中 exception: %s", e)
        return f"❌ 子代理執行失敗: {type(e).__name__}: {e}"

    # Record cost for the sub-agent chat path — chat.send_message bypasses
    # _gemini_generate's record_call, so without this the single most expensive
    # call shape (multi-turn AFC loop + full tool schema, fanned out by the
    # parallel variant) stays invisible to month_to_date_usd / the cap alert.
    # Best-effort; usage_metadata reflects the FINAL AFC response so intermediate
    # tool rounds are under-counted — still far better than recording nothing.
    try:
        from agent_core import cost_tracker
        cost_tracker.record_chat_response(resp, caller="sub_agent")
    except Exception:
        pass

    elapsed = time.time() - t0
    result = (resp.text or "").strip()
    if not result:
        return f"⚠️ 子代理 {max_turns} 輪內沒輸出最終結果（耗時 {elapsed:.1f}s）。可能需要更大 max_turns 或任務本身有問題。"

    logger.info("sub-agent 完成 (%.1fs, 輸出 %d 字)", elapsed, len(result))
    return (
        f"🤖 子代理回報（{elapsed:.1f}s）:\n"
        f"{'─' * 60}\n"
        f"{result}\n"
        f"{'─' * 60}"
    )


# ────────────────────────────────────────────────────────────────────
# T8: 並行多子代理（同時跑多個獨立任務）
# ────────────────────────────────────────────────────────────────────
def delegate_to_sub_agents_parallel(goals: list[str],
                                     shared_context: str = "",
                                     max_parallel: int = SUB_AGENT_PARALLEL_DEFAULT,
                                     max_turns_per_agent: int = SUB_AGENT_MAX_TURNS,
                                     allow_tools: list[str] = None) -> str:
    """**同時**派多個子代理各自跑一個 goal，等全部回來再合併結果。

    vs 串接一個個呼叫 `delegate_to_sub_agent`：
      串接：10 個 goal × 平均 5s = 50s 總時間
      並行：10 個 goal，max_parallel=3 → 約 5 × (10/3) ≈ 17s

    適合場景：
      - 「對這 10 個客戶各做一次 email 摘要」
      - 「同時查這 5 個 PO 的進度」
      - 「把這 20 張發票各自 OCR 結構化」
      - 「對每個品牌 (Blaklader, Lurchi, Richter...) 跑各自的月度報表」

    ⚠️ Gemini API 會 rate-limit，max_parallel 預設 3 就好。更大會撞配額。

    Args:
        goals: 目標清單。每個元素是一個 goal 字串。
               範例：["查 PO A1234 進度", "查 PO B5678 進度", "查 PO C9012 進度"]
        shared_context: 可選，所有子代理都會拿到的共同 context（例如主代理的摘要）。
        max_parallel: 同時最多幾個子代理在跑（預設 3，上限 8）。
        max_turns_per_agent: 每個子代理最多幾輪對話（預設 15）。
        allow_tools: 可選，限制子代理只能用這些 tool。

    Returns:
        所有子代理的結果合併。標註每個 goal 對應的結果與耗時。
        失敗的子代理也會列出（不會中斷其他）。
    """
    if not goals:
        return "❌ goals 不能空"
    if not isinstance(goals, list):
        return "❌ goals 必須是 list of str"
    # 清理 + 驗證
    cleaned = [str(g).strip() for g in goals if str(g).strip()]
    if not cleaned:
        return "❌ goals 全部是空字串"
    if len(cleaned) > 20:
        return f"❌ 一次最多 20 個 goal（你給了 {len(cleaned)}）；分批或用 workflow"

    # clamp parallelism
    parallel = max(1, min(int(max_parallel or SUB_AGENT_PARALLEL_DEFAULT), SUB_AGENT_PARALLEL_CAP))

    logger.info("並行 sub-agents 啟動：%d 個 goal、%d 並行", len(cleaned), parallel)

    t0 = time.time()
    results: dict[int, tuple[str, float, str]] = {}  # idx → (goal, elapsed, result)

    def _run_one(idx: int, goal: str):
        t_start = time.time()
        try:
            r = delegate_to_sub_agent(
                goal=goal,
                context=shared_context,
                allow_tools=allow_tools,
                max_turns=max_turns_per_agent,
            )
        except Exception as e:
            r = f"❌ 子代理執行例外：{type(e).__name__}: {e}"
        return idx, goal, time.time() - t_start, r

    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="subagent") as ex:
        futures = [ex.submit(_run_one, i, g) for i, g in enumerate(cleaned)]
        for fut in as_completed(futures):
            try:
                idx, goal, elapsed, res = fut.result()
                results[idx] = (goal, elapsed, res)
            except Exception as e:
                # 理論上 _run_one 自己已經接住了，這裡再擋一次
                logger.warning("並行 sub-agent future 失敗：%s", e)

    total_elapsed = time.time() - t0

    # 合併輸出（依原順序）
    lines = [
        f"🤝 並行子代理完成（{len(results)} 個 goal，總耗時 {total_elapsed:.1f}s，{parallel} 並行）",
        "=" * 60,
    ]
    for i in range(len(cleaned)):
        if i not in results:
            lines.append(f"\n❌ [{i+1}] {cleaned[i][:80]}  (沒回結果)")
            continue
        goal, elapsed, res = results[i]
        lines.append(f"\n{'─' * 60}")
        lines.append(f"[{i+1}/{len(cleaned)}] {goal[:80]}  ({elapsed:.1f}s)")
        lines.append("─" * 60)
        # 去掉單一版 delegate 回傳的外框裝飾，避免巢狀難讀
        clean = res
        for marker in ("🤖 子代理回報", "─" * 60):
            if clean.startswith(marker):
                clean = clean.split("\n", 1)[1] if "\n" in clean else clean
        lines.append(clean.strip("─ \n"))

    return "\n".join(lines)
