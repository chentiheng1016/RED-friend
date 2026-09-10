"""White agent — 法務 / Legal SoT.

intent 列表:
  - query.profile               returns White capability profile
  - query.list_specs            payload={"customer": str, "product_model": str}（兩個都可空 = 列全部）
  - query.get_latest_spec       payload={"customer": str, "product_model": str}
  - query.get_spec_version      payload={"customer", "product_model", "version": "latest"|"previous"|"v..."}
  - query.compare_specs         payload={"customer", "product_model", "old_version": "previous"|...}
  - query.search_docs           payload={"query": str, "n_results"?: int}  ← RAG Drive 語意搜索
  - command.parse_spec_sheet    payload={"file_path", "customer"?, "product_model"?, "save"?}
  - command.sync_drive          payload={"all_drives"?: bool, "folder_id"?: str, "file_id"?: str, "recursive"?: bool}
                                  all_drives=true → 掃描全 Drive + 啟用每日定時同步

White 是 SoT：所有 color 都可讀（permission_matrix 已允許）；White 本身
不會主動 query 別的部門。
"""
from __future__ import annotations

from typing import Any, Mapping

from agent_core.agents.base_agent import BaseAgent
from agent_core.agents.permission_matrix import Agent
from agent_core.agents.white_legal import specs as _sp


class WhiteLegalAgent(BaseAgent):
    identity = Agent.WHITE

    def handle_query(
        self,
        intent: str,
        payload: Mapping[str, Any],
        trace_id: str,
    ) -> Any:
        if intent == "query.profile":
            return {
                "profile": {
                    "color": "white",
                    "department": "法務 / Legal SoT",
                    "summary": "查合約、測試報告、規格書與 Drive RAG 文件；White 是所有部門可讀的 SoT。",
                    "query_intents": [
                        "query.profile",
                        "query.list_specs",
                        "query.get_latest_spec",
                        "query.get_spec_version",
                        "query.compare_specs",
                        "query.search_docs",
                    ],
                    "command_intents": [
                        "command.parse_spec_sheet",
                        "command.sync_drive",
                    ],
                    "telegram_note": (
                        "Telegram shortcut only exposes query.*; parsing and Drive sync "
                        "must go through /ingest ... +確認."
                    ),
                }
            }

        if intent == "query.list_specs":
            return {"text": _sp.list_specs(
                customer=str(payload.get("customer", "")),
                product_model=str(payload.get("product_model", "")),
            )}

        if intent == "query.get_latest_spec":
            customer = str(payload.get("customer", "")).strip()
            product_model = str(payload.get("product_model", "")).strip()
            if not customer or not product_model:
                return {"error": "customer 和 product_model 都要填"}
            spec = _sp._load_spec_version(customer, product_model, "latest")
            return {
                "customer": customer,
                "product_model": product_model,
                "found": bool(spec),
                "spec": spec or None,
            }

        if intent == "query.get_spec_version":
            customer = str(payload.get("customer", "")).strip()
            product_model = str(payload.get("product_model", "")).strip()
            version = str(payload.get("version", "latest"))
            if not customer or not product_model:
                return {"error": "customer 和 product_model 都要填"}
            spec = _sp._load_spec_version(customer, product_model, version)
            return {
                "customer": customer,
                "product_model": product_model,
                "version": version,
                "found": bool(spec),
                "spec": spec or None,
            }

        if intent == "query.compare_specs":
            return {"text": _sp.compare_specs(
                customer=str(payload.get("customer", "")),
                product_model=str(payload.get("product_model", "")),
                old_version=str(payload.get("old_version", "previous")),
            )}

        if intent == "query.search_docs":
            query_text = str(payload.get("query", "")).strip()
            if not query_text:
                return {"error": "query 不能為空"}
            from agent_core.rag_gateway import current_request_caller, semantic_search
            n = int(payload.get("n_results", 5))
            hits = semantic_search(
                "drive_docs",
                query_text,
                caller=current_request_caller(default=Agent.RED),
                n_results=n,
                trace_id=trace_id,
            )
            return {"query": query_text, "hits": hits, "total": len(hits)}

        if intent == "command.parse_spec_sheet":
            return {"text": _sp.parse_spec_sheet(
                file_path=str(payload.get("file_path", "")),
                customer=str(payload.get("customer", "")),
                product_model=str(payload.get("product_model", "")),
                save=bool(payload.get("save", True)),
            )}

        if intent == "command.sync_drive":
            from agent_core.ingest import drive_sync as _ds
            from agent_core.ingest.sync_config import (
                enable_all_drives as _enable_all,
                disable_all_drives as _disable_all,
            )
            # Use a sentinel to distinguish "key absent" from explicit false.
            # Only Python True/False literals update the persisted config:
            #   {"all_drives": true}  → enable global mode + run full sync
            #   {"all_drives": false} → disable global mode, then honour folder/file
            #   key absent            → leave config unchanged
            _ad_value = payload.get("all_drives", None)
            if _ad_value is True:
                _enable_all()
                result = _ds.sync_all_drives()
                return {"all_drives": True, "config_updated": True, **result}
            if _ad_value is False:
                _disable_all()
            folder_id = str(payload.get("folder_id", "")).strip()
            file_id   = str(payload.get("file_id", "")).strip()
            if file_id:
                return _ds.sync_file(file_id)
            if folder_id:
                if _ds.is_shared_drive_id(folder_id):
                    return _ds.sync_shared_drive(folder_id)
                return _ds.sync_folder(folder_id, recursive=bool(payload.get("recursive", False)))
            return _ds.sync_status()

        raise ValueError(f"WhiteLegalAgent: unknown intent {intent!r}")
