from __future__ import annotations


def _isolate_queue(task_queue, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(task_queue, "_QUEUE_FILE", str(tmp_path / "task_queue.json"))
    monkeypatch.setattr(task_queue, "_DLQ_FILE", str(tmp_path / "task_queue_dlq.json"))
    monkeypatch.setattr(task_queue, "_QUEUE_LOCK_FILE", str(tmp_path / "task_queue.json.lock"))
    task_queue._running_cancel_flags.clear()


def test_find_live_task_returns_pending_or_running_task(tmp_path, monkeypatch):
    from agent_core import task_queue

    _isolate_queue(task_queue, tmp_path, monkeypatch)
    out = task_queue.submit_task(
        "email_lake_rebuild",
        {"days_back": 365, "max_emails": 500},
        mutex_group="data_lake_writer",
    )
    assert "enqueued" in out

    live = task_queue.find_live_task("email_lake_rebuild", "data_lake_writer")
    assert live is not None
    assert live["tool"] == "email_lake_rebuild"
    assert live["kwargs"] == {"days_back": 365, "max_emails": 500}

    data = task_queue._load_queue()
    data["tasks"][0]["state"] = "done"
    task_queue._save_queue(data)
    assert task_queue.find_live_task("email_lake_rebuild", "data_lake_writer") is None


def test_telegram_email_lake_rebuild_enqueues_and_dedupes(tmp_path, monkeypatch):
    from agent_core import daemon_telegram, task_queue
    from agent_core.email_lake import email_lake_rebuild

    _isolate_queue(task_queue, tmp_path, monkeypatch)
    tools = daemon_telegram._queue_telegram_long_running_tools([email_lake_rebuild])
    wrapper = tools[0]

    assert wrapper is not email_lake_rebuild
    assert wrapper.__name__ == "email_lake_rebuild"

    first = wrapper(days_back=365, max_emails=500)
    assert "已排入背景隊列" in first
    assert first.data["deduped"] is False
    assert first.data["days_back"] == 365
    assert first.data["max_emails"] == 500

    data = task_queue._load_queue()
    assert len(data["tasks"]) == 1
    task = data["tasks"][0]
    assert task["tool"] == "email_lake_rebuild"
    assert task["kwargs"] == {"days_back": 365, "max_emails": 500}
    assert task["mutex_group"] == "data_lake_writer"
    assert task["timeout_sec"] == 3600

    second = wrapper(days_back=365, max_emails=500)
    assert "不重複啟動" in second
    assert second.data["deduped"] is True
    assert second.data["task_id"] == task["id"]
    assert len(task_queue._load_queue()["tasks"]) == 1
