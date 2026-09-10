"""venv 實裝版本 vs requirements 釘版的漂移偵測。

為什麼需要（2026-08-12 一天內踩到兩次）：
  1. 合了 pypdf 的 CVE 修正、以為升好了 —— 實際上 `pip install` 跑在別的地方，
     venv 還是舊版。是後來查 site-packages mtime 才發現的。
  2. 升完才發現 `requirements.txt` **不含** `requirements-dev.txt`，所以 ruff
     一直停在舊版；而 ruff 版本正好決定 lint 規則集，差一版差 4,014 個錯。

共同點：改了釘版但沒真的生效，而**沒有任何東西在檢查這件事**。`requirements.
venv.lock` 更是早就漂到 `google-genai==1.73.1`（實際 2.17.0），沒人讀它。

判準（刻意只抓一種）：**已安裝但版本不符**才算漂移。
  - `pkg==X` 而裝的是 Y      → 漂移（就是「改了沒生效」的指紋）
  - `pkg>=X` 而裝的低於 X    → 漂移
  - 完全沒裝                 → **不算**。requirements-gui / market / oracle
    這些是選配，本來就不會每台都裝；把「沒裝」也算進來會天天假警報。

另一種假警報（2026-08-14 實際踩到）：**那份 requirements 根本不是給主 venv 的**。
`requirements-box-ocr.txt` 裝的是 `var/venvs/box_ocr` 專用 venv（paddlex 會把
numpy 降版、opencv 互踩，所以刻意隔離），它釘 `onnxruntime==1.28.0`；而主 venv
裡剛好也有 onnxruntime 1.27.0 —— 那是 **chromadb 拉進來的**傳遞依賴，跟 box-ocr
的釘版毫無關係。兩個環境放在一起比，比出來的「漂移」是無意義的，照建議
`pip install` 反而會動到 chromadb 解出來的版本。
所以這類檔要在檔頭標 `deps-check: separate-venv=<venv 名>`。

**但「跳過」不等於「檢查完了」**（2026-08-14 的後續）：跳過只是不拿去跟主 venv
比，那份 requirements 本身還是沒人看。實例：Dependabot 07-31 在 #288 把
box-ocr 的 onnxruntime 從 1.27.0 bump 到 1.28.0，但**沒有任何自動流程會去安裝
那份檔案**（要人跑 `bin/setup-box-ocr`），於是 var/venvs/box_ocr 停在舊版停了
兩週，是人工查才發現的。既然標記已經說了「這份屬於哪個 venv」，就該順著
`RUNTIME_ROOT/venvs/<名>/bin/python` 去問**那個**環境裝了什麼 —— 拿對的清單比
對的環境。這就是 `check_dependency_drift` 現在做的事：主 venv ＋ 每個具名專用
venv 各比一次，結果用 `venv` 欄位標明出處。

leaf：只 import 標準庫（探測別的 venv 用 subprocess 跑它自己的直譯器）；
判準相關的都是純函式，requirements 內容與已安裝清單都可由 caller 傳。
"""
from __future__ import annotations

import json
import os
import re
import subprocess

# 檔頭標這個字串 = 這份 requirements 裝的是別的 venv。
#   `deps-check: separate-venv=box_ocr`  → 不納入主 venv 比對，改去比 box_ocr
#   `deps-check: separate-venv`（不具名）→ 只跳過，無從檢查
SEPARATE_VENV_MARKER = "deps-check: separate-venv"
# ⚠️ 只吃同一行的空白（`[ \t]` 不是 `\s`）：用 `\s` 的話「不具名標記 + 換行 + 下
# 一行的套件名」會被當成 venv 名（`separate-venv\nzzz==9.9` → 誤判 venv=zzz）。
_SEPARATE_VENV_RE = re.compile(
    re.escape(SEPARATE_VENV_MARKER)
    + r"[ \t]*(?:=[ \t]*|[ \t]+)(?P<venv>[A-Za-z0-9][A-Za-z0-9._-]*)"
)

# drift 項目的 `venv` 欄位用這個值代表「就是主 .venv」。
MAIN_VENV = "(main)"

# `pkg==1.2.3` / `pkg>=1.2` / `Pillow>=12.3.0`；跳過註解、-r include、環境標記後綴。
_REQ_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*"
    r"(?:\[[^\]]*\])?\s*"                      # extras：pkg[foo]
    r"(?P<op>==|>=)\s*"
    r"(?P<ver>[0-9][^\s;#,]*)"
)


def parse_requirements(text: str) -> dict[str, tuple[str, str]]:
    """requirements 檔內容 → {正規化套件名: (運算子, 版本)}。

    同名多次出現時後者覆蓋前者（跟 pip 實際解析順序一致）。
    """
    out: dict[str, tuple[str, str]] = {}
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0]
        if not line.strip() or line.lstrip().startswith("-"):
            continue
        m = _REQ_RE.match(line)
        if m:
            out[normalize(m.group("name"))] = (m.group("op"), m.group("ver"))
    return out


def normalize(name: str) -> str:
    """PEP 503 套件名正規化 —— `Pillow` / `google_genai` 都要對得上。"""
    return re.sub(r"[-_.]+", "-", str(name or "")).lower()


def _parse_version(v: str) -> tuple:
    """版本 → 可比較的 tuple。非數字段（rc/post/dev）退化成 -1，比同位數字小。"""
    parts = []
    for chunk in re.split(r"[._-]", str(v or "")):
        parts.append(int(chunk) if chunk.isdigit() else -1)
    return tuple(parts)


def _satisfies(installed: str, op: str, wanted: str) -> bool:
    if op == "==":
        # 直接字串比對：釘版就是要一字不差（`2.17.0` vs `2.17` 視為不同，
        # 因為那代表 requirements 寫得不精確，值得看一眼）。
        return normalize(installed) == normalize(wanted)
    iv, wv = _parse_version(installed), _parse_version(wanted)
    # 補齊長度再比，避免 (2,17) < (2,17,0) 這種假不符。
    n = max(len(iv), len(wv))
    return iv + (0,) * (n - len(iv)) >= wv + (0,) * (n - len(wv))


def find_drift(required: dict[str, tuple[str, str]],
               installed: dict[str, str]) -> list[dict]:
    """回不符的清單。**沒安裝的不算** —— 見模組 docstring 的判準說明。"""
    drift = []
    for name, (op, wanted) in sorted(required.items()):
        got = installed.get(name)
        if got is None:
            continue
        if not _satisfies(got, op, wanted):
            drift.append({"name": name, "op": op, "wanted": wanted, "installed": got})
    return drift


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _runtime_root() -> str:
    """var 根。走 logging_and_paths（吃 RED_RUNTIME_DIR），拿不到才退回 repo/var。

    lazy import 是為了保住本模組「只 import 標準庫」的 leaf 性質 —— 純函式那部分
    不會因此被拖進 logging_and_paths 的 import 副作用（建 log 目錄、清舊 log）。
    """
    try:
        from agent_core.logging_and_paths import RUNTIME_ROOT
        return RUNTIME_ROOT
    except Exception:  # noqa: BLE001 - 監控面不該因為 import 失敗就整個掛掉
        return os.path.join(_repo_root(), "var")


def parse_separate_venv_marker(text: str) -> tuple[bool, str]:
    """檔案內容 → (有沒有標記, venv 名稱)。沒具名時名稱回空字串。"""
    if SEPARATE_VENV_MARKER not in (text or ""):
        return False, ""
    m = _SEPARATE_VENV_RE.search(text)
    return True, (m.group("venv") if m else "")


def load_required(root: str | None = None) -> dict[str, tuple[str, str]]:
    """掃 repo 根目錄所有 `requirements*.txt`（含 -dev、-gui 等選配）。

    刻意連選配檔一起讀：沒裝的本來就會被 find_drift 略過，但**裝了卻版本不符**
    的（例如手動裝過 gui 依賴）照樣該抓到。

    例外：檔頭標了 `deps-check: separate-venv` 的整份跳過 —— 那是別的 venv 的
    依賴清單，跟「主 venv 裝了什麼」不是同一個環境，比對只會生假警報（見模組
    docstring 的 box-ocr × chromadb 案例）。
    """
    root = root or _repo_root()
    merged: dict[str, tuple[str, str]] = {}
    try:
        names = sorted(n for n in os.listdir(root)
                       if n.startswith("requirements") and n.endswith(".txt"))
    except OSError:
        return {}
    for n in names:
        # .lock 是另一回事（早已漂移且沒人維護），不納入判準免得製造假警報。
        if "lock" in n:
            continue
        try:
            with open(os.path.join(root, n), "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        if SEPARATE_VENV_MARKER in text:
            continue
        merged.update(parse_requirements(text))
    return merged


def separate_venv_requirements(root: str | None = None) -> dict[str, dict[str, tuple[str, str]]]:
    """{venv 名: 該檔的釘版}。只收**具名**的標記；不具名的無從檢查，略過。"""
    root = root or _repo_root()
    out: dict[str, dict[str, tuple[str, str]]] = {}
    try:
        names = sorted(n for n in os.listdir(root)
                       if n.startswith("requirements") and n.endswith(".txt"))
    except OSError:
        return {}
    for n in names:
        if "lock" in n:
            continue
        try:
            with open(os.path.join(root, n), "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        marked, venv = parse_separate_venv_marker(text)
        if not marked or not venv:
            continue
        out.setdefault(venv, {}).update(parse_requirements(text))
    return out


def load_installed() -> dict[str, str]:
    import importlib.metadata as md
    out: dict[str, str] = {}
    for dist in md.distributions():
        name = (dist.metadata or {}).get("Name")
        if name:
            out[normalize(name)] = dist.version
    return out


# 在別的 venv 裡跑這段，把它自己的已安裝清單吐成 JSON。
_PROBE = (
    "import json,importlib.metadata as m;"
    "print(json.dumps({(d.metadata or {}).get('Name') or '': d.version "
    "for d in m.distributions()}))"
)


def venv_python(venv: str, runtime_root: str | None = None) -> str:
    """專用 venv 的直譯器路徑（`RUNTIME_ROOT/venvs/<名>/bin/python`）。"""
    root = runtime_root or _runtime_root()
    return os.path.join(root, "venvs", venv, "bin", "python")


def load_installed_from_venv(python_bin: str) -> dict[str, str]:
    """問另一個 venv 裝了什麼。**永不 raise**；問不到一律回 {}。

    回 {} 的兩種情況刻意不區分成告警：
      - 直譯器不存在 = 這台機器沒裝這個選配元件（跟「沒裝不算漂移」同一判準）
      - 探測失敗（venv 壞掉 / 逾時）= 這裡沒有足以下判斷的資訊，硬報會變假警報
    """
    if not python_bin or not os.path.isfile(python_bin):
        return {}
    try:
        proc = subprocess.run([python_bin, "-c", _PROBE],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return {}
        raw = json.loads(proc.stdout.strip() or "{}")
    except Exception:  # noqa: BLE001 - 監控面永遠 best-effort
        return {}
    return {normalize(k): v for k, v in raw.items() if k}


def check_dependency_drift(root: str | None = None,
                           runtime_root: str | None = None) -> list[dict]:
    """實際跑一次比對（給 dashboard_alerts / red-status 用）。

    主 venv ＋ 每個具名專用 venv 各比一次；每筆結果帶 `venv` 欄位標明出處
    （主 venv 是 `MAIN_VENV`）。專用 venv 沒建起來就整個略過。
    """
    drift = [dict(d, venv=MAIN_VENV)
             for d in find_drift(load_required(root), load_installed())]
    for venv, required in sorted(separate_venv_requirements(root).items()):
        installed = load_installed_from_venv(venv_python(venv, runtime_root))
        if not installed:
            continue
        drift.extend(dict(d, venv=venv) for d in find_drift(required, installed))
    return drift


def fix_hint(venv: str, root: str | None = None) -> str:
    """該 venv 的修法。專用 venv 優先指它的 setup 腳本（**確認存在才建議**）。"""
    if venv == MAIN_VENV:
        return ".venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt"
    script = os.path.join("bin", f"setup-{venv.replace('_', '-')}")
    if os.path.isfile(os.path.join(root or _repo_root(), script)):
        return f"./{script}"
    return f"重裝 {venv} 專用 venv（見對應的 requirements 檔註解）"


def format_drift(drift: list[dict], root: str | None = None) -> str:
    if not drift:
        return "  ✅ venv 與 requirements 一致"
    lines = [f"  ⚠️ {len(drift)} 個套件與釘版不符（改了釘版但沒裝？）："]
    by_venv: dict[str, list[dict]] = {}
    for d in drift:
        by_venv.setdefault(d.get("venv") or MAIN_VENV, []).append(d)
    for venv, items in sorted(by_venv.items(), key=lambda kv: (kv[0] != MAIN_VENV, kv[0])):
        label = "主 .venv" if venv == MAIN_VENV else f"專用 venv：{venv}"
        lines.append(f"    [{label}]")
        for d in items[:12]:
            lines.append(f"      {d['name']:<26} 要求 {d['op']}{d['wanted']:<12} 實裝 {d['installed']}")
        if len(items) > 12:
            lines.append(f"      …（還有 {len(items) - 12} 個）")
        lines.append(f"      修：{fix_hint(venv, root)}")
    return "\n".join(lines)
