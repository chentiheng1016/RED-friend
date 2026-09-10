"""Gmail secretary tools (compat layer around gmail_ops).

Most real work lives in the
sibling top-level `gmail_ops` module; this file wires up the DI
callbacks and exposes stable public names.
"""
import base64
from email.mime.text import MIMEText

from agent_core import gmail_ops

from agent_core.gemini_client import GEMINI_MODEL, _gemini_generate
from agent_core.google_auth import get_service
from agent_core.memory import _index_memory


def _append_signature(body: str) -> str:
    return gmail_ops.append_signature(body)


def _split_paths(s) -> list:
    return gmail_ops.split_paths(s)


def _build_mime(body: str, attachments: list, html: bool = False, html_alt: str = "") -> MIMEText:
    return gmail_ops.build_mime(body, attachments, html=html, html_alt=html_alt)


def _extract_body(payload) -> str:
    """遞迴抽出 text/plain 內文；沒有則從 text/html 轉純文字。"""
    return gmail_ops.extract_body(payload)


def _list_attachments(payload) -> list:
    return gmail_ops.list_attachments(payload)


def search_gmail(query: str):
    """搜尋 Gmail。query 用 Gmail 搜尋語法：from:、to:、subject:、has:attachment、is:unread、newer_than:7d 等。回傳最多 10 封含 ID / 寄件者 / 主旨 / 摘要。"""
    return gmail_ops.search_gmail(query, get_service=get_service)


def read_gmail(message_id: str):
    """讀一封信的完整內容（寄件者、CC、內文、附件清單）。"""
    return gmail_ops.read_gmail(
        message_id,
        get_service=get_service,
        extract_body_fn=_extract_body,
        list_attachments_fn=_list_attachments,
    )


def send_gmail(to: str, subject: str, body: str, cc: str = "", bcc: str = "", attachments: str = ""):
    """寄一封新信。attachments 為檔案路徑，多個用逗號分隔。簽名檔會自動加上。"""
    return gmail_ops.send_gmail(
        to,
        subject,
        body,
        cc=cc,
        bcc=bcc,
        attachments=attachments,
        get_service=get_service,
        append_signature_fn=_append_signature,
        build_mime_fn=_build_mime,
        split_paths_fn=_split_paths,
        index_memory_fn=_index_memory,
    )


def send_gmail_internal(
    to: str, subject: str, body: str, cc: str = "", bcc: str = "",
    attachments: str = "", *, markdown_html: bool = False, generated_by: str = "",
):
    """背景 daemon / 排程寄信的內部入口（非 LLM 工具）。

    跟 send_gmail 走同一套 gmail_ops.send_gmail，多兩個開關：markdown_html
    決定要不要附 HTML alternative，generated_by 寫 X-RED-Generated 出處標記
    （小紅自產內容，讓 ingest 端不要當公司原始資料吃回 RAG）。

    刻意跟 send_gmail 分開、不掛進 tool catalog：send_gmail 是 LLM 工具，動它
    的簽名會改到 Gemini function declaration。
    """
    html_alt_fn = None
    if markdown_html:
        from agent_core.email_format import markdown_to_email_html
        html_alt_fn = markdown_to_email_html
    return gmail_ops.send_gmail(
        to,
        subject,
        body,
        cc=cc,
        bcc=bcc,
        attachments=attachments,
        get_service=get_service,
        append_signature_fn=_append_signature,
        build_mime_fn=_build_mime,
        split_paths_fn=_split_paths,
        index_memory_fn=_index_memory,
        html_alt_fn=html_alt_fn,
        generated_by=generated_by,
    )


def send_gmail_markdown(
    to: str, subject: str, body: str, cc: str = "", bcc: str = "",
    attachments: str = "", *, generated_by: str = "",
):
    """跟 send_gmail 一樣寄信，但 body 含 markdown 結構（表格/標題/粗體）時多帶
    一份 HTML alternative——表格轉真 <table>，Mail 客戶端才會對齊（背景排程通知
    信專用）。body 沒有 markdown 時行為與 send_gmail 完全相同。
    """
    return send_gmail_internal(
        to, subject, body, cc=cc, bcc=bcc, attachments=attachments,
        markdown_html=True, generated_by=generated_by,
    )


def reply_gmail(message_id: str, body: str, reply_all: bool = False, cc: str = "", attachments: str = ""):
    """回覆一封信，保持在同一個 thread。reply_all=True 會連同原 CC 一起回。"""
    return gmail_ops.reply_gmail(
        message_id,
        body,
        reply_all=reply_all,
        cc=cc,
        attachments=attachments,
        get_service=get_service,
        append_signature_fn=_append_signature,
        build_mime_fn=_build_mime,
        split_paths_fn=_split_paths,
        index_memory_fn=_index_memory,
    )


def download_gmail_attachment(message_id: str, filename: str = "", save_dir: str = "~/Downloads"):
    """下載指定信件的附件到本機。filename 留空=全部下載；填部分字串做比對。"""
    return gmail_ops.download_gmail_attachment(
        message_id,
        filename=filename,
        save_dir=save_dir,
        get_service=get_service,
    )


def summarize_inbox(hours: int = 24):
    """摘要最近 N 小時的未讀郵件；按緊急度分類並挑出需今天回覆的。"""
    return gmail_ops.summarize_inbox(
        hours,
        get_service=get_service,
        extract_body_fn=_extract_body,
        gemini_generate_fn=_gemini_generate,
        gemini_model=GEMINI_MODEL,
    )


def _gmail_create_draft(to: str, subject: str, body: str) -> str:
    """建一個 Gmail draft（不寄出），回傳 draft id。失敗回傳 'ERR:...'"""
    try:
        service = get_service('gmail', 'v1')
        full_body = _append_signature(body)
        mime = _build_mime(full_body, attachments=[])
        mime['to'] = to
        mime['subject'] = subject
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        draft = service.users().drafts().create(
            userId='me', body={'message': {'raw': raw}}
        ).execute()
        return draft.get('id', '?')
    except Exception as e:
        return f"ERR:{type(e).__name__}:{e}"
