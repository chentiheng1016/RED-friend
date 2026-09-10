"""Orange agent — Sales / 業務.

intent 列表（Phase 3c 起步，覆蓋 tool_registry_catalog 已暴露的 8 個函式）:

  query.* — 唯讀
    query.quote_history          payload={"customer", "sku", "direction", "recent_months", "expand_aliases"}
    query.customer_360           payload={"customer", "days", "push_telegram"}
    query.active_customers       payload={"days", "min_emails"}
    query.customer_alerts        payload={"days"}
    query.search_emails          payload={"query": str, "n_results"?: int}  ← RAG Gmail 語意搜索

  command.* — 寫操作 / 觸發 LLM 抽取
    command.extract_quote_email  payload={"message_id"}
    command.build_quote_history  payload={"days", "max_threads", "pause_ms"}
    command.batch_extract_quotes payload={"limit"}
    command.generate_quote       payload={"customer", "shoe_model", "items", ...}
    command.sync_gmail           payload={"gmail_query"?: str, "max_threads"?: int, "thread_id"?: str}

矩陣允許的 caller（ORANGE 為 target）:
  Yellow / Green / Blue / Indigo / Purple / Gray / Black / Red 都可查 Orange
  （已註冊的 real / stub 部門與 Red 全部走同一套 permission middleware）
"""
from __future__ import annotations

from typing import Any, Mapping

from agent_core.agents.base_agent import BaseAgent
from agent_core.agents.permission_matrix import Agent
from agent_core.agents.orange_sales import (
    customer_intel as _ci,
    quote as _q,
    quote_batch as _qb,
    quote_gen as _qg,
)


class OrangeSalesAgent(BaseAgent):
    identity = Agent.ORANGE

    def handle_query(
        self,
        intent: str,
        payload: Mapping[str, Any],
        trace_id: str,
    ) -> Any:
        # ---- query.* ----
        if intent == "query.quote_history":
            return {"text": _q.query_quote_history(
                customer=str(payload.get("customer", "")),
                sku=str(payload.get("sku", "")),
                direction=str(payload.get("direction", "")),
                recent_months=int(payload.get("recent_months", 24)),
                expand_aliases=bool(payload.get("expand_aliases", True)),
            )}

        if intent == "query.customer_360":
            return {"text": _ci.customer_360(
                customer=str(payload.get("customer", "")),
                days=int(payload.get("days", 90)),
                push_telegram=bool(payload.get("push_telegram", False)),
            )}

        if intent == "query.active_customers":
            return {"text": _ci.list_active_customers(
                days=int(payload.get("days", 90)),
                min_emails=int(payload.get("min_emails", 2)),
            )}

        if intent == "query.customer_alerts":
            return {"text": _ci.customer_alerts(
                days=int(payload.get("days", 90)),
            )}

        # ---- command.* ----
        if intent == "command.extract_quote_email":
            return {"result": _q.extract_quote_from_email(
                str(payload.get("message_id", "")),
            )}

        if intent == "command.build_quote_history":
            return {"result": _q.build_quote_history(
                days=int(payload.get("days", 730)),
                max_threads=int(payload.get("max_threads", 1000)),
                pause_ms=int(payload.get("pause_ms", 400)),
            )}

        if intent == "command.batch_extract_quotes":
            return {"result": _qb.batch_extract_quotes_from_parquet(
                limit=int(payload.get("limit", 5000)),
            )}

        if intent == "command.generate_quote":
            kwargs = {k: v for k, v in payload.items()
                      if k not in {"trace_id"}}
            return {"result": _qg.generate_quote(**kwargs)}

        if intent == "query.search_emails":
            query_text = str(payload.get("query", "")).strip()
            if not query_text:
                return {"error": "query 不能為空"}
            from agent_core.rag_gateway import current_request_caller, semantic_search
            n = int(payload.get("n_results", 5))
            hits = semantic_search(
                "gmail_threads",
                query_text,
                caller=current_request_caller(default=Agent.RED),
                n_results=n,
                trace_id=trace_id,
            )
            return {"query": query_text, "hits": hits, "total": len(hits)}

        if intent == "command.sync_gmail":
            from agent_core.ingest import gmail_sync as _gs
            thread_id = str(payload.get("thread_id", "")).strip()
            if thread_id:
                return _gs.sync_thread(thread_id)
            gmail_query = str(payload.get("gmail_query", "newer_than:180d")).strip()
            max_threads = int(payload.get("max_threads", 200))
            return _gs.sync_query(gmail_query, max_threads)

        raise ValueError(f"OrangeSalesAgent: unknown intent {intent!r}")
