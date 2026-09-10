"""Sample / order tracking.

State lives in
sample_tracker.json at project root; atomic-writes via
_atomic_write_text.

check_sample_deadlines can create follow-up Gmail drafts via
_gmail_create_draft (now in agent_core.gmail).
"""
import os
import json
from datetime import datetime, timedelta

from agent_core.gmail import _gmail_create_draft
from agent_core.logging_and_paths import logger, _SCRIPT_DIR, _atomic_write_text
from agent_core.telegram import telegram_push_agent

_SAMPLE_TRACKER_PATH = os.path.join(_SCRIPT_DIR, "sample_tracker.json")


def telegram_push(message: str):
    return telegram_push_agent("green", message)


def _load_sample_tracker() -> dict:
    if not os.path.exists(_SAMPLE_TRACKER_PATH):
        return {"samples": {}, "updated_at": ""}
    try:
        with open(_SAMPLE_TRACKER_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            d.setdefault("samples", {})
            return d
    except Exception as e:
        logger.warning("sample_tracker 讀取失敗：%s", e)
        return {"samples": {}, "updated_at": ""}


def _save_sample_tracker(data: dict):
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        _atomic_write_text(_SAMPLE_TRACKER_PATH, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning("sample_tracker 存檔失敗：%s", e)


def track_sample(
    sample_id: str,
    customer: str,
    description: str = "",
    contact_email: str = "",
    sent_date: str = "",
    expected_feedback_date: str = "",
    notes: str = "",
):
    """開始追蹤一筆樣品/訂單。到期前小紅會自動提醒 + 起草催信。
    - sample_id：唯一編號（例 S-2026-04、PO-XX-042）
    - customer：客戶名
    - description：樣品描述（例：Black/White PU sneaker, sample size US 9）
    - contact_email：客戶窗口 email，催信用
    - sent_date：寄出日 YYYY-MM-DD（預設今天）
    - expected_feedback_date：預期回饋日 YYYY-MM-DD（預設今起 7 天後）
    - notes：備註（如運費單號、內部代號）
    """
    sample_id = (sample_id or "").strip()
    if not sample_id or not customer.strip():
        return "錯誤：sample_id 和 customer 都要填"

    d = _load_sample_tracker()
    if sample_id in d["samples"] and d["samples"][sample_id].get("status") not in ("closed",):
        cur = d["samples"][sample_id]
        return (f"⚠️ {sample_id} 已在追蹤中（status={cur.get('status')}，客戶={cur.get('customer')}）。\n"
                "要更新狀態用 update_sample_status，要強制覆蓋先 close_sample 再 track。")

    if not sent_date.strip():
        sent_date = datetime.now().strftime("%Y-%m-%d")
    if not expected_feedback_date.strip():
        expected_feedback_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

    d["samples"][sample_id] = {
        "sample_id": sample_id,
        "customer": customer.strip(),
        "description": description.strip(),
        "contact_email": contact_email.strip(),
        "sent_date": sent_date,
        "expected_feedback_date": expected_feedback_date,
        "notes": notes.strip(),
        "status": "open",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "last_reminder_at": "",
        "history": [{
            "at": datetime.now().isoformat(timespec="seconds"),
            "action": "track_start",
            "note": "開始追蹤",
        }],
    }
    _save_sample_tracker(d)
    return (f"✅ 追蹤開始：{sample_id}（{customer}）\n"
            f"   寄出：{sent_date}　預期回饋：{expected_feedback_date}\n"
            f"   窗口：{contact_email or '(未設)'}\n"
            f"   到期前一天會 Telegram 提醒；逾期會起草催信草稿。")


def list_tracked_samples(status: str = "open"):
    """列出追蹤中樣品。status: open / delayed / feedback_received / closed / all（預設 open）"""
    d = _load_sample_tracker()
    if not d.get("samples"):
        return "目前沒有追蹤中樣品。用 track_sample('S-2026-04', '客戶名') 開始追一個。"
    items = list(d["samples"].values())
    if status != "all":
        items = [s for s in items if s.get("status") == status]
    if not items:
        return f"沒有 status={status} 的樣品（全部共 {len(d['samples'])} 筆）。"
    items.sort(key=lambda x: x.get("expected_feedback_date", "9999"))
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📦 追蹤中樣品 {len(items)} 筆（status={status}）："]
    for s in items:
        due = s.get("expected_feedback_date", "?")
        overdue = due < today
        today_due = due == today
        mark = "🔴" if overdue else ("🟡" if today_due else "🟢")
        tail = ""
        if overdue:
            try:
                days = (datetime.now() - datetime.strptime(due, "%Y-%m-%d")).days
                tail = f"（逾期 {days} 天）"
            except Exception:
                logger.debug("silent ignore in broad except")
        lines.append(f"  {mark} {s['sample_id']} | {s.get('customer', '?')} | "
                     f"{s.get('description', '')[:40]} | 預期 {due}{tail}")
    return "\n".join(lines)


def update_sample_status(sample_id: str, status: str, note: str = ""):
    """更新樣品狀態。status: open / delayed / feedback_received / closed"""
    sample_id = sample_id.strip()
    valid = ("open", "delayed", "feedback_received", "closed")
    if status not in valid:
        return f"status 必須是 {valid}，收到：{status!r}"
    d = _load_sample_tracker()
    if sample_id not in d["samples"]:
        return f"找不到 {sample_id}。用 list_tracked_samples('all') 看全部。"
    old_status = d["samples"][sample_id].get("status", "?")
    d["samples"][sample_id]["status"] = status
    d["samples"][sample_id].setdefault("history", []).append({
        "at": datetime.now().isoformat(timespec="seconds"),
        "action": f"{old_status} → {status}",
        "note": note,
    })
    _save_sample_tracker(d)
    return f"✅ {sample_id}: {old_status} → {status}" + (f"（{note}）" if note else "")


def close_sample(sample_id: str, note: str = "feedback 已收到/追蹤結束"):
    """標記完成 = update_sample_status(sample_id, 'closed', note) 的捷徑"""
    return update_sample_status(sample_id, "closed", note)


def delete_tracked_sample(sample_id: str):
    """永久刪除一筆追蹤（一般用 close_sample 就好，不用真的刪）"""
    d = _load_sample_tracker()
    if sample_id not in d["samples"]:
        return f"找不到 {sample_id}"
    d["samples"].pop(sample_id)
    _save_sample_tracker(d)
    return f"已刪除 {sample_id}"


def check_sample_deadlines(auto_draft_followup: bool = True, push_telegram: bool = False):
    """檢查所有 open/delayed 樣品是否逾期或即將到期。
    - auto_draft_followup：對逾期者自動在 Gmail 建 draft 催信（不寄）
    - push_telegram：結果推 Telegram

    Daemon dispatcher 每天早上 09:00 呼叫這個（大王可在 daemon_tasks.json 設）。
    """
    d = _load_sample_tracker()
    today = datetime.now().strftime("%Y-%m-%d")
    overdue, due_today, upcoming_3d = [], [], []

    for sid, s in d["samples"].items():
        if s.get("status") not in ("open", "delayed"):
            continue
        due = s.get("expected_feedback_date", "")
        if not due:
            continue
        try:
            due_dt = datetime.strptime(due, "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        if due < today:
            overdue.append(s)
        elif due == today:
            due_today.append(s)
        elif (due_dt - datetime.now()).days <= 3:
            upcoming_3d.append(s)

    if not (overdue or due_today or upcoming_3d):
        msg = "✅ 所有樣品都在預期時間內，無須跟催。"
        if push_telegram:
            try:
                telegram_push(msg)
            except Exception:
                logger.debug("silent ignore in broad except")
        return msg

    report = [f"📦 樣品追蹤檢查 @ {today}"]
    drafts_created = []

    if overdue:
        report.append(f"\n🔴 逾期 {len(overdue)} 筆：")
        for s in overdue:
            try:
                days = (datetime.now() - datetime.strptime(s['expected_feedback_date'], "%Y-%m-%d")).days
            except Exception:
                days = 0
            report.append(f"  • {s['sample_id']} | {s['customer']} | 逾期 {days} 天")
            if auto_draft_followup and s.get("contact_email"):
                last_rem = s.get("last_reminder_at", "")
                if last_rem:
                    try:
                        last_dt = datetime.fromisoformat(last_rem)
                        hours_since = (datetime.now() - last_dt).total_seconds() / 3600
                        if hours_since < 24:
                            report.append(f"    ⏭️  已在 {hours_since:.0f} 小時前建過 draft，跳過")
                            continue
                    except Exception:
                        logger.debug("silent ignore in broad except")
                try:
                    subj = f"Follow-up: sample {s['sample_id']} feedback"
                    body = (f"Hi,\n\n"
                            f"Hope this note finds you well.\n\n"
                            f"We'd like to follow up on sample {s['sample_id']} "
                            f"({s.get('description', '')}) shipped on {s.get('sent_date', '')}. "
                            f"Feedback was originally expected by {s['expected_feedback_date']}, "
                            f"and we'd appreciate any update when you have a chance so we can "
                            f"plan the next step.\n\n"
                            f"Best regards,\nOwner Name\nJAIFUNG CORPORATION")
                    draft_id = _gmail_create_draft(s["contact_email"], subj, body)
                    if draft_id.startswith("ERR:"):
                        raise RuntimeError(draft_id)
                    drafts_created.append(f"    ✉️  draft → {s['contact_email']} (draft_id={draft_id[:10]}…)")
                    d["samples"][s["sample_id"]]["last_reminder_at"] = datetime.now().isoformat(timespec="seconds")
                    d["samples"][s["sample_id"]].setdefault("history", []).append({
                        "at": datetime.now().isoformat(timespec="seconds"),
                        "action": "auto_draft_followup",
                        "note": f"draft_id={draft_id}",
                    })
                except Exception as _e:
                    logger.warning("auto_draft_followup 失敗 %s: %s", s['sample_id'], _e)
                    drafts_created.append(f"    ⚠️ draft 失敗 ({s['sample_id']}): {_e}")

    if due_today:
        report.append(f"\n🟡 今天到期 {len(due_today)} 筆：")
        for s in due_today:
            report.append(f"  • {s['sample_id']} | {s['customer']} | {s.get('description', '')[:40]}")

    if upcoming_3d:
        report.append(f"\n🟢 3 天內到期 {len(upcoming_3d)} 筆：")
        for s in upcoming_3d:
            report.append(f"  • {s['sample_id']} | {s['customer']} | {s['expected_feedback_date']}")

    if drafts_created:
        report.append("\n自動起草的催信 draft（在 Gmail Drafts 資料夾，大王確認後按寄出）：")
        report.extend(drafts_created)
        _save_sample_tracker(d)

    result = "\n".join(report)
    if push_telegram:
        try:
            telegram_push(result[:3900])
        except Exception:
            logger.debug("silent ignore in broad except")
    return result
