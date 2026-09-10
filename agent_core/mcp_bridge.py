"""MCP (Model Context Protocol) ↔ Gemini function-calling bridge.

讓小紅用 Anthropic MCP 生態的 server 當工具，跟 skills/ 一樣無縫整合。

架構：
  1. 啟動時讀 `mcp_servers.json`（repo 根目錄）
  2. 每個 server 跑一次 subprocess 呼叫 list_tools() 取 tool schema
  3. 每個 MCP tool 動態產生一個 Python 函式：
       - 真實簽名（從 inputSchema 的 properties 產出 KEYWORD_ONLY 參數）
       - 真實 docstring（MCP server 自己的說明）
       - 呼叫時起短連線跑 call_tool → 回字串
     Gemini 的 automatic function calling 會 introspect 這些函式，無感整合。
  4. 匯出 `MCP_TOOLS: list[Callable]` 供 tool_registry 合併進 BUILTIN_TOOLS

Config 格式（mcp_servers.json）：
  {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"],
      "env": {}
    }
  }

失敗的 server 會被 log 跳過，不影響主 agent 啟動。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
from typing import Any, Callable

from agent_core.logging_and_paths import _SCRIPT_DIR, logger, startup_print


MCP_CONFIG_FILE = os.path.join(_SCRIPT_DIR, "mcp_servers.json")


# ── JSON Schema → Python 型別對照（給 inspect.Signature 用）──
_TYPE_MAP = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "object": dict,
    "null": type(None),
}


def _schema_to_py_type(schema: dict | None):
    """JSON schema → Python 型別註解。array 一定要回 typed list（如 list[str]）
    否則 Gemini 會噴 'properties[...].items: missing field'。"""
    if not schema:
        return str
    t = schema.get("type")
    if isinstance(t, list):  # ["string", "null"] 這種
        t = next((x for x in t if x != "null"), "string")
    if t == "array":
        item_schema = schema.get("items") or {"type": "string"}
        item_type = _schema_to_py_type(item_schema)
        return list[item_type]
    return _TYPE_MAP.get(t or "string", str)


def _sanitize_param_name(name: str) -> str:
    """Python 識別字符化；前導數字前加 _。"""
    safe = re.sub(r"\W", "_", name)
    if safe and safe[0].isdigit():
        safe = "_" + safe
    return safe or "_arg"


def _sanitize_tool_name(server_name: str, tool_name: str) -> str:
    """Gemini 要求名稱 [a-zA-Z0-9_-]，長度 ≤ 64。加 server 前綴避免撞。"""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp_{server_name}_{tool_name}")
    return safe[:63]


# ────────────────────────────────────────────────────────────────────
# 底層 async：起 stdio subprocess → initialize → list/call
# ────────────────────────────────────────────────────────────────────
def _get_mcp():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    return ClientSession, StdioServerParameters, stdio_client


async def _async_list_tools(server_params) -> list[dict]:
    ClientSession, _, stdio_client_fn = _get_mcp()
    async with stdio_client_fn(server_params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema or {"type": "object", "properties": {}},
                }
                for t in result.tools
            ]


# C5（review 找到的延伸面）：MCP server 回傳值原樣餵給 LLM，等同 V3 之延伸。
# 任意 MCP server（filesystem 讀檔、fetch 抓網頁、其他第三方）回的內容都
# 屬 untrusted 來源 — 可能含 prompt injection、API key、信用卡、PII。原本
# 直接 return 給 Gemini，整個 V3/V5/C4 防線在這裡被繞。修法：
#   1. 全過 sanitize_for_llm（injection redact + PII redact 兩層）
#   2. 加大小上限避免 token DoS（攻擊者把 1MB 廢話塞進 LLM context 燒錢）
_MCP_OUTPUT_LIMIT = 32_000  # ~8K Gemini tokens；超過截斷


async def _async_call_tool(server_params, tool_name: str, arguments: dict) -> str:
    from agent_core.prompt_injection import sanitize_for_llm

    ClientSession, _, stdio_client_fn = _get_mcp()
    async with stdio_client_fn(server_params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments or {})
            parts = []
            for c in (result.content or []):
                if hasattr(c, "text") and c.text is not None:
                    parts.append(c.text)
                elif hasattr(c, "type"):
                    parts.append(f"[non-text content type={c.type}]")
                else:
                    # review LOW：原本 str(c) 會 leak 物件 repr（含 binary blob
                    # metadata、attacker-supplied bytes）。改成固定描述。
                    parts.append(f"[non-text content type={type(c).__name__}]")
            out = "\n".join(parts) if parts else "(no content)"
            # C5: sanitize 後回傳。長度超限就截斷且註明。
            # round 4 LOW：先截斷再 sanitize — 否則 100MB attacker payload 會
            # 跑完整個 _PATTERNS regex scan 才被切掉，CPU/記憶體放大攻擊。
            # round 5 X6：原本切 2× cap 邊界可能切到 secret 中段（前半 11 chars
            # 不符 AKIA 16-char regex，後半也不符），漏 redact 半截 token。
            # 改成 sanitize 一個更大但仍有界的窗（4× cap = 128KB；足以覆蓋
            # 任何單一 secret pattern + 給 regex 完整上下文），再精切顯示。
            _SANITIZE_BUDGET = _MCP_OUTPUT_LIMIT * 4  # 128KB，遠大於任何 token
            original_len = len(out)
            if original_len > _SANITIZE_BUDGET:
                out = out[:_SANITIZE_BUDGET]
            sanitized = sanitize_for_llm(out)
            if len(sanitized) > _MCP_OUTPUT_LIMIT:
                sanitized = (sanitized[:_MCP_OUTPUT_LIMIT]
                             + f"\n…（MCP 回應 {original_len} 字過長，已截至 "
                             f"{_MCP_OUTPUT_LIMIT} 字。如需完整內容請拆 query 或減 limit）")
            if getattr(result, "isError", False):
                return f"[MCP tool error] {sanitized}"
            return sanitized


# ────────────────────────────────────────────────────────────────────
# 動態產 Python 函式
# ────────────────────────────────────────────────────────────────────
def _make_mcp_callable(server_params, server_name: str, tool_info: dict) -> Callable:
    """從 MCP tool info 造一個 Gemini AFC 能吃的 Python 函式。

    - 真實簽名（從 inputSchema 的 properties 推）
    - 真實 docstring（MCP server 自己的說明）
    - 呼叫時 asyncio.run 進短連線跑 tool
    """
    tool_name = tool_info["name"]
    description = tool_info.get("description") or f"MCP tool {tool_name} on {server_name}"
    schema = tool_info.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    # 把 MCP 的 property name 映射到 Python-safe param name
    # （大多是英文沒事，保險起見還是 sanitize）
    param_map: dict[str, str] = {}   # safe_name → original_name
    params = []
    for orig_name, pdef in properties.items():
        safe = _sanitize_param_name(orig_name)
        # 去重（罕見）
        while safe in param_map:
            safe += "_"
        param_map[safe] = orig_name
        ptype = _schema_to_py_type(pdef)
        default = inspect.Parameter.empty if orig_name in required else None
        params.append(inspect.Parameter(
            safe,
            inspect.Parameter.KEYWORD_ONLY,
            default=default,
            annotation=ptype,
        ))
    sig = inspect.Signature(parameters=params, return_annotation=str)

    # Round 6 Y3 + round 7 補：MCP server 的 path 參數**不經 path_safety**，
    # 直接由 server 自帶的 allowed_dirs 守門。但 server allowed_dirs 通常 = 整個 repo，
    # 攻擊者過 V4 後可以讀 / 寫 agent_core/persona.py / mcp_servers.json 等
    # 受 _PROTECTED_PROJECT_DIRS 應該保護的路徑。
    # 這裡 hook：凡 schema property name 看起來像「路徑」就先過 safe_path。
    # 涵蓋單複數 + 連字號 / 底線分隔。
    # 注意：不要把 url / uri 放進來 — 那是網址不是檔案路徑，path_safety 看
    # 不懂 URL 反而會把 'http://x/y' 解成 './http:/x/y' 給 MCP server 造成
    # 誤判。fetch 類 MCP 是 by-design 通網路，且 V4 已確認門。
    _PATH_PARAM_HINTS = (
        "path", "file", "directory", "dir", "folder", "src", "source",
        "dest", "destination", "output", "input", "filename", "target",
        "out_path", "in_path",
    )
    _PATH_PARAM_PLURAL_HINTS = tuple(h + "s" for h in _PATH_PARAM_HINTS)

    # Round 8 L8-2：camelCase-only schemas（searchPath/rootPath/outputPath）
    # 不會經過底線分割。先把 camelCase 轉 snake，再走原邏輯。
    import re as _re
    _CAMEL_BOUNDARY = _re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

    def _looks_like_path_param(name: str) -> bool:
        # 1. lowercase + 把 - 轉 _ + 把 camelCase 拆成 snake_case 樣式
        n = name.replace("-", "_")
        n = _CAMEL_BOUNDARY.sub("_", n).lower()
        # 整個 name 等於某個 hint（單或複數）
        if n in _PATH_PARAM_HINTS or n in _PATH_PARAM_PLURAL_HINTS:
            return True
        # underscored 的 sub-tokens 中含 hint 或其複數
        parts = set(n.split("_"))
        for h in _PATH_PARAM_HINTS:
            if h in parts or (h + "s") in parts:
                return True
            # 前/後綴形式：如 out_path / file_path
            if n.startswith(h + "_") or n.endswith("_" + h):
                return True
        return False

    def _fn(**kwargs):
        # 把 safe_name 翻回 MCP 原始參數名
        args = {param_map.get(k, k): v for k, v in kwargs.items() if v is not None}
        # Y3: 過 path_safety
        try:
            from agent_core.path_safety import safe_path
        except Exception:
            safe_path = None
        if safe_path is not None:
            for k, v in list(args.items()):
                if not _looks_like_path_param(k):
                    continue
                if isinstance(v, str):
                    try:
                        args[k] = safe_path(v)
                    except ValueError as e:
                        return f"[MCP path-safety] {tool_name}({k}={v!r}) 被擋：{e}"
                elif isinstance(v, list):
                    new_list = []
                    for item in v:
                        if isinstance(item, str):
                            try:
                                new_list.append(safe_path(item))
                            except ValueError as e:
                                return (f"[MCP path-safety] {tool_name}({k}=[..., {item!r}, ...]) "
                                        f"清單元素被擋：{e}")
                        else:
                            new_list.append(item)
                    args[k] = new_list
        try:
            return asyncio.run(_async_call_tool(server_params, tool_name, args))
        except Exception as e:
            logger.warning("MCP tool %s 執行失敗：%s", tool_name, e)
            return f"[MCP 呼叫失敗] {type(e).__name__}: {e}"

    _fn.__name__ = _sanitize_tool_name(server_name, tool_name)
    _fn.__doc__ = description[:1024]
    _fn.__signature__ = sig
    _fn._is_mcp_tool = True  # 標記方便後續 introspect
    _fn._mcp_server = server_name
    _fn._mcp_tool = tool_name
    return _fn


# ────────────────────────────────────────────────────────────────────
# 載入 config + 初始化
# ────────────────────────────────────────────────────────────────────
def _load_config() -> dict:
    if not os.path.isfile(MCP_CONFIG_FILE):
        return {}
    try:
        with open(MCP_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "mcpServers" in data:
            data = data["mcpServers"] or {}
        # 過掉 _ 開頭的註解 key
        return {k: v for k, v in data.items() if not k.startswith("_")} if isinstance(data, dict) else {}
    except Exception as e:
        startup_print(f"[MCP] ⚠️ mcp_servers.json 讀取失敗（{e}），跳過 MCP 整合")
        return {}


def _build_server_params(cfg: dict):
    _, StdioServerParameters, _ = _get_mcp()
    command = cfg.get("command")
    if not command:
        raise ValueError("MCP server config 缺 'command'")
    args = cfg.get("args") or []
    env_override = cfg.get("env") or None
    # 合併環境變數：繼承當前 PATH/HOME 等，再疊 user 的 override
    if env_override is not None:
        merged = os.environ.copy()
        merged.update(env_override)
        env = merged
    else:
        env = None
    return StdioServerParameters(command=command, args=args, env=env)


def load_mcp_tools() -> tuple[list[Callable], list[str]]:
    """讀 config → 對每個 server 取 tool list → 產生 callable 清單。
    回傳 (tools, server_info_lines)。"""
    config = _load_config()
    if not config:
        return [], []

    try:
        _get_mcp()
    except ImportError:
        startup_print("[MCP] ⚠️ mcp SDK 未安裝（pip install mcp），跳過 MCP 整合")
        return [], []

    tools: list[Callable] = []
    info: list[str] = []

    for server_name, cfg in config.items():
        try:
            server_params = _build_server_params(cfg)
        except Exception as e:
            startup_print(f"[MCP] ⚠️ server '{server_name}' config 錯誤：{e}")
            continue

        try:
            tool_infos = asyncio.run(_async_list_tools(server_params))
        except Exception as e:
            startup_print(f"[MCP] ⚠️ server '{server_name}' 連線 / list_tools 失敗：{e}")
            continue

        if not tool_infos:
            startup_print(f"[MCP] server '{server_name}' 沒回任何 tool")
            continue

        added = 0
        for ti in tool_infos:
            try:
                tools.append(_make_mcp_callable(server_params, server_name, ti))
                added += 1
            except Exception as e:
                startup_print(f"[MCP] ⚠️ 跳過 {server_name}.{ti.get('name','?')}：{e}")

        if added:
            info.append(f"{server_name} ({added} tools)")
            startup_print(f"[MCP] ✅ 載入 {server_name}（{added} 個 tool）")

    return tools, info


# ────────────────────────────────────────────────────────────────────
# Module-level cache — agent.py 啟動時呼叫 load_mcp_tools()
# ────────────────────────────────────────────────────────────────────
MCP_TOOLS: list[Callable] = []
MCP_SERVERS_INFO: list[str] = []


def bootstrap():
    global MCP_TOOLS, MCP_SERVERS_INFO
    MCP_TOOLS, MCP_SERVERS_INFO = load_mcp_tools()


def list_mcp_servers() -> str:
    """給小紅 / 大王查目前接到哪些 MCP server（可當 tool）。"""
    if not MCP_SERVERS_INFO:
        return "🔌 目前沒有載入任何 MCP server。\n\n要加：編輯 mcp_servers.json，新增 server 後重啟 agent。範例見 mcp_servers.json.example。"
    lines = ["🔌 MCP servers 已載入："]
    for s in MCP_SERVERS_INFO:
        lines.append(f"  • {s}")
    lines.append(f"\n共 {len(MCP_TOOLS)} 個 MCP tool。工具名前綴是 mcp_<server>_<tool>。")
    return "\n".join(lines)
