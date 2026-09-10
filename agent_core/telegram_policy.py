"""Human-readable Telegram command policy for admin surfaces."""
from __future__ import annotations

from typing import Any

from agent_core.agents.permission_matrix import Agent, QUERY_MATRIX


def _targets_text(agent: Agent) -> str:
    if agent is Agent.RED:
        return "all departments"
    targets = sorted({agent.value, *(target.value for target in QUERY_MATRIX.get(agent, frozenset()))})
    return ", ".join(targets) if targets else agent.value


def telegram_command_policy_rows() -> list[dict[str, Any]]:
    """Return the effective Telegram command allowlist as display rows."""
    rows: list[dict[str, Any]] = [
        {
            "actor": "red",
            "commands": "/new, /reset, /whoami",
            "scope": "Owner diagnostics and session control.",
            "confirmation": "No",
        },
        {
            "actor": "red",
            "commands": "/dept <color> query.*",
            "scope": "Read-only query access to all registered departments.",
            "confirmation": "No",
        },
        {
            "actor": "red",
            "commands": "/dev ..., /shipping ..., /sales ..., /purchase ..., /warehouse ..., /accounting ..., /production ..., /cashier ..., /legal ..., /ingest ... +確認",
            "scope": "Department shortcuts plus explicit RAG sync commands.",
            "confirmation": "Required for write/sync paths.",
        },
        {
            "actor": "red",
            "commands": "Freeform Gemini conversation",
            "scope": "Full tool catalog, protected by tg_auth sensitive-tool gates.",
            "confirmation": "Required for sensitive tools.",
        },
        {
            "actor": "green",
            "commands": "/dev|/sample|/樣品室 profile|recipes|sample|samples|validate|draft|query.*",
            "scope": "Green sample room read/query helpers.",
            "confirmation": "No",
        },
        {
            "actor": "green",
            "commands": "/dev enqueue <json_payload> +確認",
            "scope": "Queue a Green Edge task only; ERP execution still waits for Edge-side approval.",
            "confirmation": "Yes",
        },
        {
            "actor": "orange",
            "commands": "/sales customer|active|alerts|quote|search|query.*",
            "scope": "Orange sales/customer intelligence queries.",
            "confirmation": "No",
        },
        {
            "actor": "blue",
            "commands": "/shipping shipments|shipment|eta|records|alerts|query.*",
            "scope": "Blue shipping status, ETD/ETA, AWB/B/L/container and email-lake records.",
            "confirmation": "No",
        },
        {
            "actor": "yellow",
            "commands": "/purchase profile|pos|po|eta|suppliers|alerts|risks|records|query.*",
            "scope": "Yellow procurement supplier, PO, ETA and procurement-record queries.",
            "confirmation": "No",
        },
        {
            "actor": "indigo",
            "commands": "/warehouse inventory|item|stock|alerts|records|query.*",
            "scope": "Indigo warehouse inventory, stock availability, alerts and warehouse-record queries.",
            "confirmation": "No",
        },
        {
            "actor": "purple",
            "commands": "/accounting summary|records|record|invoices|payments|remittance|alerts|query.*",
            "scope": "Purple accounting invoice, payment, remittance, statement and email-lake queries.",
            "confirmation": "No",
        },
        {
            "actor": "gray",
            "commands": "/production profile|status|history|query.*",
            "scope": "Gray production anomaly history and status queries.",
            "confirmation": "No",
        },
        {
            "actor": "gray",
            "commands": "/production report <json_payload> +確認",
            "scope": "Report a production anomaly and trigger cross-department coordination.",
            "confirmation": "Yes",
        },
        {
            "actor": "black",
            "commands": "/cashier summary|records|payments|receipts|alerts|query.*",
            "scope": "Black cashier cash-in/cash-out, payment, receipt and alert queries.",
            "confirmation": "No",
        },
        {
            "actor": "white",
            "commands": "/legal profile|specs|spec|version|compare|search|query.*",
            "scope": "White Legal SoT spec, contract, test report and indexed Drive document queries.",
            "confirmation": "No",
        },
    ]
    for agent in Agent:
        if agent is Agent.RED:
            continue
        rows.append({
            "actor": agent.value,
            "commands": "/dept <allowed-color> query.*",
            "scope": f"Read-only query targets: {_targets_text(agent)}.",
            "confirmation": "No",
        })
        rows.append({
            "actor": agent.value,
            "commands": "Freeform natural-language query",
            "scope": (
                "Read-only dept_nlp_query engine: query.* intents "
                f"({_targets_text(agent)}) + ACL-scoped RAG search. "
                "No full-tool Gemini session."
            ),
            "confirmation": "No",
        })
        rows.append({
            "actor": agent.value,
            "commands": "/ingest, email/file/sync tools",
            "scope": "Blocked from employee Telegram entry.",
            "confirmation": "N/A",
        })
    rows.append({
        "actor": "group chats",
        "commands": "/command, /command@BotName, @BotName mention",
        "scope": "Plain group chatter is ignored before rate-limit, downloads, or Gemini.",
        "confirmation": "Depends on command.",
    })
    return rows
