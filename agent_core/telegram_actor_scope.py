"""Telegram 多使用者工具區隔 — owner（大王）vs 其他授權使用者。

大王以外的授權使用者（employee registry / RED_TELEGRAM_AGENT_CHATS /
default-private actor）跟大王共用同一個 bot daemon process，但工具層必須
區隔，否則任何被授權的員工都等於拿到大王的全部身分：

  - sensitive 工具（副作用以「大王的身分」飛出去：寄大王的 Gmail、在大王
    的 Mac 跑 shell、動檔案系統 / vault / 桌面 / 排程…）
      → 非 owner 在 session build 時整顆移除（不是「要 +確認」，是模型
        根本看不到 — 移除比拒絕省 token，也不會引導模型去嘗試）。
  - 大王個人帳號的唯讀工具（個人 Gmail / Calendar / Mac 應用深連結）
      → 同樣移除。唯讀不代表可以給別人讀。
  - 其餘（RAG recall、公司 email lake / timeline、部門查詢、一般對話）
      → 保留。公司資料的縱深由 rag_gateway 的 allowed_colors 把關。

設計成葉模組（同 telegram_agent_config 風格）：只 import os，tg_auth 在
函式內 lazy import，daemon / tool registry 都不在 import path 上。

env 旋鈕（逗號分隔工具名）：
  RED_TG_NONOWNER_TOOL_ALLOW — 明確放行（從封鎖名單豁免）
  RED_TG_NONOWNER_TOOL_BLOCK — 額外封鎖（加進封鎖名單）
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Mapping

# 大王「個人帳號 / 個人機器」的唯讀工具。這些不在 tg_auth._SENSITIVE_TOOLS
# （唯讀原則上免確認），但讀的是大王的私人資料，非 owner 不該碰。
# 公司資料工具（query_po_timeline / search_drive_docs / recall …）刻意
# 不在此 — 公司語料的存取邊界由 rag_gateway allowed_colors 管。
OWNER_PRIVATE_READ_TOOLS = frozenset({
    # 大王個人 Gmail
    "search_gmail", "read_gmail", "summarize_inbox",
    "classify_email", "prioritized_inbox", "analyze_email_reply_times",
    # citation 工具的 fetch_full=True 會直接打大王 Gmail API
    "fetch_email_by_thread_id", "fetch_emails_by_thread_ids",
    # 大王個人 Calendar / 會議
    "list_calendar_events", "briefing_next_meeting",
    # 大王 Mac 上的應用深連結 / 提醒
    "link_to_email", "link_to_calendar", "set_task_reminder",
    # MCP gmail/calendar 唯讀 variants（寫入 variants 已在 sensitive 清單；
    # 這批歷史上註冊成無前綴名，所以 mcp_ 前綴規則蓋不到，要列名）
    "search_threads", "get_thread", "list_drafts", "list_labels",
    "list_events", "get_event", "list_calendars", "suggest_time",
    # Google Chat 歷史。跟 search_drive_docs（刻意留在外、靠 allowed_colors
    # 分色把關）不同：chat chunk 在 ingest 時一律蓋 access_red、沒有任何
    # per-color ACL，本質是 owner 私有通訊（跨部門群組、私訊、大王的對話）。
    # 又因一般對話路徑的非 owner 區隔靠「移除工具」而非設 rag caller，留著會
    # 讓 current_request_caller() 退回 RED→access_where 無過濾→回出全部 chat。
    # 故與大王個人 Gmail 同級，對非 owner 整顆移除（fail-closed）。
    "search_google_chat",
    # xiaohong_memory 讀取工具（學習迴圈 PR 審查抓到的缺口）：recall 的 where
    # 只支援 source 過濾、完全不看 visibility_scope——confirmed_fact（大王
    # 確認的商務事實：付款條件/價格）、note、行為準則、糾正歷史全是大王
    # 私有記憶，非 owner 一律拿不到這些工具（fail-closed，與 search_google_chat
    # 同理由）。等 recall 有 caller-scope 過濾再考慮放行。
    "recall", "load_memory", "memory_stats",
    "list_behaviors", "memory_governance_report",
    "list_mistakes", "recent_factual_corrections",
    # Owner session 主控台的唯讀面：綜覽誰正在跟小紅對話，是大王的管理視角，
    # 不能給員工看（會洩漏其他同事/群組的對話存在與活動）。控制面
    # （pause/resume/reset）另循 tg_auth._SENSITIVE_TOOLS 移除。
    "list_sessions",
})

# MCP server 工具一律 owner-only：它們跑在大王的 Mac、用大王配置的憑證，
# 而且 server 清單（mcp_servers.json）可任意擴充 — 列舉唯讀工具名永遠列
# 不全（gemini-code-assist 抓到 mcp_filesystem_read_file 等唯讀檔案工具
# 漏網就是例證）。前綴 + _is_mcp_tool 屬性雙重判定；要對員工開特定 MCP
# 工具用 RED_TG_NONOWNER_TOOL_ALLOW 明確放行。
_MCP_TOOL_NAME_PREFIX = "mcp_"


def _env_tool_names(var: str) -> frozenset[str]:
    raw = os.environ.get(var, "") or ""
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def actor_requires_separation(actor: Mapping[str, Any] | None) -> bool:
    """這個 inbound actor 是否要套非 owner 區隔？

    規則：
      - None / 非 Mapping / 空 dict → False（tests、REPL bridge、舊呼叫者
        沒帶 actor — 維持大王路徑，行為不變）。
      - is_owner=true → False（大王本人 / approval owner）。
      - 其餘（employee registry / env 綁定 / default-private / join 核准）
        → True。
    """
    if not isinstance(actor, Mapping) or not actor:
        return False
    return str(actor.get("is_owner") or "").strip().lower() != "true"


def actor_label(actor: Mapping[str, Any] | None) -> str:
    """給 log / LLM context 用的人類可讀身分標籤。"""
    if not isinstance(actor, Mapping):
        return "員工"
    name = str(actor.get("name") or "").strip()
    email = str(actor.get("email") or "").strip()
    if name and email:
        return f"{name} ({email})"
    return name or email or f"chat {actor.get('chat_id', '?')}"


def is_tool_blocked_for_non_owner(
    tool_name: str,
    *,
    allow: frozenset[str] | None = None,
    block: frozenset[str] | None = None,
    is_mcp_tool: bool = False,
) -> bool:
    """非 owner actor 是否該封鎖這顆工具。

    封鎖 = sensitive（tg_auth.is_sensitive，含 tool_tiers pattern）
           ∪ OWNER_PRIVATE_READ_TOOLS
           ∪ MCP 工具（mcp_ 前綴或 _is_mcp_tool 屬性）
           ∪ RED_TG_NONOWNER_TOOL_BLOCK
           − RED_TG_NONOWNER_TOOL_ALLOW

    allow/block 可由 caller 預先解析傳入（避免逐工具重複讀 env）。
    """
    name = str(tool_name or "").strip()
    if not name:
        return False
    if allow is None:
        allow = _env_tool_names("RED_TG_NONOWNER_TOOL_ALLOW")
    if block is None:
        block = _env_tool_names("RED_TG_NONOWNER_TOOL_BLOCK")
    if name in allow:
        return False
    if name in block:
        return True
    if is_mcp_tool or name.startswith(_MCP_TOOL_NAME_PREFIX):
        return True
    if name in OWNER_PRIVATE_READ_TOOLS:
        return True
    try:
        from agent_core.tg_auth import is_sensitive
        return is_sensitive(name)
    except Exception:
        # tg_auth 載不起來時 fail-closed：分不出 safe/sensitive 就一律封。
        return True


def filter_tools_for_non_owner(tools_list: Iterable[Any]) -> tuple[list[Any], list[str]]:
    """回傳 (保留的工具, 被移除的工具名)。順序維持原 list。"""
    allow = _env_tool_names("RED_TG_NONOWNER_TOOL_ALLOW")
    block = _env_tool_names("RED_TG_NONOWNER_TOOL_BLOCK")
    kept: list[Any] = []
    removed: list[str] = []
    for fn in tools_list:
        name = getattr(fn, "__name__", "")
        if is_tool_blocked_for_non_owner(
            name,
            allow=allow,
            block=block,
            is_mcp_tool=bool(getattr(fn, "_is_mcp_tool", False)),
        ):
            removed.append(name)
        else:
            kept.append(fn)
    return kept, removed


def actor_system_addendum(
    actor: Mapping[str, Any] | None,
    *,
    has_own_google_tools: bool = False,
) -> str:
    """非 owner session 的 system instruction 附加段（區隔規則）。

    has_own_google_tools：此 actor 是否拿到了 actor-scoped Gmail/行事曆工具
    （以他自己公司信箱身分操作）。True 時放寬「寄信/行事曆=大王限定」的措辭，
    改說明那些動作落在他自己的帳號。
    """
    if not actor_requires_separation(actor):
        return ""
    label = actor_label(actor)
    color = str((actor or {}).get("color") or "").strip() or "未知"
    email = str((actor or {}).get("email") or "").strip()
    my_mail_rule = (
        f"他說「我的信 / 我的信箱」指的是 {email}（他自己的公司信箱），"
        f"絕不是大王的個人 Gmail。\n" if email else ""
    )
    if has_own_google_tools:
        mail_cal_rule = (
            "2. 你手上的 Gmail / 行事曆工具一律操作**他自己的帳號**"
            f"（{email}）：回信回他信箱的信、寄信用他的名義、排會議建在他"
            "的行事曆。「通知全公司開會」= 用 create_calendar_event 把全公司"
            "加為與會者（attendees 填「全公司」），系統會以他名義寄出邀請。"
            "**絕不可**碰大王的 Gmail / 行事曆。\n"
            "3. 其餘僅大王可用的工具（shell、檔案、桌面操控、大王私人資料）"
            "已自動隱藏；他要求這類動作就說明僅大王本人可用。\n"
        )
    else:
        mail_cal_rule = (
            "2. 只能用你目前看得到的工具；部分工具（寄信、shell、檔案、桌面"
            "操控等）僅大王可用，已自動隱藏 — 若此使用者要求這類動作，直接"
            "說明該功能僅大王本人可用，請他聯絡大王。\n"
        )
    return (
        "\n\n【使用者區隔 — 此對話對象不是大王】\n"
        f"目前對話對象是「{label}」（部門色 {color}），不是大王本人。\n"
        f"{my_mail_rule}"
        "鐵則：\n"
        "1. 大王的私人資料（個人 Gmail / 行事曆 / Mac 上的檔案內容 / 私人"
        "記憶）不得查詢、轉述或摘要給此使用者。\n"
        f"{mail_cal_rule}"
        "4. 公司營運資料（訂單 / 樣品 / 出貨 / 內部信件語料）可以照常查詢"
        "回答。\n"
        "5. 稱呼對方用他的名字，不要叫他大王。\n"
        "6. 🛑【指錯→重新查核（最高指導原則）】此使用者說你答錯、數字不對、"
        "要你再確認時：必須用手上的查詢工具把該事實重查一次，依重查結果照實"
        "回答——推翻原答就更正並說明、仍支持就維持並列出處、重查後仍無法確認"
        "就老實說「查不到能確認的資料」。禁止沒重查就道歉附和或硬拗，禁止"
        "編造任何數字、單號或聯絡資訊。\n"
    )


def actor_message_prefix(actor: Mapping[str, Any] | None) -> str:
    """非 owner 訊息的 per-turn 發訊者標記（接在系統時間行後面）。"""
    if not actor_requires_separation(actor):
        return ""
    color = str((actor or {}).get("color") or "").strip() or "?"
    return f"[發訊者：{actor_label(actor)}｜部門色 {color}｜非大王]\n"
