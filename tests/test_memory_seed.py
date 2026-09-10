"""memory_seed：版本控管的 seed 事實同步機制。

涵蓋：
  - KV 合併把 seed 寫進 memory.json，seed 對它的 key 優先
  - 執行期才有的 key（不在 seed）保持不動
  - marker 讓重複呼叫變 no-op（idempotent）
  - seed 內容變了 → 重新套用
  - 向量索引 best-effort：index fn 回空字串（向量庫沒就緒）時不寫 vec marker，
    下次重試
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest


@pytest.fixture
def seed_env(monkeypatch):
    """把 memory_seed 的三個路徑都指到 tmp，避免污染真實 var/ 與 repo。"""
    from agent_core import memory_seed

    tmp = tempfile.mkdtemp(prefix="red_seed_")
    seed_path = os.path.join(tmp, "memory_seed.json")
    mem_path = os.path.join(tmp, "memory.json")
    marker_path = os.path.join(tmp, ".memory_seed_synced")

    monkeypatch.setenv("RED_MEMORY_SEED_FILE", seed_path)
    monkeypatch.setattr(memory_seed, "MEMORY_FILE", mem_path)
    monkeypatch.setattr(memory_seed, "_MARKER_FILE", marker_path)
    monkeypatch.setattr(memory_seed, "STATE_DIR", tmp)

    def write_seed(d: dict):
        with open(seed_path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)

    def read_mem() -> dict:
        if not os.path.isfile(mem_path):
            return {}
        with open(mem_path, "r", encoding="utf-8") as f:
            return json.load(f)

    yield {
        "module": memory_seed,
        "seed_path": seed_path,
        "mem_path": mem_path,
        "marker_path": marker_path,
        "write_seed": write_seed,
        "read_mem": read_mem,
    }
    shutil.rmtree(tmp, ignore_errors=True)


def test_kv_merge_writes_seed_facts(seed_env):
    ms = seed_env["module"]
    seed_env["write_seed"]({"JAIFUNG": "佳紘有限公司"})

    summary = ms.sync_memory_seed()

    assert summary["kv_applied"] == 1
    assert seed_env["read_mem"]() == {"JAIFUNG": "佳紘有限公司"}


def test_seed_wins_over_existing_value(seed_env):
    ms = seed_env["module"]
    # 執行期 memory 已有一個 stale 值
    with open(seed_env["mem_path"], "w", encoding="utf-8") as f:
        json.dump({"JAIFUNG": "佳豐（錯的）", "其他": "保留我"}, f, ensure_ascii=False)
    seed_env["write_seed"]({"JAIFUNG": "佳紘有限公司"})

    ms.sync_memory_seed()

    mem = seed_env["read_mem"]()
    assert mem["JAIFUNG"] == "佳紘有限公司"   # seed 覆蓋 stale 值
    assert mem["其他"] == "保留我"            # 非 seed key 不動


def test_idempotent_second_call_is_noop(seed_env):
    ms = seed_env["module"]
    seed_env["write_seed"]({"JAIFUNG": "佳紘有限公司"})

    first = ms.sync_memory_seed()
    second = ms.sync_memory_seed()

    assert first["kv_applied"] == 1
    assert second["kv_applied"] == 0   # marker hash 相同 → 不重做


def test_reapplies_when_seed_changes(seed_env):
    ms = seed_env["module"]
    seed_env["write_seed"]({"JAIFUNG": "佳紘有限公司"})
    ms.sync_memory_seed()

    # 模擬 git pull 帶進更新後的 seed
    seed_env["write_seed"]({"JAIFUNG": "佳紘有限公司", "新事實": "deca 2026-06-13 出口"})
    summary = ms.sync_memory_seed()

    assert summary["kv_applied"] == 1   # 只套新增/變動的那筆
    assert seed_env["read_mem"]()["新事實"] == "deca 2026-06-13 出口"


def test_missing_seed_is_noop(seed_env):
    ms = seed_env["module"]
    # 沒寫 seed 檔
    summary = ms.sync_memory_seed()
    assert summary["skipped"] is True
    assert not os.path.isfile(seed_env["mem_path"])


def test_corrupt_seed_is_noop(seed_env):
    ms = seed_env["module"]
    with open(seed_env["seed_path"], "w", encoding="utf-8") as f:
        f.write("{ not valid json ")
    summary = ms.sync_memory_seed()
    assert summary["skipped"] is True


def test_vector_index_called_and_marker_set(seed_env):
    ms = seed_env["module"]
    seed_env["write_seed"]({"JAIFUNG": "佳紘有限公司"})

    calls = []

    def fake_index(text, source, metadata=None, doc_id=None):
        calls.append({"text": text, "source": source,
                      "metadata": metadata, "doc_id": doc_id})
        return doc_id  # 模擬成功

    summary = ms.sync_memory_seed(index_memory_fn=fake_index)

    assert summary["vec_applied"] == 1
    assert calls[0]["source"] == "seed"
    assert calls[0]["doc_id"] == "seed-JAIFUNG"
    assert "佳紘有限公司" in calls[0]["text"]
    # vec marker 已寫 → 再呼叫不重 index
    calls.clear()
    ms.sync_memory_seed(index_memory_fn=fake_index)
    assert calls == []


def test_vector_index_unavailable_retries_next_time(seed_env):
    ms = seed_env["module"]
    seed_env["write_seed"]({"JAIFUNG": "佳紘有限公司"})

    def failing_index(text, source, metadata=None, doc_id=None):
        return ""   # 向量庫沒就緒

    ms.sync_memory_seed(index_memory_fn=failing_index)

    # vec marker 不該被寫（因為 index 失敗）→ 換成會成功的 fn 仍會再試
    calls = []

    def ok_index(text, source, metadata=None, doc_id=None):
        calls.append(doc_id)
        return doc_id

    summary = ms.sync_memory_seed(index_memory_fn=ok_index)
    assert summary["vec_applied"] == 1
    assert calls == ["seed-JAIFUNG"]
