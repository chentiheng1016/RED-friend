"""Google Calendar + Drive user-facing tools.

Both are thin wrappers that call get_service() from google_auth, so the
module is tiny and fully self-contained.

Drive functions delegate to the top-level drive_ops module.
"""
import os
from datetime import datetime, timezone, timedelta

from agent_core import drive_ops

from agent_core.google_auth import get_service, _get_media_file_upload
from agent_core.logging_and_paths import logger
from agent_core.prompt_injection import sanitize_for_llm


def _clean_path(p):
    return os.path.expanduser(p.strip().strip("'").strip('"'))


def list_calendar_events(max_results: int = 5):
    """列出 Google 行事曆接下來的 N 個活動（含時間、主旨、地點）。"""
    print("\n[系統日誌] 📅 讀取行事曆中...")
    try:
        service = get_service('calendar', 'v3')
        now = datetime.now(timezone.utc).isoformat()
        events_result = service.events().list(
            calendarId='primary', timeMin=now, maxResults=max_results,
            singleEvents=True, orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        if not events:
            return "接下來沒有任何行程。"
        output = "您的近期行程如下：\n"
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            # 行事曆標題是 attacker-controllable（Google 把別人 email 來的會議邀請自動
            # 加進主行事曆）→ 餵 LLM 前淨化，擋零點擊 prompt injection（健檢 High）。
            title = sanitize_for_llm(event.get('summary', '（無標題）'))
            output += f"- {start}: {title} (ID: {event['id']})\n"
        return output
    except Exception as e:
        return f"行事曆讀取失敗：{e}"


def create_calendar_event(summary: str, start_time: str, end_time: str, location: str = None, description: str = None):
    """建一個 Google 行事曆活動。start_time / end_time 用 ISO 格式 '2026-04-18T14:30:00+08:00'。"""
    print(f"\n[系統日誌] 📅 建立行程：{summary}...")
    try:
        service = get_service('calendar', 'v3')
        event = {
            'summary': summary, 'location': location, 'description': description,
            'start': {'dateTime': start_time, 'timeZone': 'Asia/Taipei'},
            'end': {'dateTime': end_time, 'timeZone': 'Asia/Taipei'},
        }
        created = service.events().insert(calendarId='primary', body=event).execute()
        return f"行程已成功建立！(ID: {created.get('id', '未知')})"
    except Exception as e:
        return f"行程建立失敗：{e}"


def _shift_end_keeping_duration(old_start_iso, old_end_iso, new_start_iso):
    """把結束時間依原時長平移到新的開始時間；任何一段解析不了就回 None。

    回 None 時呼叫端不送 end，交給 Google 自己判（多半會因 end < start 退回，
    那個錯誤訊息比我們亂猜一個結束時間誠實）。
    """
    try:
        old_s = datetime.fromisoformat(str(old_start_iso).replace('Z', '+00:00'))
        old_e = datetime.fromisoformat(str(old_end_iso).replace('Z', '+00:00'))
        new_s = datetime.fromisoformat(str(new_start_iso).replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        return None
    duration = old_e - old_s
    if duration.total_seconds() <= 0:
        return None
    return (new_s + duration).isoformat()


def _given(value):
    """欄位有沒有被指定。空字串/全空白視同沒給 —— LLM 常把「不改」填成 ""，
    當成「清空這個欄位」會靜默砍掉使用者的資料。本工具不支援清空欄位。"""
    return value is not None and str(value).strip() != ""


def update_calendar_event(event_id: str, summary: str = None, start_time: str = None,
                          end_time: str = None, location: str = None,
                          description: str = None):
    """改既有 Google 行事曆活動。event_id 從 list_calendar_events 取得。

    只改**有傳進來**的欄位；沒傳的保持原樣（走 events().patch()，不是 update()
    —— 後者會把 body 裡沒帶到的欄位整個清掉）。時間用 ISO 格式
    '2026-08-28T15:00:00+08:00'。

    只給 start_time 不給 end_time 時，結束時間會依**原本的時長**一併平移
    （「把三點的會改到四點」是最常見的用法；只改開始時間會讓 Google 因為
    end 早於 start 直接退回）。

    不支援：清空欄位（空字串視同「不改」）、改整天活動的日期（請用
    delete_calendar_event + create_calendar_event 重建）。
    """
    print(f"\n[系統日誌] 📅 更新行程 ID: {event_id}...")
    if not str(event_id or "").strip():
        return "行程更新失敗：event_id 不能空（先用 list_calendar_events 取得）。"
    if not any(_given(v) for v in (summary, start_time, end_time, location, description)):
        return "行程更新失敗：沒有指定任何要改的欄位。"
    try:
        service = get_service('calendar', 'v3')
        before = service.events().get(
            calendarId='primary', eventId=event_id).execute()
    except Exception as e:
        return f"行程更新失敗：讀不到這個行程（{e}）"

    old_start = before.get('start') or {}
    old_end = before.get('end') or {}
    if not old_start.get('dateTime') and (_given(start_time) or _given(end_time)):
        return ("行程更新失敗：這是整天活動（沒有具體時間），本工具不支援改它的日期。"
                "請用 delete_calendar_event + create_calendar_event 重建。")

    body = {}
    if _given(summary):
        body['summary'] = summary
    if _given(location):
        body['location'] = location
    if _given(description):
        body['description'] = description
    if _given(start_time):
        body['start'] = {'dateTime': start_time, 'timeZone': 'Asia/Taipei'}
    shifted = ""
    if _given(end_time):
        body['end'] = {'dateTime': end_time, 'timeZone': 'Asia/Taipei'}
    elif _given(start_time):
        new_end = _shift_end_keeping_duration(
            old_start.get('dateTime'), old_end.get('dateTime'), start_time)
        if new_end:
            body['end'] = {'dateTime': new_end, 'timeZone': 'Asia/Taipei'}
            shifted = "（結束時間依原時長一併平移）"

    try:
        updated = service.events().patch(
            calendarId='primary', eventId=event_id, body=body).execute()
    except Exception as e:
        return f"行程更新失敗：{e}"

    # 行事曆的標題/地點/說明都是 attacker-controllable（Google 把別人 email 來的
    # 會議邀請自動加進主行事曆）→ 回給 LLM 前一律淨化，同 list_calendar_events。
    changes = []
    for label, path in (("標題", ('summary',)), ("地點", ('location',)),
                        ("說明", ('description',))):
        was, now = before.get(path[0]) or "", updated.get(path[0]) or ""
        if was != now:
            changes.append(f"{label}：{sanitize_for_llm(was) or '（空）'}"
                           f" → {sanitize_for_llm(now) or '（空）'}")
    for label, key in (("開始", 'start'), ("結束", 'end')):
        was = (before.get(key) or {}).get('dateTime') or ""
        now = (updated.get(key) or {}).get('dateTime') or ""
        if was != now:
            changes.append(f"{label}：{was} → {now}")
    title = sanitize_for_llm(updated.get('summary', '（無標題）'))
    detail = "；".join(changes) if changes else "（送出的值與原本相同，沒有實際變更）"
    return f"✅ 行程已更新{shifted}：{title}\n  {detail}\n  (ID: {event_id})"


def delete_calendar_event(event_id: str):
    """刪除 Google 行事曆活動。event_id 從 list_calendar_events 取得。"""
    print(f"\n[系統日誌] 📅 刪除行程 ID: {event_id}...")
    try:
        service = get_service('calendar', 'v3')
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        return "行程已成功刪除。"
    except Exception as e:
        return f"行程刪除失敗：{e}"


def _find_next_meeting(lookahead_hours: int = 72):
    """回傳 (event dict, minutes_until_start)；若沒找到回 (None, None)。"""
    try:
        service = get_service('calendar', 'v3')
        now = datetime.now(timezone.utc)
        tmin = now.isoformat()
        tmax = (now + timedelta(hours=lookahead_hours)).isoformat()
        res = service.events().list(
            calendarId='primary', timeMin=tmin, timeMax=tmax,
            maxResults=5, singleEvents=True, orderBy='startTime'
        ).execute()
        events = res.get('items', [])
        if not events:
            return None, None
        for ev in events:
            start_raw = ev['start'].get('dateTime') or ev['start'].get('date')
            if not start_raw:
                continue
            try:
                if 'T' in start_raw:
                    start_dt = datetime.fromisoformat(start_raw.replace('Z', '+00:00'))
                else:
                    start_dt = datetime.fromisoformat(start_raw + "T00:00:00+08:00")
            except (ValueError, TypeError):
                continue
            delta_min = int((start_dt - now).total_seconds() / 60)
            return ev, delta_min
        return None, None
    except Exception as e:
        logger.warning("_find_next_meeting 失敗：%s", e)
        return None, None


def search_drive_files(keyword: str):
    """用關鍵字搜尋 Google Drive 檔案，回前 10 筆（含 file_id 可再用 upload_to_drive / 其他）。"""
    return drive_ops.search_drive_files(keyword, get_service=get_service)


def upload_to_drive(local_file_path: str, folder_id: str = None):
    """把本機檔案上傳到 Google Drive；folder_id 可指定目標資料夾，空=我的 Drive 根目錄。"""
    return drive_ops.upload_to_drive(
        local_file_path,
        folder_id=folder_id,
        clean_path_fn=_clean_path,
        get_service=get_service,
        media_upload_factory=_get_media_file_upload(),
    )
