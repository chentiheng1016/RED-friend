"""Built-in tool list + skill loader + hot-reload."""

from agent_core import mcp_bridge as _mcp_bridge
from agent_core import skills as _skills_mod
from agent_core.chat_session import chat_state
from agent_core.skills import _SKILLS_DIR, _load_skills_from_dir, list_skills
from agent_core.tool_annotations import resolve_string_annotations as _resolve_string_annotations
from agent_core.tool_registry_catalog import BASE_BUILTIN_TOOLS, build_builtin_tools

_build_chat_fn = None


def set_build_chat_fn(fn):
    """Inject the chat builder so reload_skills can rebuild the session."""
    global _build_chat_fn
    _build_chat_fn = fn


def reload_skills():
    """重新掃描 skills/ 並重建 chat；tools_list 會原地更新。"""
    tools_list[:] = [tool for tool in tools_list if not getattr(tool, "_is_skill", False)]
    new_tools, info = _load_skills_from_dir(_SKILLS_DIR)
    # Validate exactly like startup does (see _validate_tool_schemas call
    # below): a skill whose schema Gemini rejects (e.g. `def fn(x: list)`
    # missing the item type) must be filtered HERE too. Otherwise reload
    # reports success and the rebuilt chat 400s on the *next* user message —
    # the precise failure _validate_tool_schemas exists to prevent.
    good_tools, broken = _validate_tool_schemas(_resolve_string_annotations(new_tools))
    tools_list.extend(good_tools)
    _skills_mod._loaded_skills_info = info
    if _build_chat_fn is None:
        return "skill 已重載，但 build_chat_fn 尚未注入（agent.py 初始化順序異常）。"
    try:
        chat_state["chat"] = _build_chat_fn()
        chat_state["turns"] = 0
    except Exception as exc:
        return f"skill 已重載但 chat 重建失敗：{exc}"
    skill_count = len([tool for tool in tools_list if getattr(tool, "_is_skill", False)])
    msg = f"✅ 已重新載入 skills/，目前 {skill_count} 個 skill 工具。對話記憶已重置 — 接下來的對話會用最新工具。"
    if broken:
        names = "、".join(name for name, _reason in broken)
        msg += f"\n⚠️ {len(broken)} 個工具 schema 有問題已剔除（否則下一則訊息會 400）：{names}"
    return msg


BUILTIN_TOOLS = build_builtin_tools(extra_tools=[list_skills, reload_skills])

_skill_tools, _skills_mod._loaded_skills_info = _load_skills_from_dir(_SKILLS_DIR)
_mcp_tools, _mcp_info = _mcp_bridge.load_mcp_tools()
_mcp_bridge.MCP_TOOLS = _mcp_tools
_mcp_bridge.MCP_SERVERS_INFO = _mcp_info


def _validate_tool_schemas(tools: list) -> tuple[list, list]:
    """對每個 tool 跑 Gemini FunctionDeclaration.from_callable，過濾掉 schema 有問題的。

    最常見的 bug：`def fn(x: list)` 沒寫 `list[str]`，Gemini 會拒絕（array 缺 items）。
    其他 Python 合法但 Gemini 不吃的 type hint 也會在這被擋下。

    過濾掉壞 tool 避免 Telegram / REPL 在第一次 LLM call 時 400 炸整個 chat session。
    """
    try:
        from google.genai import types as _T
        from agent_core.gemini_client import _get_gemini_client
        client = _get_gemini_client()
    except SystemExit:
        # Import-time schema validation must not make smoke tests or daemon
        # imports require a live Gemini key. Runtime calls will still surface
        # the missing-key failure when the client is actually needed.
        return tools, []
    except Exception:
        # client 還沒準備好（例如 API key 沒設），跳過驗證
        return tools, []

    good = []
    broken = []
    for t in tools:
        try:
            fd = _T.FunctionDeclaration.from_callable(client=client._api_client, callable=t)
            if fd.parameters:
                props = fd.parameters.properties or {}
                # 掃 array 缺 items 的
                bad_props = [
                    p for p, pd in props.items()
                    if str(pd.type) == "Type.ARRAY" and not pd.items
                ]
                if bad_props:
                    broken.append((t.__name__, f"array 參數缺 item type: {bad_props}"))
                    continue
        except Exception as e:
            broken.append((t.__name__, f"from_callable 失敗: {str(e)[:80]}"))
            continue
        good.append(t)
    return good, broken


_all_tools = _resolve_string_annotations(BUILTIN_TOOLS + _skill_tools + _mcp_tools)
tools_list, _broken_tools = _validate_tool_schemas(_all_tools)

if _broken_tools:
    import sys
    print(
        f"[tool_registry] ⚠️ {len(_broken_tools)} 個 tool schema 有問題，已從 tools_list 剔除：",
        file=sys.stderr,
    )
    for name, reason in _broken_tools:
        print(f"  • {name}: {reason}", file=sys.stderr)
    print(
        "  → 這些 tool 在 LLM 呼叫時會 400 INVALID_ARGUMENT。請把 `list` 改成 `list[str]` 或類似。",
        file=sys.stderr,
    )
