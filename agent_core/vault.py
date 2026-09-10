"""Credential Vault (T6) — 統一 secret 存取 + audit trail。

Before this: 每個 skill / module 直接 `keyring.get_password(SERVICE, NAME)`，
散在 10+ 檔。大王問「到底有哪些 secret、上次誰讀了」只能 grep。

現在統一：
  - vault.get_secret(name)     ← 會寫 access log
  - vault.set_secret(name, v)  ← 會寫 access log + 立刻生效
  - vault.list_secrets()       ← 列全部已知 secret 的狀態（有沒有設、上次讀時間）
  - vault.audit_log(hours)     ← 誰 / 何時讀了誰

Known secrets registry（下面的 _KNOWN_SECRETS）: 文件化所有小紅用到的 secret，
list_secrets() 會顯示：每個 secret 描述 + 取得方式 URL + 目前狀態。

💾 底層：macOS 鑰匙圈（keyring 套件），跨平台（Windows/Linux 也 work）。
📝 audit log：vault_access.log (JSONL)，90 天後自動瘦身。

⚠️ **值不進 log**。只記 (name, operation, timestamp, caller)。安全。
"""
import json
import os
import sys
import traceback
from datetime import datetime, timedelta
from typing import Any

from agent_core.logging_and_paths import VAULT_ACCESS_LOG, logger


_SERVICE = "xiaohong-agent"  # keyring 的 "service" — 跟歷史 code 相容
_VAULT_LOG = VAULT_ACCESS_LOG
_LOG_RETENTION_DAYS = 90


# ────────────────────────────────────────────────────────────────────
# Known secrets registry — 大王 / 新使用者一眼看懂要設什麼
# 新增 secret 時請把它加進來，否則 list_secrets() 不會顯示 status
# ────────────────────────────────────────────────────────────────────
_KNOWN_SECRETS: dict[str, dict] = {
    "gemini-api-key": {
        "desc": "Gemini API key（小紅核心，不設會直接 exit）",
        "how_to_get": "https://aistudio.google.com/apikey",
        "critical": True,
    },
    "telegram-bot-token": {
        "desc": "Telegram bot token（用 bot 跟小紅聊天用）",
        "how_to_get": "@BotFather 建 bot → /newbot 拿 token",
    },
    "telegram-chat-id": {
        "desc": "大王的 Telegram chat ID（ACL 白名單）",
        "how_to_get": "@userinfobot → 看你的 ID",
    },
    "einvoice-appid": {
        "desc": "財政部電子發票 appID（查中獎號碼用）",
        "how_to_get": "https://www.einvoice.nat.gov.tw/",
    },
    "moenv-api-key": {
        "desc": "環境部空氣品質 API key（查 AQI / PM2.5）",
        "how_to_get": "https://data.moenv.gov.tw/",
    },
    "cwa-api-key": {
        "desc": "中央氣象署 Authorization token（查警特報 / 預報）",
        "how_to_get": "https://opendata.cwa.gov.tw/user/authkey",
    },
}


# ────────────────────────────────────────────────────────────────────
# 底層：keyring wrapper + audit log
# ────────────────────────────────────────────────────────────────────
def _get_keyring():
    """Lazy import keyring；沒裝 / 平台支援差時回 None."""
    try:
        import keyring
        return keyring
    except Exception as e:
        logger.debug("keyring 不可用：%s", e)
        return None


def _log_access(op: str, name: str, caller: str = "",
                 ok: bool = True, reason: str = "") -> None:
    """寫一行 JSONL 到 vault_access.log。失敗也不 raise（別因為 log 問題 break 主流程）。"""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "op": op,              # "get" / "set" / "delete"
        "name": name,
        "caller": caller or _infer_caller(),
        "ok": ok,
        "reason": reason[:200] if reason else "",
    }
    try:
        os.makedirs(os.path.dirname(_VAULT_LOG), exist_ok=True)
        with open(_VAULT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("vault log 寫失敗：%s", e)


def _infer_caller(depth: int = 4) -> str:
    """從 call stack 推誰呼叫的（module:function）。"""
    try:
        frame = sys._getframe(depth)
        mod = frame.f_globals.get("__name__", "?")
        fn = frame.f_code.co_name
        return f"{mod}.{fn}"
    except Exception:
        return "?"


# ────────────────────────────────────────────────────────────────────
# 公開 API：get / set / delete / has
# ────────────────────────────────────────────────────────────────────
def get_secret(name: str, default: str = "", caller: str = "") -> str:
    """讀 secret。永遠回字串（沒設回 default）。會寫 audit log。

    Args:
        name: secret key（例如 'gemini-api-key'）。
        default: 沒設時回什麼。預設空字串。
        caller: 可選，記在 audit log 的呼叫源。空字串 = 自動從 stack 推。
    """
    kr = _get_keyring()
    if kr is None:
        _log_access("get", name, caller, ok=False, reason="keyring 不可用")
        return default
    try:
        val = kr.get_password(_SERVICE, name)
    except Exception as e:
        _log_access("get", name, caller, ok=False, reason=str(e))
        return default

    ok = bool(val)
    _log_access("get", name, caller, ok=ok,
                reason="" if ok else "secret 未設定")
    return (val or default)


def has_secret(name: str) -> bool:
    """檢查 secret 是否已設定。不算 audit access（只看存不存在）。"""
    kr = _get_keyring()
    if kr is None:
        return False
    try:
        return bool(kr.get_password(_SERVICE, name))
    except Exception:
        return False


def set_secret(name: str, value: str, caller: str = "", *,
               allow_overwrite_critical: bool = False) -> str:
    """設（或覆蓋）一個 secret。寫 audit log 但**不**記 value。

    Round 7 C7-2 補丁：原本可任意覆蓋 `gemini-api-key` / `telegram-bot-token`
    等 critical secret — 攻擊者過 +確認 後寫入自己的 key，下次重啟所有 prompt
    都送到 attacker 的 Google account。現在 critical secret 必須帶
    `allow_overwrite_critical=True`（LLM 不會主動填這 flag），且未列在
    `_KNOWN_SECRETS` 的 name 一律拒絕（防止 typo / 攻擊者塞自訂 key）。
    """
    if not name or not name.strip():
        return "❌ name 不能空"
    if not value:
        return "❌ value 不能空（要刪請用 delete_vault_secret）"
    name = name.strip()
    # C7-2 (a): 不在 registry 的 name 不允許 — 若真的需要先在 _KNOWN_SECRETS 文件化
    if name not in _KNOWN_SECRETS:
        _log_access("set", name, caller, ok=False, reason="未列在 _KNOWN_SECRETS")
        return (f"❌ secret name '{name}' 未列在 registry，拒絕寫入。\n"
                f"   為防誤打 / 攻擊者塞自訂 key，新 secret 必須先在\n"
                f"   `agent_core/vault.py:_KNOWN_SECRETS` 文件化。")
    # C7-2 (b): critical secret 覆蓋需明確旗標（LLM 不會主動傳 True）
    is_critical = bool(_KNOWN_SECRETS.get(name, {}).get("critical"))
    if is_critical and not allow_overwrite_critical:
        existing = None
        try:
            kr_check = _get_keyring()
            if kr_check is not None:
                existing = kr_check.get_password(_SERVICE, name)
        except Exception:
            pass
        if existing:
            _log_access("set", name, caller, ok=False,
                        reason="critical secret already set, allow_overwrite_critical=False")
            return (f"❌ '{name}' 是 critical secret，已存在；拒絕覆蓋。\n"
                    f"   若大王本人確要替換，請開 REPL 直接 set_secret(...)\n"
                    f"   並傳 allow_overwrite_critical=True；LLM 工具呼叫不會帶這旗標。")
    kr = _get_keyring()
    if kr is None:
        _log_access("set", name, caller, ok=False, reason="keyring 不可用")
        return "❌ keyring 不可用（macOS 以外平台可能需要 pip install secretstorage / keyring.backends.*）"
    try:
        kr.set_password(_SERVICE, name, value)
    except Exception as e:
        _log_access("set", name, caller, ok=False, reason=str(e))
        return f"❌ 寫鑰匙圈失敗：{type(e).__name__}: {e}"
    _log_access("set", name, caller, ok=True)
    known = _KNOWN_SECRETS.get(name, {})
    hint = f"（{known.get('desc')}）" if known else ""
    return f"✅ 已設 secret '{name}' {hint}"


def delete_secret(name: str, caller: str = "") -> str:
    """刪除某個 secret。寫 audit log。"""
    if not name.strip():
        return "❌ name 不能空"
    kr = _get_keyring()
    if kr is None:
        return "❌ keyring 不可用"
    try:
        kr.delete_password(_SERVICE, name)
        _log_access("delete", name, caller, ok=True)
        return f"🗑️ 已刪 secret '{name}'"
    except Exception as e:
        _log_access("delete", name, caller, ok=False, reason=str(e))
        return f"❌ 刪除失敗：{type(e).__name__}: {e}"


# ────────────────────────────────────────────────────────────────────
# 暴露給 agent 的 query tools
# ────────────────────────────────────────────────────────────────────
def list_vault_secrets() -> str:
    """列出小紅已知 secret 的狀態（有沒有設，上次讀/寫時間）。**不顯示 value**。

    Returns:
        表格狀清單：name / status / desc / how_to_get / 最近存取時間。
    """
    kr = _get_keyring()

    # 從 audit log 找每個 secret 最近存取時間
    last_access: dict[str, str] = {}
    if os.path.isfile(_VAULT_LOG):
        try:
            with open(_VAULT_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    nm = e.get("name")
                    if nm:
                        last_access[nm] = e.get("ts", "")
        except (ValueError, TypeError):
            pass

    lines = ["🔐 Vault 狀態"]
    lines.append("-" * 70)
    if kr is None:
        lines.append("❌ keyring 不可用，所有 secret 都讀不到")
        return "\n".join(lines)

    # 先列 known secrets
    for name, info in _KNOWN_SECRETS.items():
        is_set = has_secret(name)
        icon = "✅" if is_set else ("🔴" if info.get("critical") else "⚪")
        last = last_access.get(name, "")
        line = f"{icon} {name:25s}"
        line += f"  {info['desc']}"
        lines.append(line)
        if not is_set:
            lines.append(f"   📝 申請：{info['how_to_get']}")
            lines.append(f"   🔧 設定：對小紅說「設 {name} 為 ...」"
                         " 或跑 set_vault_secret('{name}', 'VALUE')")
        if last:
            lines.append(f"   ⏱ 最近存取：{last}")
    # 再列 audit log 裡有但不在 registry 的 secret（外部 code 加的）
    extra_names = set(last_access.keys()) - set(_KNOWN_SECRETS.keys())
    if extra_names:
        lines.append("")
        lines.append("—— 其他（audit log 有但不在 registry） ——")
        for name in sorted(extra_names):
            is_set = has_secret(name)
            icon = "✅" if is_set else "❌"
            lines.append(f"{icon} {name}   最近存取：{last_access[name]}")
    return "\n".join(lines)


def set_vault_secret(name: str, value: str) -> str:
    """幫大王在 macOS 鑰匙圈設一個 secret。**不會把 value 寫進 log**。

    Args:
        name: secret key（例如 'gemini-api-key'）。
        value: 實際的 key 值。
    """
    return set_secret(name, value, caller="user_via_agent")


def delete_vault_secret(name: str) -> str:
    """刪除一個 secret。用於 key rotation 或撤銷授權。

    Args:
        name: secret key name。
    """
    return delete_secret(name, caller="user_via_agent")


def vault_access_log(hours: int = 24, name: str = "", limit: int = 50) -> str:
    """查 vault 存取紀錄（誰何時讀了哪個 secret）。

    Args:
        hours: 看最近幾小時內（預設 24）。
        name: 可選，只看某個 secret；空字串 = 全部。
        limit: 最多回幾筆（新到舊）。
    """
    if not os.path.isfile(_VAULT_LOG):
        return "（vault_access.log 不存在，還沒任何存取紀錄）"

    cutoff = datetime.now() - timedelta(hours=hours)
    entries = []
    try:
        with open(_VAULT_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if name and e.get("name") != name:
                    continue
                try:
                    t = datetime.fromisoformat(e.get("ts", ""))
                    if t < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
                entries.append(e)
    except Exception as e:
        return f"❌ 讀 log 失敗：{e}"

    if not entries:
        return f"🔍 最近 {hours}h 沒有符合條件的 vault 存取"

    entries = entries[-limit:][::-1]  # 新在前

    lines = [f"🔐 Vault 存取紀錄（最近 {hours}h，共 {len(entries)} 筆）"]
    lines.append("-" * 70)
    ops_icon = {"get": "📖", "set": "✍️", "delete": "🗑️"}
    for e in entries:
        icon = ops_icon.get(e.get("op"), "?")
        ok_icon = "✅" if e.get("ok") else "❌"
        lines.append(
            f"{ok_icon} {icon} {e.get('ts','')}  "
            f"{e.get('op','?'):6s} {e.get('name',''):25s} ← {e.get('caller','?')}"
        )
        if not e.get("ok") and e.get("reason"):
            lines.append(f"     reason: {e['reason']}")
    return "\n".join(lines)


def prune_vault_log(days: int = _LOG_RETENTION_DAYS) -> str:
    """清舊的 vault access log（保留最近 N 天）。"""
    if not os.path.isfile(_VAULT_LOG):
        return "（log 不存在）"
    cutoff = datetime.now() - timedelta(days=days)
    kept = []
    total = 0
    try:
        with open(_VAULT_LOG, "r", encoding="utf-8") as f:
            for line in f:
                total += 1
                try:
                    e = json.loads(line)
                    t = datetime.fromisoformat(e.get("ts", ""))
                    if t >= cutoff:
                        kept.append(line)
                except Exception:
                    kept.append(line)  # 解不出時間就保留（安全起見）
    except Exception as e:
        return f"❌ 讀 log 失敗：{e}"
    try:
        with open(_VAULT_LOG, "w", encoding="utf-8") as f:
            f.writelines(kept)
    except Exception as e:
        return f"❌ 寫 log 失敗：{e}"
    return f"🧹 vault log 清完：{total} → {len(kept)} 行（保留最近 {days} 天）"
