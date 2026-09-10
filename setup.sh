#!/bin/bash
# 小紅 (RED) 安裝器 — 讓新使用者從 git clone 到可跑的完整路徑。
#
# 用法：
#   ./setup.sh              # 互動式，會確認每一步
#   ./setup.sh --no-plists  # 只建 venv + 裝 deps，不部署 launchd plist
#   ./setup.sh --full       # 裝全部 optional bundles（較大）
#   ./setup.sh --yes        # 不問，全部自動做
#
# 做的事：
#   1. 檢查 python3 >= 3.12
#   2. 建 .venv（如果還沒建）
#   3. 安裝預設 bundles（core + rag + docs；--full 則全裝）
#   4. 把 launchd/templates/*.plist 渲染成真實路徑 → ~/Library/LaunchAgents/
#   5. launchctl load 所有 plist
#
# 之後執行 agent：./bin/agent

set -e

# ---- 解析 repo root（setup.sh 所在目錄）----
SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$SCRIPT_DIR"

AUTO_YES=false
NO_PLISTS=false
INSTALL_DEV=false
INSTALL_FULL=false
for arg in "$@"; do
    case "$arg" in
        --yes|-y) AUTO_YES=true ;;
        --no-plists) NO_PLISTS=true ;;
        --dev) INSTALL_DEV=true ;;
        --full) INSTALL_FULL=true ;;
        --help|-h)
            grep -E "^#" "$0" | head -20
            exit 0 ;;
    esac
done

ask() {
    # $1 = prompt
    if $AUTO_YES; then
        return 0
    fi
    read -r -p "$1 [Y/n] " ans
    case "$ans" in
        n|N|no|NO) return 1 ;;
        *) return 0 ;;
    esac
}

say() { echo ""; echo "──── $* ────"; }

say "1/5 檢查 Python 版本"
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ 找不到 python3。請先安裝 Python 3.12 以上（建議 Homebrew: brew install python@3.12）"
    exit 1
fi
PY_VER=$(python3 -c 'import sys; print(".".join(map(str,sys.version_info[:2])))')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 12 ]; }; then
    echo "⚠️ 系統 python3 版本：$PY_VER（建議 ≥ 3.12）"
    echo "   可接受但 3.11 以下未經測試。"
    ask "仍然繼續？" || exit 1
fi
echo "✅ python3 = $PY_VER"

say "2/5 建立 .venv"
if [ -d "$REPO_ROOT/.venv" ]; then
    echo "✅ .venv 已存在（$REPO_ROOT/.venv）"
else
    echo "建立中..."
    python3 -m venv "$REPO_ROOT/.venv"
    echo "✅ .venv 建好"
fi

say "3/5 安裝依賴（pip install -r requirements.txt）"
if ask "這一步會下載約 500 MB-2 GB，要繼續嗎？"; then
    "$REPO_ROOT/.venv/bin/pip" install --upgrade pip
    if $INSTALL_FULL; then
        "$REPO_ROOT/.venv/bin/pip" install -r "$REPO_ROOT/requirements.txt"
        echo "✅ 已安裝完整 bundles（含 GUI / audio 等 optional 功能）"
    else
        "$REPO_ROOT/.venv/bin/pip" install \
            -r "$REPO_ROOT/requirements-core.txt" \
            -r "$REPO_ROOT/requirements-rag.txt" \
            -r "$REPO_ROOT/requirements-docs.txt"
        echo "✅ 已安裝預設 bundles（core + rag + docs）"
        echo "   若要 GUI / OCR / browser 等進階功能，可再跑 ./setup.sh --full"
    fi
    if $INSTALL_DEV; then
        "$REPO_ROOT/.venv/bin/pip" install -r "$REPO_ROOT/requirements-dev.txt"
        echo "✅ 開發工具也裝好了"
    fi
    echo "✅ 依賴裝好"
else
    echo "⏭ 跳過 pip install。之後請自行跑 .venv/bin/pip install -r requirements.txt"
fi

if $NO_PLISTS; then
    say "4/5 略過（--no-plists）"
    say "5/5 略過"
else
    say "4/5 渲染 + 部署 launchd plist"
    TEMPLATE_DIR="$REPO_ROOT/launchd/templates"
    DEST_DIR="$HOME/Library/LaunchAgents"
    mkdir -p "$DEST_DIR"

    if [ ! -d "$TEMPLATE_DIR" ]; then
        echo "❌ 找不到 $TEMPLATE_DIR"
        exit 1
    fi

    if ask "把 $(ls "$TEMPLATE_DIR" | wc -l | tr -d ' ') 個 plist 部署到 $DEST_DIR（會覆蓋同名舊版）？"; then
        count=0
        for tpl in "$TEMPLATE_DIR"/*.plist; do
            name=$(basename "$tpl")
            # 把 @@REPO_ROOT@@ 替換成實際路徑
            sed "s|@@REPO_ROOT@@|$REPO_ROOT|g" "$tpl" > "$DEST_DIR/$name"
            count=$((count + 1))
        done
        echo "✅ 已渲染並複製 $count 份 plist 到 $DEST_DIR"
    else
        echo "⏭ 跳過 plist 部署"
    fi

    say "5/5 load plist 到 launchd"
    if ask "launchctl load 所有 xiaohong plist？（會啟動背景 daemon）"; then
        loaded=0
        for plist in "$DEST_DIR"/com.xiaohong.*.plist; do
            [ -f "$plist" ] || continue
            # 先 unload（如果已 loaded）避免重複
            launchctl unload "$plist" 2>/dev/null || true
            if launchctl load "$plist" 2>/dev/null; then
                loaded=$((loaded + 1))
            else
                echo "  ⚠️ load 失敗：$(basename "$plist")"
            fi
        done
        echo "✅ 成功 load $loaded 個 daemon"
    else
        echo "⏭ 跳過 launchctl load。之後可手動：launchctl load $DEST_DIR/com.xiaohong.*.plist"
    fi
fi

cat <<EOF

═══════════════════════════════════════════════
✅ 安裝完成

執行 agent：
    $REPO_ROOT/bin/agent

或把 bin/ 加進 PATH：
    export PATH="$REPO_ROOT/bin:\$PATH"
    agent

檢查 daemon 狀態：
    launchctl list | grep xiaohong

查看 log：
    ls $REPO_ROOT/var/logs/
═══════════════════════════════════════════════
EOF
