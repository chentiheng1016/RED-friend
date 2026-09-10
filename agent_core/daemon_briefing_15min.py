"""Briefing 15-min daemon helper — 共用實作。

每 5 分鐘檢查未來 1 小時內會議；若有「12-20 分鐘後開始」的會議
（留 buffer 給 dispatcher 5 分鐘 jitter），推 briefing 到 Telegram。

避免重複推：state 記最近 _DEDUP_MAX_IDS 個 briefed event_id（list），
同會議只推一次。健檢 LOW 修補：原本只看「下一場」+ 單槽 dedup —— 兩場
相隔 <10 分鐘的連續會議，第二場等第一場離開窗口時自己也 <12 分鐘了，
永遠不被 brief。現在窗口內**每一場**都檢查、dedup 用 list。
"""
from __future__ import annotations

from typing import Any, Callable

# 推送窗口（會議開始前 N 分鐘內 push）
_PUSH_WINDOW_MIN = 12   # 早於這個就 skip（太早提醒）
_PUSH_WINDOW_MAX = 20   # 晚於這個就 skip（已經開始）

# dedup 帳本長度：窗口 8 分鐘寬、每 5 分鐘查一輪，同輪最多 brief 幾場
# 連續會議；留 8 個 event_id 綽綽有餘（也吃得下跨輪殘留）。
_DEDUP_MAX_IDS = 8


def find_upcoming_meetings(
    lookahead_hours: int = 1,
    *,
    get_service_fn: Callable[..., Any] | None = None,
) -> list[tuple[dict, int]]:
    """回 lookahead 內**所有**會議的 list[(event, minutes_until)]（升冪）。

    介面對齊 google_suite._find_next_meeting（它只回第一場）——生產呼叫端
    （launchd/scripts/briefing_15min.py）把這個傳給
    task_briefing_15min(find_upcoming_meetings_fn=...) 即可啟用多場 brief。
    get_service_fn 注入參數供測試 mock；預設走 google_auth.get_service。
    失敗回空 list（呼叫端視為「沒會議」）。
    """
    try:
        if get_service_fn is None:
            from agent_core.google_auth import get_service as get_service_fn  # type: ignore[no-redef]
        from datetime import datetime, timedelta, timezone
        service = get_service_fn("calendar", "v3")
        now = datetime.now(timezone.utc)
        res = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(hours=lookahead_hours)).isoformat(),
            maxResults=10, singleEvents=True, orderBy="startTime",
        ).execute()
        out: list[tuple[dict, int]] = []
        for ev in res.get("items", []) or []:
            start = ev.get("start") or {}
            start_raw = start.get("dateTime") or start.get("date")
            if not start_raw:
                continue
            try:
                if "T" in start_raw:
                    start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                else:
                    start_dt = datetime.fromisoformat(start_raw + "T00:00:00+08:00")
            except (ValueError, TypeError):
                continue
            out.append((ev, int((start_dt - now).total_seconds() / 60)))
        return out
    except Exception as e:
        print(f"[briefing_15min] find_upcoming_meetings 失敗：{e}")
        return []


def task_briefing_15min(
    *,
    find_next_meeting_fn: Callable[..., Any],
    meeting_briefing_fn: Callable[..., str],
    telegram_push_fn: Callable[[str], Any],
    load_state: Callable[[], dict[str, Any]],
    update_state: Callable[[Callable[[dict[str, Any]], None]], None],
    ts_fn: Callable[[], str],
    find_upcoming_meetings_fn: Callable[..., Any] | None = None,
) -> None:
    """窗口（12-20 分鐘後開始）內的會議 → 推 briefing。否則安靜。

    - find_upcoming_meetings_fn（回 list[(event, minutes_until)]）有注入時，
      窗口內**每一場**都會被 brief —— 連續會議（相隔 <10 分鐘）第二場
      不再漏掉。
    - 預設 None 退回舊行為：find_next_meeting_fn 只看下一場。
    - dedup：state["briefed_event_ids"] 最近 _DEDUP_MAX_IDS 個 event_id；
      向後相容讀舊單槽欄位 last_briefed_event_id。
    """
    candidates: list[tuple[Any, Any]] = []
    if find_upcoming_meetings_fn is not None:
        try:
            candidates = list(find_upcoming_meetings_fn(lookahead_hours=1) or [])
        except Exception as e:
            print(f"[briefing_15min] 找未來會議失敗：{e}")
            return
    else:
        try:
            event, minutes_until = find_next_meeting_fn(lookahead_hours=1)
        except Exception as e:
            print(f"[briefing_15min] 找下一場會議失敗：{e}")
            return
        if event and minutes_until is not None:
            candidates = [(event, minutes_until)]

    if not candidates:
        print("[briefing_15min] 1 小時內沒會議，skip")
        return

    in_window = [
        (ev, mins) for ev, mins in candidates
        if ev and mins is not None
        and _PUSH_WINDOW_MIN <= mins <= _PUSH_WINDOW_MAX
    ]
    if not in_window:
        mins_str = ", ".join(str(m) for _, m in candidates)
        print(f"[briefing_15min] 會議在 {mins_str} 分鐘後，"
              f"不在 {_PUSH_WINDOW_MIN}-{_PUSH_WINDOW_MAX}min 窗口，skip")
        return

    state = load_state()
    briefed_ids = [x for x in (state.get("briefed_event_ids") or []) if x]
    legacy_id = state.get("last_briefed_event_id", "")
    if legacy_id and legacy_id not in briefed_ids:
        briefed_ids.append(legacy_id)  # 舊單槽欄位也算已推（升級不重推）

    for event, minutes_until in in_window:
        event_id = event.get("id", "")
        title = event.get("summary", "(無標題)")
        if not event_id:
            # 沒 id 沒法 dedup（也沒法產 briefing）——跳過，維持舊行為
            print(f"[briefing_15min] 會議無 event_id（{title}），skip")
            continue
        if event_id in briefed_ids:
            print(f"[briefing_15min] 已推過 {event_id[:20]}... ({title})，skip")
            continue

        print(f"[briefing_15min] 🔔 推送 briefing：{title}（{minutes_until} 分鐘後開始）")
        try:
            briefing = meeting_briefing_fn(
                event_id=event_id, lookback_days=30, push_telegram=False,
            )
        except Exception as e:
            print(f"[briefing_15min] 產 briefing 失敗：{e}")
            continue

        header = f"🔔 15 分鐘後會議：{title}\n{'=' * 40}\n\n"
        try:
            telegram_push_fn((header + briefing)[:3900])
            print("[briefing_15min] ✅ 已推 Telegram")
        except Exception as e:
            print(f"[briefing_15min] Telegram push 失敗：{e}")
            continue

        briefed_ids.append(event_id)

        def _upd(st, _eid=event_id):
            ids = [x for x in (st.get("briefed_event_ids") or []) if x]
            legacy = st.get("last_briefed_event_id", "")
            if legacy and legacy not in ids:
                ids.append(legacy)
            if _eid not in ids:
                ids.append(_eid)
            st["briefed_event_ids"] = ids[-_DEDUP_MAX_IDS:]
            # 向後相容：舊欄位繼續寫（降版 / 其他讀者不會壞）
            st["last_briefed_event_id"] = _eid
            st["last_briefed_at"] = ts_fn()
        update_state(_upd)
