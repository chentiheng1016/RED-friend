"""部門間呼叫的唯一入口 — 強制走 permission matrix.

所有 agent 不得直接 import 別 agent 的模組；要查別部門的資料必須透過
PermissionMiddleware.dispatch()。這個約束讓未來：
  - 替換成 Redis / gRPC bus 時只改一處
  - 加 audit log / metrics / rate limit 時集中
  - 拒絕未授權呼叫有單一檢查點
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from agent_core.agents.permission_matrix import Agent, can_query


logger = logging.getLogger("agent_core.agents.middleware")


class PermissionDenied(Exception):
    """caller 試圖呼叫 matrix 不允許的 target。"""


@dataclass(frozen=True)
class AgentRequest:
    caller: Agent
    target: Agent
    intent: str                 # e.g. "query.sample_status"
    payload: Mapping[str, Any]
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)


_CURRENT_AGENT_REQUEST = contextvars.ContextVar("current_agent_request", default=None)


def current_agent_request() -> AgentRequest | None:
    return _CURRENT_AGENT_REQUEST.get()


@contextlib.contextmanager
def agent_request_context(req: AgentRequest):
    """把 req 設為當前 AgentRequest context（與 dispatch 內部同一 contextvar）。

    給 dispatch 之外的呼叫端用 —— 例如員工 Telegram 自由對話把工具執行包進
    caller=<部門色> 的 context，讓 rag_gateway.current_request_caller 的
    per-color 資料 ACL 生效（否則預設 Agent.RED = SUPER_ADMIN 視野）。
    """
    token = _CURRENT_AGENT_REQUEST.set(req)
    try:
        yield req
    finally:
        _CURRENT_AGENT_REQUEST.reset(token)


def _audit(
    *,
    event_type: str,
    severity: str,
    detail: str,
    req: AgentRequest,
    elapsed_ms: Optional[float] = None,
    error: Optional[BaseException] = None,
) -> None:
    """Best-effort BigQuery audit. Never blocks dispatch on logging failure.

    Why this exists:
      * Before this hook, only `gray_production.anomaly` wrote to the BigQuery
        exception_log. Every other `command.*` (state-changing) call across all
        9 agents went un-audited — so a Red GM reviewing weekly couldn't see
        who-changed-what.
      * Centralising the hook here means individual agents don't have to
        remember to log; auditing happens automatically for every dispatch.

    What gets logged:
      * `event_type=command_audit` for command.* dispatches (success or failure).
      * `event_type=permission_denied` for any intent that fails the matrix
        check — a security-relevant signal worth keeping even on read-only
        attempts.

    Payload values are NOT logged (only top-level keys), because payloads can
    contain customer data, file paths, raw text, or PII. The trace_id is the
    join key if the full payload is needed from in-process logs.
    """
    # Lazy import — exception_logger is optional and only configured when
    # BIGQUERY_PROJECT_ID is set; avoid import-time coupling for tests / CI.
    try:
        from agent_core.exception_logger import log_event
    except Exception:
        return
    try:
        payload_keys: list = []
        if isinstance(req.payload, Mapping):
            payload_keys = list(req.payload.keys())
        extra: dict[str, Any] = {
            "caller": req.caller.value,
            "target": req.target.value,
            "intent": req.intent,
            "payload_keys": payload_keys,
        }
        if elapsed_ms is not None:
            extra["elapsed_ms"] = round(elapsed_ms, 1)
        if error is not None:
            extra["error_type"] = type(error).__name__
        log_event(
            event_type=event_type,
            source_agent=req.caller.value,
            severity=severity,
            detail=detail,
            trace_id=req.trace_id,
            extra=extra,
        )
    except Exception as exc:
        # Auditing must never break the request path.
        logger.warning("audit log failed (event=%s trace=%s): %s",
                       event_type, req.trace_id, exc)


class PermissionMiddleware:
    """In-process dispatcher。registry 是 color → BaseAgent 的查表（注入）。

    用法：
        mw = PermissionMiddleware(registry)
        result = mw.dispatch(AgentRequest(caller=Agent.ORANGE, target=Agent.GREEN,
                                          intent="query.sample_status",
                                          payload={"sp_id": "SP-123"}))
    """

    def __init__(self, registry: "AgentRegistryProtocol") -> None:
        self._registry = registry

    def dispatch(self, req: AgentRequest) -> Any:
        if not can_query(req.caller, req.target):
            logger.warning(
                "permission_denied caller=%s target=%s intent=%s trace=%s",
                req.caller.value, req.target.value, req.intent, req.trace_id,
            )
            # Audit denials regardless of intent prefix — even a denied query is
            # a meaningful security signal (someone is probing the matrix).
            _audit(
                event_type="permission_denied",
                severity="warning",
                detail=(
                    f"permission_denied: {req.caller.value} → "
                    f"{req.target.value} / {req.intent}"
                ),
                req=req,
            )
            raise PermissionDenied(
                f"{req.caller.value} → {req.target.value} not permitted "
                f"(intent={req.intent})"
            )

        target_agent = self._registry.get(req.target)
        if target_agent is None:
            raise LookupError(
                f"target agent {req.target.value} not registered "
                f"(intent={req.intent}, trace={req.trace_id})"
            )

        logger.info(
            "dispatch caller=%s target=%s intent=%s trace=%s",
            req.caller.value, req.target.value, req.intent, req.trace_id,
        )
        # We audit `command.*` (state-changing) intents but NOT `query.*` —
        # queries are read-only and high-frequency; auditing them all would
        # flood BigQuery and obscure the meaningful state-change trail.
        is_command = req.intent.startswith("command.")
        t0 = time.monotonic()
        err: Optional[BaseException] = None
        token = _CURRENT_AGENT_REQUEST.set(req)
        try:
            return target_agent.handle_query(
                intent=req.intent, payload=req.payload, trace_id=req.trace_id,
            )
        except BaseException as exc:
            err = exc
            raise
        finally:
            _CURRENT_AGENT_REQUEST.reset(token)
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            logger.debug(
                "dispatch_done target=%s intent=%s elapsed_ms=%.1f trace=%s",
                req.target.value, req.intent, elapsed_ms, req.trace_id,
            )
            if is_command:
                if err is None:
                    _audit(
                        event_type="command_audit",
                        severity="info",
                        detail=(
                            f"command_dispatched: {req.caller.value} → "
                            f"{req.target.value} / {req.intent} "
                            f"({elapsed_ms:.0f}ms)"
                        ),
                        req=req,
                        elapsed_ms=elapsed_ms,
                    )
                else:
                    # Codex P2: do NOT inline str(err) — exception messages can
                    # echo back user-controlled / sensitive payload content
                    # (e.g. file paths, customer fields, raw text from a bad
                    # input). Detail keeps only the exception **class name**;
                    # the canonical structured field is already in
                    # extra["error_type"]. trace_id is the join key if a
                    # human needs the full message from in-process logs.
                    _audit(
                        event_type="command_audit",
                        severity="error",
                        detail=(
                            f"command_failed: {req.caller.value} → "
                            f"{req.target.value} / {req.intent} "
                            f"[{type(err).__name__}]"
                        ),
                        req=req,
                        elapsed_ms=elapsed_ms,
                        error=err,
                    )


class AgentRegistryProtocol:
    """Structural protocol — 真正實作在 registry.py，這裡只是型別 hint。"""

    def get(self, color: Agent) -> Optional["BaseAgentProtocol"]:  # noqa: F821
        raise NotImplementedError


class BaseAgentProtocol:  # noqa: D401 — structural only
    """handle_query 的最小介面；具體在 base_agent.py。"""

    def handle_query(self, intent: str, payload: Mapping[str, Any], trace_id: str) -> Any:
        raise NotImplementedError
