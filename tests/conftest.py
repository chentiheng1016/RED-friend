"""Pytest 全域 fixture：避免測試副作用污染真實 var/。

目前主要保護：
  - tool_budgets._BUDGET_DIR — 否則任何 wrap_sensitive_tool DANGEROUS test
    會在真實 var/data/tool_budgets/YYYY-MM-DD.json 增加 counter。
  - tg_auth._confirm_state / _dangerous_confirm_state — 跨 test 殘留的
    confirm token 會讓後續 test 莫名其妙過得太順。
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolate_tool_budgets():
    """每 test 用獨立 tmp dir 存 budget — 不污染真實狀態。"""
    try:
        from agent_core import tool_budgets
    except Exception:
        yield
        return
    tmp = tempfile.mkdtemp(prefix="red_budget_")
    orig = tool_budgets._BUDGET_DIR
    tool_budgets._BUDGET_DIR = tmp
    try:
        yield
    finally:
        tool_budgets._BUDGET_DIR = orig
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_policy_log():
    """每 test 隔離 policy_engine 的 _LOG_FILE — 不污染真實狀態。"""
    try:
        from agent_core import policy_engine
    except Exception:
        yield
        return
    tmp = tempfile.mkdtemp(prefix="red_policy_")
    orig = policy_engine._LOG_FILE
    policy_engine._LOG_FILE = os.path.join(tmp, "policy_decisions.jsonl")
    # 同時清掉跨 test 殘留的 env override
    saved_env = {}
    for k in ("RED_BLOCK_TOOL", "RED_FORCE_DRY_RUN_FOR",
              "RED_RAISE_TIER_TO_DANGEROUS"):
        if k in os.environ:
            saved_env[k] = os.environ.pop(k)
    try:
        yield
    finally:
        policy_engine._LOG_FILE = orig
        for k, v in saved_env.items():
            os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_work_mode():
    """每 test 隔離 mode_manager 的 state file — 不污染真實狀態。"""
    try:
        from agent_core import mode_manager
    except Exception:
        yield
        return
    tmp = tempfile.mkdtemp(prefix="red_workmode_")
    orig_mode = mode_manager._MODE_FILE
    orig_hist = mode_manager._HISTORY_FILE
    mode_manager._MODE_FILE = os.path.join(tmp, "work_mode.json")
    mode_manager._HISTORY_FILE = os.path.join(tmp, "work_mode_history.jsonl")
    try:
        yield
    finally:
        mode_manager._MODE_FILE = orig_mode
        mode_manager._HISTORY_FILE = orig_hist
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_intent_log():
    """每 test 隔離 intent_router 的 _LOG_FILE — 不污染真實狀態。"""
    try:
        from agent_core import intent_router
    except Exception:
        yield
        return
    tmp = tempfile.mkdtemp(prefix="red_intent_")
    orig = intent_router._LOG_FILE
    intent_router._LOG_FILE = os.path.join(tmp, "intent_log.jsonl")
    try:
        yield
    finally:
        intent_router._LOG_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_task_memory():
    """每 test 用獨立 tmp 路徑存 task_memory.json — 不污染真實狀態。"""
    try:
        from agent_core import task_memory
    except Exception:
        yield
        return
    tmp = tempfile.mkdtemp(prefix="red_taskmem_")
    orig = task_memory._TASK_FILE
    task_memory._TASK_FILE = os.path.join(tmp, "task_memory.json")
    try:
        yield
    finally:
        task_memory._TASK_FILE = orig
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_task_queue():
    """每 test 用獨立 tmp 路徑存 queue / DLQ — 不污染真實狀態。"""
    try:
        from agent_core import task_queue
    except Exception:
        yield
        return
    tmp = tempfile.mkdtemp(prefix="red_queue_")
    orig_q = task_queue._QUEUE_FILE
    orig_d = task_queue._DLQ_FILE
    task_queue._QUEUE_FILE = os.path.join(tmp, "task_queue.json")
    task_queue._DLQ_FILE = os.path.join(tmp, "task_queue_dlq.json")
    # 清掉 module-level cancel-flag dict 防跨 test 污染
    task_queue._running_cancel_flags.clear()
    try:
        yield
    finally:
        task_queue._QUEUE_FILE = orig_q
        task_queue._DLQ_FILE = orig_d
        task_queue._running_cancel_flags.clear()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_confirm_state():
    """每 test 結束清掉 tg_auth 的 confirm state，避免跨 test 殘留。"""
    yield
    try:
        from agent_core import tg_auth
        with tg_auth._state_lock:
            tg_auth._confirm_state.clear()
            tg_auth._dangerous_confirm_state.clear()
            tg_auth._confirm_history.clear()
            tg_auth._lockout_until.clear()
    except Exception:
        pass
