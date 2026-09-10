#!/usr/bin/env bash
# 拆除小紅的所有 launchd 背景任務（com.xiaohong.*）
# 動態列出已部署的 plist 逐一 unload + 刪除，不用手動維護 LABELS list
#
# 用法：
#   ./uninstall_daemon.sh              # 互動式，刪前最後確認
#   ./uninstall_daemon.sh --dry-run    # 只秀會刪什麼，不實際動
#   ./uninstall_daemon.sh -n           # 同 --dry-run
#   ./uninstall_daemon.sh --yes        # 不問直接刪（給 CI / 自動化用）
set -uo pipefail

DRY_RUN=false
AUTO_YES=false
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY_RUN=true ;;
        --yes|-y) AUTO_YES=true ;;
        --help|-h)
            grep -E "^#" "$0" | head -12
            exit 0 ;;
        *)
            echo "❌ 未知參數：$arg（支援：--dry-run, --yes, --help）"
            exit 1 ;;
    esac
done

DST_DIR="$HOME/Library/LaunchAgents"

# 解析 repo root（uninstall_daemon.sh 在 repo 根目錄）
REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# ---- 先掃一遍確認要刪什麼 ----
declare -a TARGETS=()
for plist in "$DST_DIR"/com.xiaohong.*.plist; do
    [ -f "$plist" ] || continue
    TARGETS+=("$plist")
done

if [ "${#TARGETS[@]}" -eq 0 ]; then
    echo "   - 沒找到任何 com.xiaohong.*.plist，不用拆"
    exit 0
fi

# ---- 預覽 ----
if $DRY_RUN; then
    echo "🧪 dry-run 模式 — 只看不動"
else
    echo "⚠️  準備從 $DST_DIR 卸載並刪除："
fi
for p in "${TARGETS[@]}"; do
    printf "   • %s\n" "$(basename "$p" .plist)"
done
echo "   共 ${#TARGETS[@]} 個"

if $DRY_RUN; then
    echo ""
    echo "ℹ️  真的要刪：改跑 ./uninstall_daemon.sh （不帶 --dry-run）"
    exit 0
fi

# ---- 互動確認 ----
if ! $AUTO_YES; then
    echo ""
    read -r -p "確認要卸載這 ${#TARGETS[@]} 個 daemon？[y/N] " ans
    case "$ans" in
        y|Y|yes|YES) ;;
        *)
            echo "❎ 已取消"
            exit 0 ;;
    esac
fi

# ---- 實際刪 ----
echo ""
echo "==> 開始卸載"
count=0
for plist in "${TARGETS[@]}"; do
    label=$(basename "$plist" .plist)
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "   ✓ removed $label"
    count=$((count + 1))
done

echo ""
echo "==> 確認已清："
launchctl list | grep "com.xiaohong" || echo "   ✅ 乾淨"

echo ""
echo "🧹 拆除完成。共移除 $count 個 daemon。"
echo "再裝回去：bash $REPO/setup.sh"
