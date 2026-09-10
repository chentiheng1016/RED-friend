"""Actor-scoped Gmail / Calendar tools — 以「員工自己的」公司信箱身分操作。

多使用者區隔（[[project_telegram_user_separation]]）下，非 owner actor（例
gm@company.example）的大王帳號工具會被 telegram_actor_scope 移除。本模組
提供替代品：透過 service account 網域委派 impersonate 員工**自己的** Workspace
信箱，所以「幫我回信 / 通知全公司開會 / 排會議」全部落在員工自己的帳號裡，
完全不碰大王的 Gmail / 行事曆。

認證走 google_auth.get_service_for_account（subject=員工 email）。前置需求：
service account 的網域委派要授權對該 subject 的下列 scope —
  - gmail.readonly + gmail.send   讀自己信箱 + 寄/回信
  - calendar                      建/查自己的行事曆活動
gmail.readonly 已開（RAG 在讀這些信箱）；send + calendar 要大王在 Workspace
管理控制台對 SA 的 client_id 追加。沒開時工具回友善訊息，不丟 stack trace。

安全邊界：
  - userId="me" / calendarId="primary" 在委派 service 下 = 員工自己，非大王。
  - 寄信附件限制在 Telegram 上傳區（~/Downloads/小紅-uploads），杜絕把大王
    Mac 上任意檔案夾帶外洩（補回被移除的 read_file 沒擋到的縫）。
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Mapping

from agent_core.tool_annotations import resolve_string_annotations

# 委派 scope（最小集合，不含 modify/delete）。讀信跟寄信用「不同」 service —
# 否則 send 還沒授權時，[readonly, send] 整組 token mint 會一起失敗，連讀信都
# 被牽連。拆開後：讀信只要 readonly（現已開），寄/回信才需要 send。
_GMAIL_READ_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
_GMAIL_SEND_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",  # reply 要先讀原信
    "https://www.googleapis.com/auth/gmail.send",
)
_CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar",)

_DEFAULT_SA_FILE = "var/state/google/service_account.json"


def _sa_file() -> str:
    """Resolve the service-account key path; '' if not configured/missing."""
    configured = (
        os.environ.get("RED_GOOGLE_SERVICE_ACCOUNT_FILE")
        or _DEFAULT_SA_FILE
    ).strip()
    if not configured:
        return ""
    path = configured
    if not os.path.isabs(path):
        try:
            from agent_core.logging_and_paths import REPO_ROOT
            path = os.path.join(str(REPO_ROOT), configured)
        except Exception:
            path = os.path.abspath(configured)
    return path if os.path.exists(path) else ""


def actor_google_tools_available(actor: Mapping[str, Any] | None) -> bool:
    """這個 actor 能不能拿 actor-scoped Google 工具？

    需要：是個帶 email 的 actor + service account 金鑰存在 + 未被 env 關閉。
    （是否為 owner 由 caller 判斷 — owner 走自己的 OAuth，不該拿這組。）
    """
    if os.environ.get("RED_TG_ACTOR_GOOGLE_TOOLS", "1") == "0":
        return False
    if not isinstance(actor, Mapping):
        return False
    email = str(actor.get("email") or "").strip()
    if not email or "@" not in email:
        return False
    return bool(_sa_file())


def _company_emails() -> list[str]:
    """全公司 email（從 employee registry 推導），給「通知全公司」展開用。"""
    try:
        from agent_core.web_server.employee_registry import list_employees
        emails = {
            str(e.get("email") or "").strip().lower()
            for e in list_employees()
            if str(e.get("email") or "").strip()
        }
        return sorted(emails)
    except Exception:
        return []


def _expand_attendees(attendees: str) -> list[str]:
    """把 attendees 字串展開成 email 清單。

    '全公司' / 'all' / '全員' 等 → 全公司；否則用逗號/分號/空白切出含 @ 的。
    """
    raw = (attendees or "").strip()
    if not raw:
        return []
    if raw.lower() in {"全公司", "全體", "全員", "公司全體", "all", "everyone", "all-staff"}:
        return _company_emails()
    # 中文語境常用頓號/全形逗號分號分隔 → 一併切，避免整串被當單一無效 email。
    return [tok for tok in re.split(r"[,;\s、，；]+", raw) if "@" in tok]


def _attachment_allow_root() -> str:
    """Telegram 上傳根目錄 — 跟 daemon 實際存附件的地方一致（含 env 覆寫 /
    雲端模式 artifact store），不要自己 hardcode，否則覆寫後合法附件被誤拒。"""
    try:
        from agent_core.exchange_policy import telegram_upload_root
        return os.path.realpath(telegram_upload_root())
    except Exception:
        return os.path.realpath(os.path.join(os.path.expanduser("~"), "Downloads", "小紅-uploads"))


def _vet_attachments(attachments: str) -> tuple[bool, str]:
    """附件路徑白名單檢查。

    回 (ok, detail)。ok=False 時 detail 是該回給使用者的拒絕訊息。
    只允許 Telegram 上傳區底下的檔案 — 防員工夾帶大王 Mac 任意檔案外洩。

    切分隔符必須跟下游 gmail_ops.split_paths 完全一致（逗號 + 分號），否則
    `上傳區/a.pdf;/etc/passwd` 會整串通過檢查、再被 split_paths 拆出
    `/etc/passwd` 夾帶送出（Codex P1）。
    """
    raw = (attachments or "").strip()
    if not raw:
        return True, ""
    root = _attachment_allow_root()
    bad: list[str] = []
    # split_paths 切 [,;]；這裡多納入換行，等價或更嚴（不會放過它會拆的）。
    for piece in re.split(r"[,;\n]+", raw):
        p = piece.strip().strip("'\"")
        if not p:
            continue
        real = os.path.realpath(os.path.expanduser(p))
        if not (real == root or real.startswith(root + os.sep)):
            bad.append(p)
    if bad:
        return False, (
            "🚫 附件只能用你上傳給小紅的檔案（上傳區底下）。\n"
            f"   不被允許的路徑：{', '.join(bad)}\n"
            "   請先把檔案傳給小紅，再請他附上。"
        )
    return True, ""


_SCOPE_HINT = (
    "⚠️ 此功能尚未開通。需大王到 Google Workspace 管理控制台 → 安全性 → "
    "API 控制 → 網域整體委派，為 service account（client_id "
    "106147013660041858132）追加授權後即可使用：\n"
    "   • gmail.send（寄/回信）  • calendar（排會議）\n"
    "   gmail.readonly 已開，不用動。"
)


def _is_scope_error(text: str) -> bool:
    s = str(text or "")
    return (
        "unauthorized_client" in s
        or "access_denied" in s
        or "not authorized for" in s
        or "Client is unauthorized" in s
    )


def _friendly_error(exc: Exception, *, fallback: str) -> str:
    s = str(exc)
    if _is_scope_error(s):
        return _SCOPE_HINT
    return f"{fallback}：{type(exc).__name__}: {s[:200]}"


def _translate_result(result: Any) -> Any:
    """gmail_ops 會把 scope 錯誤吞成回傳字串 — 在這層攔截換成友善提示。"""
    if isinstance(result, str) and _is_scope_error(result):
        return _SCOPE_HINT
    return result


def send_gmail_as(
    actor_email: str, to: str, subject: str, body: str,
    *, cc: str = "", bcc: str = "", attachments: str = "", sa_file: str | None = None,
    markdown_html: bool = False, generated_by: str = "",
) -> Any:
    """代 actor_email 身分寄一封信（網域委派冒充），不需要先建整包
    build_actor_google_tools 互動工具清單——給背景 dispatcher 這類非互動路徑
    直接呼叫。跟 build_actor_google_tools 內的 send_gmail 走同一套認證
    （_GMAIL_SEND_SCOPES）與簽名檔規則（該 actor 自己的簽名，不是大王的）。

    需要網域委派已對該 actor 的 subject 開 gmail.send（見模組 docstring）；
    若未開通，回友善的 _SCOPE_HINT 而非丟例外。

    markdown_html=True：body 含 markdown 結構（表格/標題/粗體）時多帶一份 HTML
    alternative（email_format），排程報表的表格在 Mail 客戶端才會對齊。

    generated_by：非空時寫入 X-RED-Generated 出處標記。排程報表務必帶——這條
    路徑是 addr 寄給 addr（冒名自寄），From 就是真人地址，不帶標記的話 ingest
    端完全無法把「小紅產的報表」跟「本人寫的信」分開。
    """
    email = str(actor_email or "").strip()
    sa = (sa_file or _sa_file()).strip()
    if not email or "@" not in email:
        return "❌ send_gmail_as：actor_email 不能空"
    if not sa:
        return "❌ send_gmail_as：service account 金鑰未設定"

    from agent_core import google_auth as _google_auth
    from agent_core import gmail_ops
    from agent_core.gmail import _build_mime, _split_paths
    from agent_core.memory import _index_memory

    def _gmail_send_service(api: str, version: str):
        return _google_auth.get_service_for_account(
            f"actor:{email}:gmail.rw", api, version,
            service_account_file=sa, subject=email, scopes=_GMAIL_SEND_SCOPES,
        )

    def _actor_append_signature(mail_body: str) -> str:
        sig = ""
        name = ""
        try:
            from agent_core.web_server.employee_registry import get_employee
            rec = get_employee(email) or {}
            sig = str(rec.get("signature") or "").strip()
            name = str(rec.get("name") or "").strip()
        except Exception:
            pass
        if not sig:
            display = name if (name and not name.islower()) else (
                name.title() if name else email.split("@", 1)[0].title()
            )
            sig = f"Best regards,\n\n{display}"
        first = sig.split("\n", 1)[0].strip()
        if first and first in mail_body:
            return mail_body
        return mail_body.rstrip() + "\n\n" + sig + "\n"

    html_alt_fn = None
    if markdown_html:
        from agent_core.email_format import markdown_to_email_html
        html_alt_fn = markdown_to_email_html

    try:
        return _translate_result(gmail_ops.send_gmail(
            to, subject, body, cc=cc, bcc=bcc, attachments=attachments,
            get_service=_gmail_send_service, append_signature_fn=_actor_append_signature,
            build_mime_fn=_build_mime, split_paths_fn=_split_paths,
            index_memory_fn=_index_memory, html_alt_fn=html_alt_fn,
            generated_by=generated_by,
        ))
    except Exception as e:
        return _friendly_error(e, fallback="寄信失敗")


def build_actor_google_tools(actor_email: str, *, sa_file: str | None = None) -> list[Callable]:
    """回傳一組綁定 actor_email 的 Gmail/行事曆工具（以該員工身分操作）。

    工具沿用大王版的公開名稱（search_gmail / send_gmail / reply_gmail /
    read_gmail / list_calendar_events / create_calendar_event），但 service
    是委派到 actor_email 的。標記 `_actor_scoped` + `_tg_auth_wrapped`（後者讓
    telegram 確認門略過 — 大王已選免確認）。
    """
    email = str(actor_email or "").strip()
    sa = (sa_file or _sa_file()).strip()
    if not email or not sa:
        return []

    # 用模組引用呼叫（非 from-import）— 讓 get_service_for_account 可被 patch、
    # 也避免提早綁定。
    from agent_core import google_auth as _google_auth
    from agent_core import gmail_ops

    def _gmail_read_service(api: str, version: str):
        return _google_auth.get_service_for_account(
            f"actor:{email}:gmail.ro", api, version,
            service_account_file=sa, subject=email, scopes=_GMAIL_READ_SCOPES,
        )

    def _gmail_send_service(api: str, version: str):
        return _google_auth.get_service_for_account(
            f"actor:{email}:gmail.rw", api, version,
            service_account_file=sa, subject=email, scopes=_GMAIL_SEND_SCOPES,
        )

    def _calendar_service(api: str, version: str):
        return _google_auth.get_service_for_account(
            f"actor:{email}:cal", api, version,
            service_account_file=sa, subject=email, scopes=_CALENDAR_SCOPES,
        )

    def _actor_append_signature(body: str) -> str:
        """以**這個 actor 自己**的簽名檔結尾——絕不套大王的 EMAIL_SIGNATURE。

        簽名檔來源：員工 registry 的 signature 欄（員工用 set_my_signature 自助
        設定）。沒設時退回最小 fallback（只放名字），讓信不會掛到大王頭上、也
        不亂編職稱/電話。員工已自帶 sign-off 時不重複加。
        """
        sig = ""
        name = ""
        try:
            from agent_core.web_server.employee_registry import get_employee
            rec = get_employee(email) or {}
            sig = str(rec.get("signature") or "").strip()
            name = str(rec.get("name") or "").strip()
        except Exception:
            pass
        if not sig:
            display = name if (name and not name.islower()) else (
                name.title() if name else email.split("@", 1)[0].title()
            )
            sig = f"Best regards,\n\n{display}"
        first = sig.split("\n", 1)[0].strip()
        if first and first in body:
            return body
        return body.rstrip() + "\n\n" + sig + "\n"

    # ── Gmail（沿用 gmail_ops，只換 get_service）──
    def search_gmail(query: str):
        """搜尋你自己公司信箱的信。Gmail 搜尋語法：from:/subject:/is:unread/newer_than:7d 等。"""
        try:
            return _translate_result(gmail_ops.search_gmail(query, get_service=_gmail_read_service))
        except Exception as e:
            return _friendly_error(e, fallback="搜尋信件失敗")

    def read_gmail(message_id: str):
        """讀你自己信箱裡一封信的完整內容（寄件者、CC、內文、附件清單）。"""
        try:
            from agent_core.gmail import _extract_body, _list_attachments
            return _translate_result(gmail_ops.read_gmail(
                message_id, get_service=_gmail_read_service,
                extract_body_fn=_extract_body, list_attachments_fn=_list_attachments,
            ))
        except Exception as e:
            return _friendly_error(e, fallback="讀取信件失敗")

    def send_gmail(to: str, subject: str, body: str, cc: str = "", bcc: str = "", attachments: str = ""):
        """用你自己的公司信箱寄一封新信。attachments 為檔案路徑（限你上傳給小紅的檔），多個用逗號分隔。"""
        ok, detail = _vet_attachments(attachments)
        if not ok:
            return detail
        try:
            from agent_core.gmail import _build_mime, _split_paths
            from agent_core.memory import _index_memory
            return _translate_result(gmail_ops.send_gmail(
                to, subject, body, cc=cc, bcc=bcc, attachments=attachments,
                get_service=_gmail_send_service, append_signature_fn=_actor_append_signature,
                build_mime_fn=_build_mime, split_paths_fn=_split_paths,
                index_memory_fn=_index_memory,
            ))
        except Exception as e:
            return _friendly_error(e, fallback="寄信失敗")

    def reply_gmail(message_id: str, body: str, reply_all: bool = False, cc: str = "", attachments: str = ""):
        """回覆你信箱裡的一封信，保持在同一個 thread。reply_all=True 連同原 CC 一起回。附件限你上傳給小紅的檔。"""
        ok, detail = _vet_attachments(attachments)
        if not ok:
            return detail
        try:
            from agent_core.gmail import _build_mime, _split_paths
            from agent_core.memory import _index_memory
            return _translate_result(gmail_ops.reply_gmail(
                message_id, body, reply_all=reply_all, cc=cc, attachments=attachments,
                get_service=_gmail_send_service, append_signature_fn=_actor_append_signature,
                build_mime_fn=_build_mime, split_paths_fn=_split_paths,
                index_memory_fn=_index_memory,
            ))
        except Exception as e:
            return _friendly_error(e, fallback="回信失敗")

    # ── 行事曆（委派到員工自己的 primary calendar）──
    def list_calendar_events(max_results: int = 5):
        """列出你自己行事曆接下來的 N 個活動（含時間、主旨、ID）。"""
        try:
            from datetime import datetime, timezone
            service = _calendar_service("calendar", "v3")
            now = datetime.now(timezone.utc).isoformat()
            result = service.events().list(
                calendarId="primary", timeMin=now, maxResults=max_results,
                singleEvents=True, orderBy="startTime",
            ).execute()
            events = result.get("items", [])
            if not events:
                return "接下來沒有任何行程。"
            from agent_core.prompt_injection import sanitize_for_llm
            out = "你的近期行程：\n"
            for ev in events:
                start = ev["start"].get("dateTime", ev["start"].get("date"))
                # 行事曆標題 attacker-controllable（受邀活動自動入曆）→ 淨化擋注入（健檢 High）
                title = sanitize_for_llm(ev.get('summary', '（無標題）'))
                out += f"- {start}: {title} (ID: {ev['id']})\n"
            return out
        except Exception as e:
            return _friendly_error(e, fallback="行事曆讀取失敗")

    def create_calendar_event(
        summary: str, start_time: str, end_time: str,
        location: str = "", description: str = "", attendees: str = "",
    ):
        """在你自己的行事曆建活動並（若有與會者）自動寄出邀請。

        start_time / end_time 用 ISO：'2026-06-20T14:30:00+08:00'。
        attendees：填「全公司」邀請全體員工；或逗號分隔的 email；留空=只在自己行事曆建、不通知。
        有與會者時會以你的名義寄出 Google 行事曆邀請（sendUpdates=all）。
        """
        try:
            service = _calendar_service("calendar", "v3")
            event: dict[str, Any] = {
                "summary": summary,
                "start": {"dateTime": start_time, "timeZone": "Asia/Taipei"},
                "end": {"dateTime": end_time, "timeZone": "Asia/Taipei"},
            }
            if location:
                event["location"] = location
            if description:
                event["description"] = description
            invited = _expand_attendees(attendees)
            if invited:
                event["attendees"] = [{"email": e} for e in invited]
            created = service.events().insert(
                calendarId="primary", body=event,
                sendUpdates="all" if invited else "none",
            ).execute()
            note = (
                f"，已寄邀請給 {len(invited)} 人" if invited else "（未設與會者，未發通知）"
            )
            return f"✅ 會議已建立{note}。(ID: {created.get('id', '未知')})"
        except Exception as e:
            return _friendly_error(e, fallback="會議建立失敗")

    def set_my_signature(signature: str):
        """設定你自己寄信用的簽名檔（只改你自己的，立即生效）。

        把整段貼進來（例如：Best regards、你的中英文名、職稱、公司、電話）。
        之後你用小紅寄信／回信都會自動加在信末。傳空字串＝清掉自訂、改用預設
        （只放你的名字）。
        """
        try:
            from agent_core.web_server.employee_registry import set_employee_signature
            set_employee_signature(email, signature)
            sig = str(signature or "").strip()
            if sig:
                return f"✅ 已更新你的寄信簽名檔，之後寄信會自動用：\n\n{sig}"
            return "✅ 已清除你的自訂簽名檔，之後寄信會用預設（只放你的名字）。"
        except Exception as e:
            return _friendly_error(e, fallback="設定簽名檔失敗")

    tools = [
        search_gmail, read_gmail, send_gmail, reply_gmail,
        list_calendar_events, create_calendar_event, set_my_signature,
    ]
    for fn in tools:
        fn._actor_scoped = True          # type: ignore[attr-defined]
        fn._actor_email = email          # type: ignore[attr-defined]
        # 大王已選「免確認」— 標記讓 filter_tools_for_telegram 略過確認門包裝。
        fn._tg_auth_wrapped = True        # type: ignore[attr-defined]
    # 本模組是 `from __future__ import annotations` → 上面這些 nested 工具函式的
    # __annotations__ 是字串（'str' 等）。這些工具是**動態**逐 actor 建的，不走
    # tool_registry 的靜態組裝點，所以得在這裡自己過同一支解析器，否則 google-genai
    # 派發 send_gmail 參數時對字串 annotation 做 isinstance() 會炸
    # `TypeError: isinstance() arg 2 must be a type...`（UserS 2026-06-15 實際踩到）。
    return resolve_string_annotations(tools)
