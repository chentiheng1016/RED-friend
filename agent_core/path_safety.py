"""Path traversal / sensitive path guard（V7 安全性修補）。

背景：
  小紅的檔案 skill（excel_ops / pdf_ops / image_gen / vision_ops 等）長期以
  `os.path.expanduser + abspath` 解析使用者給的路徑。沒任何邊界檢查 → 大王
  叫小紅「讀 ~/.ssh/id_rsa」「打 /etc/passwd」「分析 /private/var/log/wifi.log」
  全部會成功。

  直接攻擊路徑：
    - LLM 被 prompt-injection 操控（見 V3） → 引誘讀敏感檔
    - 子代理（delegate_to_sub_agent）拿同 tool list → 同樣風險
    - 大王自己手快不小心打路徑（誤碰）

  我們不能完全鎖死路徑（小紅本來就要存取使用者全機檔），所以走 deny-list：
    1. 明確阻擋幾類眾所周知的敏感位置
    2. 名稱規律明顯像 secret 的檔案（credentials.json / *_token.json 等）擋掉
    3. 真的要繞 → 大王得自己改 code，不是隨口叫得動

API：
  - safe_path(p): 回傳 resolved path 或 raise ValueError
  - is_path_safe(p): True/False，給 caller 自己處理
  - check_path(p): 回傳 (ok, reason)；reason 用來組錯訊息
"""
import os
import re

_REPO_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def safe_cjk_filename(name: str, *, max_len: int = 200, fallback: str = "untitled") -> str:
    """把不可信的檔名清成安全字元：保留英數 / CJK / 連字號 / 底線 / 點，其餘
    （含 path-traversal 的 `..`、`./`、空白）都換成底線、再限長。

    Telegram 附件落地路徑與 exchange_policy artifact 匯出本來各有一份逐字相同
    的實作，差別只在長度上限與 fallback 字串，這裡統一成單一咽喉，補強過濾時
    兩條路徑一起受益。
    """
    name = (name or "").strip()
    if not name:
        return fallback
    name = name.replace("..", "_").lstrip("./")
    name = name.replace(" ", "_")
    name = re.sub(r"[^\w一-鿿\-.]", "_", name)
    return name[:max_len] or fallback


def _resolve_protected(parent: str, name: str) -> str:
    """Round 7 LOW: 把 protected entry 也 realpath，避免 subdir 是 symlink 時
    「resolved 路徑」與「protected 字串」不對齊。若 join 後不存在仍 return 原字串。"""
    raw = os.path.join(parent, name)
    try:
        return os.path.realpath(raw) if os.path.exists(raw) else raw
    except Exception:
        return raw


_PROTECTED_PROJECT_DIRS = [
    _resolve_protected(_REPO_ROOT, name)
    for name in (
        "agent_core",
        "skills",
        "launchd",
        "bin",
        "scripts",
        ".github",
        "var/state",
        # Round 6 Y1/Y2：把 RAG 索引 / 內部 lake / runs 紀錄 / cost 紀錄全部納保護。
        # 否則攻擊者過 V4 +確認 後可寫 chroma_db / parquet 注 fake 內容
        # → 自我複製式 prompt-injection（每次 RAG 命中污染條目都污染下一輪）
        "var/data",
        "var/runs",
        "var/cost",
        "var/logs",
        "tests",  # 防止 LLM 過 +確認 後改測試把安全檢查刪光
    )
]
# Round 6 Y1/Y10：個別檔案保護 — 這些檔位於 repo root，不在 _PROTECTED_PROJECT_DIRS
# 涵蓋的目錄裡，但同樣不能被 LLM 直接寫（會造成 persistence / self-mod 攻擊）
_PROTECTED_PROJECT_FILES = [
    _resolve_protected(_REPO_ROOT, name)
    for name in (
        "mcp_servers.json",        # Y1: 啟動讀取 → spawn subprocess，攻擊者植惡意 server config = persistent RCE
        "CLAUDE.md",               # Y10: agent persona / system instruction
        "agents.md",
        "AGENTS.md",
        "setup.sh",                # 安裝腳本，自我修改
        "Makefile",
        "requirements.txt",        # pip install 來源
        "requirements-core.txt",
        "requirements-rag.txt",
        "requirements-dev.txt",
        ".pre-commit-config.yaml",
        "pyproject.toml",
        "setup.py",
        ".env",                    # 雖然 V7 filename rule 已擋，再 belt-and-suspenders
        # M7-2 round 7：repo root 內的 state / behavior 紀錄，被改寫即影響 agent 行為
        "daemon_tasks.json",       # 排程任務清單 — 直接改寫等於排程任意 prompt
        "memory.json",             # 早期 memory store
        "mistake_ledger.json",     # 誤判 ledger
        "hotword_state.json",      # 熱詞觸發狀態
        "skill_audit.json",        # skill 載入紀錄
        "behavior_policies.md",    # behavior policy 文件版本（學到的 rule）
    )
]

# ────────────────────────────────────────────────────────────────────
# Deny list: directories
# ────────────────────────────────────────────────────────────────────
# 這些目錄底下任何檔案都拒絕。expanduser 後比對。
_DENY_DIR_PATTERNS = [
    # SSH / GPG keys
    r"^/Users/[^/]+/\.ssh($|/)",
    r"^/Users/[^/]+/\.gnupg($|/)",
    # 雲端憑證
    r"^/Users/[^/]+/\.aws($|/)",
    r"^/Users/[^/]+/\.config/gcloud($|/)",
    r"^/Users/[^/]+/\.azure($|/)",
    r"^/Users/[^/]+/\.kube($|/)",
    r"^/Users/[^/]+/\.docker/config($|/)",
    # M1 補丁（review 找到漏列）：更多雲端 / 套件管理 / 開發者工具憑證目錄
    r"^/Users/[^/]+/\.config/op($|/)",                # 1Password CLI vault
    r"^/Users/[^/]+/\.config/anthropic($|/)",         # Anthropic CLI tokens
    r"^/Users/[^/]+/\.config/github-copilot($|/)",
    r"^/Users/[^/]+/\.config/rclone($|/)",            # rclone remote configs
    r"^/Users/[^/]+/Library/Application Support/Slack($|/)",
    r"^/Users/[^/]+/Library/Application Support/Code($|/)",  # VS Code 工作區可能含 settings/secrets
    r"^/Users/[^/]+/Library/Application Support/Discord($|/)",
    r"^/Users/[^/]+/Library/Cookies($|/)",            # Safari cookie store
    r"^/var/db($|/)",
    # macOS 鑰匙圈 / Keychain
    r"^/Users/[^/]+/Library/Keychains($|/)",
    r"^/Library/Keychains($|/)",
    # 系統與設定檔
    r"^/etc($|/)",
    r"^/private/etc($|/)",
    r"^/System($|/)",
    r"^/private/var/db($|/)",
    r"^/private/var/log($|/)",  # 系統日誌可能含敏感（wifi、auth.log 等）
    # Gmail / Google OAuth tokens（小紅自己用的）
    r"^/Users/[^/]+/\.config/google-credentials($|/)",
]


# ────────────────────────────────────────────────────────────────────
# Deny list: filenames（不論在哪個目錄都擋）
# ────────────────────────────────────────────────────────────────────
_DENY_FILENAME_PATTERNS = [
    # 通用憑證
    r"\.env(\..+)?$",                 # .env, .env.local, .env.production
    r"^credentials?\.json$",          # credentials.json, credential.json
    r"^token\.json$",
    r"^client_secret(.+)?\.json$",    # GCP-style
    r"^service[_-]account.*\.json$",
    r"^oauth.*\.(json|yml|yaml)$",
    r"^.*[._-](secret|secrets|token|apikey|api_key)\.(json|ya?ml|env)$",
    # M1 補丁（review 找到漏列）：套件管理工具 / shell history / git creds
    r"^\.netrc$",                     # FTP / HTTP creds
    r"^\.npmrc$",                     # npm token
    r"^\.pypirc$",                    # PyPI publish token
    r"^\.git-credentials$",           # git plaintext creds store
    r"^\.bash_history$",              # 含過往 inline-secret 指令
    r"^\.zsh_history$",
    r"^\.python_history$",            # REPL 歷史
    r"^\.psql_history$", r"^\.mysql_history$",
    r"^\.aws_history$",
    # SSH keys（即使被搬離 ~/.ssh 也認名字）
    r"^id_rsa(\.pub)?$",
    r"^id_ed25519(\.pub)?$",
    r"^id_ecdsa(\.pub)?$",
    r"^.*\.pem$",                     # PEM 私鑰
    r"^.*\.p12$",                     # PKCS#12
    r"^.*\.pfx$",
    r"^.*\.key$",                     # 一般 key file
    # 系統憑證 / shadow
    r"^shadow$", r"^master\.passwd$", r"^passwd$",
    # 1Password / Bitwarden 等密碼管理工具的 vault
    r"^.*\.agilekeychain$",
    r"^.*\.opvault$",
]


_DENY_DIR_RES = [re.compile(p) for p in _DENY_DIR_PATTERNS]
_DENY_FILENAME_RES = [re.compile(p, re.IGNORECASE) for p in _DENY_FILENAME_PATTERNS]


def _resolve(path: str) -> str:
    """expanduser → abspath → realpath（追 symlink，避免 ~/foo → /etc/passwd）。"""
    if not path:
        return ""
    p = os.path.expanduser(path.strip())
    if not os.path.isabs(p):
        p = os.path.abspath(p)
    # realpath 把 symlink 解開，免得攻擊者用 symlink 繞 deny dir 檢查
    try:
        p = os.path.realpath(p)
    except Exception:
        pass
    return p


def check_path(path: str) -> tuple[bool, str]:
    """檢查 path 是否安全。回傳 (ok, reason)。

    reason 在 ok=True 時是 resolved 後的絕對路徑；ok=False 時是被拒原因。
    """
    if not path or not str(path).strip():
        return False, "path 不能空"

    resolved = _resolve(str(path))
    if not resolved:
        return False, "path 解析失敗"

    # 0. 小紅自己的核心程式 / 自動化狀態：避免 LLM 透過檔案工具改寫 agent、
    # skill loader、launchd plist、state 檔後形成持久化攻擊。
    for protected in _PROTECTED_PROJECT_DIRS:
        if resolved == protected or resolved.startswith(protected + os.sep):
            return False, (f"路徑落在小紅受保護的專案區：{resolved}\n"
                           "  （agent_core / skills / launchd / scripts / var/data / var/state /\n"
                           "    var/runs / var/cost / var/logs / tests 等不允許由工具直接存取）\n"
                           "  若要修改程式碼或 RAG 索引，請走人工 code review / git 流程。")
    # Round 6 Y1/Y10：個別保護檔案（repo root config / persona / 套件管理）
    for protected_file in _PROTECTED_PROJECT_FILES:
        if resolved == protected_file:
            return False, (f"檔案受保護：{resolved}\n"
                           "  （mcp_servers.json / CLAUDE.md / setup.sh / requirements*.txt 等\n"
                           "    為啟動或 LLM 行為核心配置，攻擊者寫入即是 persistent backdoor）\n"
                           "  若 大王 確要修改請手動編輯 + 重啟 agent。")

    # 1. 目錄 deny list
    for pat in _DENY_DIR_RES:
        if pat.search(resolved):
            return False, (f"路徑落在敏感目錄：{resolved}\n"
                           "  （SSH / 雲端憑證 / 系統設定 / Keychain 等被全面阻擋）\n"
                           "  若大王確定要存取，請手動把檔案 copy 到 ~/Downloads 等安全位置")

    # 2. 檔名 deny list
    fname = os.path.basename(resolved)
    for pat in _DENY_FILENAME_RES:
        if pat.match(fname):
            return False, (f"檔名疑似含憑證 / 私鑰：{fname}\n"
                           "  常見規律（.env / credentials.json / *.pem / id_rsa 等）一律擋\n"
                           "  若是誤判（例如真的有客戶叫 credentials.json 的訂單表），改個檔名就過")

    return True, resolved


def is_path_safe(path: str) -> bool:
    """簡化版：只回 True/False。"""
    ok, _ = check_path(path)
    return ok


def safe_path(path: str) -> str:
    """檢查通過則回 resolved 絕對路徑；不通過 raise ValueError。

    用法（在 skill 裡）：
        from agent_core.path_safety import safe_path
        p = safe_path(user_path)  # raises ValueError if blocked
    """
    ok, info = check_path(path)
    if not ok:
        raise ValueError(f"❌ 路徑被安全策略阻擋：{info}")
    return info
