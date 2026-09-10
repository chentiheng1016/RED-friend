"""Tiny email parsing helpers — leaf module (breaks email_classify↔email_lake cycle).

Only `_extract_email_addr` for now. Kept out of email_lake so
email_classify, quote, and email_lake itself can all consume the helper
without creating a module-import cycle.
"""
import re


def _extract_email_addr(from_header: str) -> str:
    """從 'Name <addr@host>' 或 'addr@host' 形式抽出純 email。"""
    if not from_header:
        return ""
    m = re.search(r'<([^>]+)>', from_header)
    if m:
        return m.group(1).strip().lower()
    return from_header.strip().lower()
