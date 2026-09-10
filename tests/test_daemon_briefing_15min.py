"""briefing_15min 連續會議漏 brief（健檢 LOW）。

原本只看「下一場」會議 + 單槽 dedup（last_briefed_event_id）：兩場相隔
<10 分鐘時，第二場等第一場離開 12-20min 窗口後自己也 <12 分鐘了，永遠
不被 brief。修法：注入 find_upcoming_meetings_fn 看窗口內**所有**會議、
dedup 改最近 N 個 event_id 的 list（預設 None 退回舊單場行為）。
"""
import unittest

from agent_core.daemon_briefing_15min import (
    _DEDUP_MAX_IDS,
    find_upcoming_meetings,
    task_briefing_15min,
)


def _ev(eid: str, title: str = "") -> dict:
    return {"id": eid, "summary": title or f"會議{eid}"}


class _Harness:
    """依賴注入測試鷹架：記錄推送 / briefing 呼叫，state 是真 dict。"""

    def __init__(self, state: dict | None = None,
                 briefing_fail_ids: set | None = None):
        self.state = state if state is not None else {}
        self.pushed: list[str] = []
        self.briefed_ids: list[str] = []
        self._fail = briefing_fail_ids or set()

    def meeting_briefing(self, *, event_id, lookback_days, push_telegram):
        if event_id in self._fail:
            raise RuntimeError(f"briefing fail {event_id}")
        self.briefed_ids.append(event_id)
        return f"brief-{event_id}"

    def run(self, *, upcoming=None, find_next=None):
        task_briefing_15min(
            find_next_meeting_fn=find_next or (lambda lookahead_hours: (None, None)),
            meeting_briefing_fn=self.meeting_briefing,
            telegram_push_fn=self.pushed.append,
            load_state=lambda: self.state,
            update_state=lambda fn: fn(self.state),
            ts_fn=lambda: "2026-07-11T08:00:00",
            find_upcoming_meetings_fn=(
                (lambda lookahead_hours: upcoming) if upcoming is not None else None),
        )


class MultiMeetingWindowTests(unittest.TestCase):
    def test_back_to_back_meetings_both_briefed_same_run(self):
        h = _Harness()
        h.run(upcoming=[(_ev("A"), 14), (_ev("B"), 19)])
        self.assertEqual(h.briefed_ids, ["A", "B"])
        self.assertEqual(len(h.pushed), 2)
        self.assertEqual(h.state["briefed_event_ids"], ["A", "B"])
        self.assertEqual(h.state["last_briefed_event_id"], "B")

    def test_back_to_back_second_meeting_briefed_on_later_run(self):
        """核心修補場景：A 10:00 開、B 10:05 開（相隔 <10 分）。
        第一輪只有 A 在窗口；第二輪 A 已 <12min 出窗、B 進窗 —— B 必須被 brief
        （修前單槽 dedup + 只看下一場，B 永遠漏掉）。"""
        h = _Harness()
        # T-14min 檢查輪：A=14（窗內）、B=19（也窗內？不，本場景設 B=21 出窗）
        h.run(upcoming=[(_ev("A"), 14), (_ev("B"), 21)])
        self.assertEqual(h.briefed_ids, ["A"])
        # 5 分鐘後下一輪：A=9（出窗）、B=16（進窗）
        h.run(upcoming=[(_ev("A"), 9), (_ev("B"), 16)])
        self.assertEqual(h.briefed_ids, ["A", "B"])
        self.assertEqual(len(h.pushed), 2)

    def test_same_meeting_not_briefed_twice(self):
        h = _Harness()
        h.run(upcoming=[(_ev("A"), 14)])
        h.run(upcoming=[(_ev("A"), 12)])  # 下一輪同場仍在窗內
        self.assertEqual(h.briefed_ids, ["A"])
        self.assertEqual(len(h.pushed), 1)

    def test_out_of_window_meetings_skipped(self):
        h = _Harness()
        h.run(upcoming=[(_ev("A"), 5), (_ev("B"), 30)])
        self.assertEqual(h.pushed, [])
        self.assertNotIn("briefed_event_ids", h.state)

    def test_legacy_single_slot_state_respected(self):
        # 升級當下舊 state 只有 last_briefed_event_id：不能重推那一場
        h = _Harness(state={"last_briefed_event_id": "A"})
        h.run(upcoming=[(_ev("A"), 14), (_ev("B"), 18)])
        self.assertEqual(h.briefed_ids, ["B"])
        self.assertIn("A", h.state["briefed_event_ids"])
        self.assertIn("B", h.state["briefed_event_ids"])

    def test_dedup_ledger_trimmed_to_max(self):
        old_ids = [f"old{i}" for i in range(_DEDUP_MAX_IDS)]
        h = _Harness(state={"briefed_event_ids": list(old_ids)})
        h.run(upcoming=[(_ev("NEW"), 15)])
        ids = h.state["briefed_event_ids"]
        self.assertEqual(len(ids), _DEDUP_MAX_IDS)
        self.assertEqual(ids[-1], "NEW")
        self.assertNotIn("old0", ids)  # 最舊的被擠掉

    def test_briefing_failure_does_not_mark_briefed(self):
        # A 產 briefing 失敗 → 不入 dedup（下一輪還有機會補推）；B 照推
        h = _Harness(briefing_fail_ids={"A"})
        h.run(upcoming=[(_ev("A"), 14), (_ev("B"), 18)])
        self.assertEqual(h.briefed_ids, ["B"])
        self.assertNotIn("A", h.state.get("briefed_event_ids", []))

    def test_push_failure_does_not_mark_briefed(self):
        h = _Harness()
        boom = RuntimeError("tg down")

        def bad_push(msg):
            raise boom
        task_briefing_15min(
            find_next_meeting_fn=lambda lookahead_hours: (None, None),
            meeting_briefing_fn=h.meeting_briefing,
            telegram_push_fn=bad_push,
            load_state=lambda: h.state,
            update_state=lambda fn: fn(h.state),
            ts_fn=lambda: "t",
            find_upcoming_meetings_fn=lambda lookahead_hours: [(_ev("A"), 14)],
        )
        self.assertNotIn("briefed_event_ids", h.state)

    def test_event_without_id_skipped(self):
        h = _Harness()
        h.run(upcoming=[({"summary": "無 id 會議"}, 14)])
        self.assertEqual(h.pushed, [])


class LegacySingleMeetingFallbackTests(unittest.TestCase):
    """find_upcoming_meetings_fn=None → 舊行為（只看下一場）。"""

    def test_next_meeting_in_window_briefed_once(self):
        h = _Harness()
        h.run(find_next=lambda lookahead_hours: (_ev("A"), 15))
        h.run(find_next=lambda lookahead_hours: (_ev("A"), 13))
        self.assertEqual(h.briefed_ids, ["A"])
        self.assertEqual(len(h.pushed), 1)

    def test_no_meeting_is_quiet(self):
        h = _Harness()
        h.run(find_next=lambda lookahead_hours: (None, None))
        self.assertEqual(h.pushed, [])

    def test_out_of_window_is_quiet(self):
        h = _Harness()
        h.run(find_next=lambda lookahead_hours: (_ev("A"), 45))
        self.assertEqual(h.pushed, [])

    def test_finder_exception_is_quiet(self):
        def boom(lookahead_hours):
            raise RuntimeError("calendar down")
        h = _Harness()
        h.run(find_next=boom)
        self.assertEqual(h.pushed, [])


class FindUpcomingMeetingsTests(unittest.TestCase):
    """生產注入用 finder：mock get_service_fn 驗證回傳所有會議。"""

    def _fake_service(self, items):
        from unittest import mock as m
        service = m.MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {
            "items": items}
        return service

    def test_returns_all_events_with_minutes(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        items = [
            {"id": "A", "start": {"dateTime": (now + timedelta(minutes=15)).isoformat()}},
            {"id": "B", "start": {"dateTime": (now + timedelta(minutes=19)).isoformat()}},
            {"id": "C", "start": {}},  # 無起始時間 → 跳過
        ]
        out = find_upcoming_meetings(
            lookahead_hours=1,
            get_service_fn=lambda *a: self._fake_service(items))
        self.assertEqual([ev["id"] for ev, _ in out], ["A", "B"])
        self.assertAlmostEqual(out[0][1], 14, delta=1)  # int() 截尾 → 14 或 15
        self.assertAlmostEqual(out[1][1], 18, delta=1)

    def test_service_failure_returns_empty(self):
        def boom(*a):
            raise RuntimeError("oauth down")
        self.assertEqual(find_upcoming_meetings(get_service_fn=boom), [])


if __name__ == "__main__":
    unittest.main()
