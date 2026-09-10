#!/bin/bash
# 每週備份 — 2026-06-14 重新設計（見 memory: reference_home_xiaohong_hub「備份策略」）。
#
# 舊設計的問題：每週 tar+gzip 整個 chroma_db（104G 純衍生快取）→ 75G/份，
# KEEP_COUNT=12 會讓 ~/RED_backups 奔向 ~900G、約 3 週塞爆磁碟。而且那 75G 幾乎
# 100% 是「可由 rag_sync 從 Gmail/Drive 重建」的衍生資料，備份目的只是「出事時快速救回」。
#
# 新設計：分清兩類資料，各用對的工具：
#   ① chroma_db（衍生、可重建）→ 健康驗證後做 APFS COW clone（cp -c：瞬間、零額外空間），
#      留最近 N 份。出事時換目錄即還原，比解壓 75G 快得多；終極後盾仍是 rag_sync 全量重建。
#   ② 真正無可取代的 runtime/業務狀態（~55M，不在 git、不在 Google 雲端）→ tar.gz 留多份。
#      若設了 RED_OFFSITE_DIR 才額外推一份到異地（這才是真正的災備——同碟 clone 擋不了硬碟故障）。
#
# 由 com.xiaohong.backup_weekly launchd plist 每週日 03:00 呼叫。
# 註：cp -c 需要 APFS（clonefile）；本機 /System/Volumes/Data 為 APFS，已實測 104G clone 0.01s / 零增長。

set -u  # 未定義變數直接噴錯（不用 -e，讓任一步失敗仍續跑其餘備份與 log）
# ⚠️ launchd 注入 LANG=en_US.UTF-8。在此 UTF-8 locale 下，bash 解析 $var 名稱時會把緊跟在後的
#    全形標點（、）等）首位元組吃進變數名 → 查到未定義變數、set -u 立即中止整支腳本。故下方所有
#    後面緊接 CJK 字元的變數一律用 ${var} 明確界定名稱邊界（2026-06-21 backup_weekly 失敗根因）。

REPO_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/../.." >/dev/null 2>&1 && pwd)"
RUNTIME_DIR="${RED_RUNTIME_DIR:-$REPO_DIR/var}"
BACKUP_DIR="$HOME/RED_backups"
LOG_FILE="$RUNTIME_DIR/logs/backup_weekly.log"

CHROMA_SRC="$RUNTIME_DIR/data/chroma_db"
CHROMA_CLONES_DIR="$BACKUP_DIR/chroma_clones"
CHROMA_KEEP="${RED_BACKUP_CHROMA_KEEP:-2}"      # 衍生快取只需「最近的健康狀態」，留 2 份（current + 上一份保險）
STATE_KEEP="${RED_BACKUP_STATE_KEEP:-12}"       # 無可取代狀態很小（~55M），多留無妨
ERP_DB_SRC="$RUNTIME_DIR/data/erp_mirror/erp_full.duckdb"
ERP_CLONES_DIR="$BACKUP_DIR/erp_clones"
ERP_KEEP="${RED_BACKUP_ERP_KEEP:-2}"            # 716MB 單檔、可從 ERP 全量重建(12-15h) → 留 2 份 COW clone
CHROMA_HTTP_URL="${RED_CHROMA_HTTP_URL:-http://127.0.0.1:8000}"

mkdir -p "$BACKUP_DIR" "$CHROMA_CLONES_DIR" "$ERP_CLONES_DIR" "$(dirname "$LOG_FILE")"

TS=$(date +%Y%m%d_%H%M)
log() {
    line="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    # launchd 下 StandardOutPath 與 LOG_FILE 是同一檔，若再 tee 到 stdout 會讓每行寫兩次。
    # 故只有 stdout 是終端機（手動跑）才額外印到畫面；非 TTY（launchd）時只 append 一次。
    if [ -t 1 ]; then echo "$line" | tee -a "$LOG_FILE"; else echo "$line" >>"$LOG_FILE"; fi
}

log "▶️ 開始備份（TS=${TS}）"

# ============ Part 1：chroma_db — 健康閘 + APFS COW clone ============
# 健康閘的用意：chroma 反覆腐壞過（HNSW header 爆、upsert SIGSEGV）。只在 server heartbeat
# 正常時才快照，否則寧可不動、保留上一份好的 clone——絕不把壞狀態蓋掉最後的好副本。
chroma_healthy() {
    curl -fsS --max-time 15 "$CHROMA_HTTP_URL/api/v2/heartbeat" >/dev/null 2>&1
}

if [ ! -d "$CHROMA_SRC" ]; then
    log "⚠️ 找不到 ${CHROMA_SRC}，跳過 chroma 備份"
elif ! chroma_healthy; then
    log "⚠️ chroma server heartbeat 失敗（${CHROMA_HTTP_URL}）→ 不快照，保留既有 clone（避免備到壞狀態）"
else
    NEW_CLONE="$CHROMA_CLONES_DIR/chroma_db_$TS"
    log "📸 chroma 健康，COW clone → chroma_clones/chroma_db_$TS"
    if cp -c -R "$CHROMA_SRC" "$NEW_CLONE" 2>>"$LOG_FILE"; then
        # clone 後 sanity check（讀的是 clone 副本、唯讀，不碰 live DB）：
        #   sqlite 可開且 collections 表有列 + 每個 HNSW 段都帶 header.bin
        ncoll=$(sqlite3 "file:$NEW_CLONE/chroma.sqlite3?mode=ro" 'SELECT count(*) FROM collections' 2>>"$LOG_FILE")
        nheader=$(find "$NEW_CLONE" -name header.bin 2>/dev/null | wc -l | tr -d ' ')
        if [ -n "$ncoll" ] && [ "$ncoll" -ge 1 ] 2>/dev/null && [ "$nheader" -ge 1 ]; then
            log "✅ clone 驗證 OK（collections=${ncoll:-?}、HNSW 段=${nheader:-?}）"
            pruned=0
            while IFS= read -r old; do
                [ -n "$old" ] && rm -rf "${CHROMA_CLONES_DIR:?}/$old" && { pruned=$((pruned+1)); log "🗑️  刪舊 clone：$old"; }
            done < <(ls -1t "$CHROMA_CLONES_DIR" 2>/dev/null | grep -E '^chroma_db_[0-9]' | tail -n +$((CHROMA_KEEP+1)))
        else
            log "❌ clone 驗證失敗（collections=${ncoll:-?}、header=${nheader:-?}）→ 丟棄這份壞 clone，保留上一份好的"
            rm -rf "$NEW_CLONE"
        fi
    else
        log "❌ COW clone 失敗（cp -c）"
        rm -rf "$NEW_CLONE" 2>/dev/null
    fi
fi

# ============ Part 1b：ERP 鏡像 DuckDB — APFS COW clone ============
# 716MB 單檔、可從 ERP 主機全量重建(12-15h、且前提是主機還活著)，故不塞進 tar 狀態包
# （會把 18M 的包撐大 40 倍且天天變），改比照 chroma 走 COW clone。clone 後唯讀開一下驗證。
if [ ! -f "$ERP_DB_SRC" ]; then
    log "⚠️ 找不到 ${ERP_DB_SRC}，跳過 ERP 鏡像備份"
else
    NEW_ERP="$ERP_CLONES_DIR/erp_full_$TS.duckdb"
    log "📸 ERP 鏡像 COW clone → erp_clones/erp_full_$TS.duckdb"
    if cp -c "$ERP_DB_SRC" "$NEW_ERP" 2>>"$LOG_FILE"; then
        nrows=$("$REPO_DIR/.venv/bin/python" -c "import duckdb;print(duckdb.connect('$NEW_ERP',read_only=True).execute('SELECT COUNT(*) FROM v_orders').fetchone()[0])" 2>>"$LOG_FILE")
        if [ -n "$nrows" ] && [ "$nrows" -ge 1 ] 2>/dev/null; then
            log "✅ ERP clone 驗證 OK（v_orders=${nrows:-?} 列）"
            while IFS= read -r old; do
                [ -n "$old" ] && rm -f "${ERP_CLONES_DIR:?}/$old" && log "🗑️  刪舊 ERP clone：$old"
            done < <(ls -1t "$ERP_CLONES_DIR" 2>/dev/null | grep -E '^erp_full_.*\.duckdb$' | tail -n +$((ERP_KEEP+1)))
        else
            log "❌ ERP clone 驗證失敗（v_orders=${nrows:-?}）→ 丟棄，保留上一份好的"
            rm -f "$NEW_ERP"
        fi
    else
        log "❌ ERP COW clone 失敗（cp -c）"
        rm -f "$NEW_ERP" 2>/dev/null
    fi
fi

# ============ Part 2：無可取代的 runtime/業務狀態 → tar.gz ============
# 只收「不在 git、又不在 Google 雲端、重建昂貴」的東西。chroma/HNSW/向量一律不收（Part 1 管）。
# ⚠️ 絕不收 secrets：var/state/google/（OAuth token、credentials、service_account 網域委派金鑰）
#    一律 --exclude。狀態包是「明文」、會推上 Drive，secrets 走 Secret Manager（gcp_sync_secrets.py）
#    這條加密託管的路，不進這包。改這裡前先想清楚：任何新的明文 secret 落點都要在此排除。
STATE_OUT="$BACKUP_DIR/RED_state_backup_${TS}.tar.gz"
STATE_EXCLUDES=(--exclude='var/state/google' --exclude='*.lock' --exclude='*credentials*.json' --exclude='*service_account*.json' --exclude='*token.json*')
STATE_FAILED=0
state_targets=()
for p in \
    "data/data_lake_internal" "data/data_lake" "data/cost" \
    "data/bom_history" "data/quote_history" "data/idp_outputs" \
    "data/erp_schema_probe" "data/drive_sync_skip_state.json" "state" "factory_data"
do
    [ -e "$RUNTIME_DIR/$p" ] && state_targets+=("var/$p")
done

if [ ${#state_targets[@]} -gt 0 ]; then
    cd "$REPO_DIR" || { log "❌ 切目錄失敗"; exit 1; }
    # 一次列出包內容（command substitution 會帶出 tar 退出狀態 → 壓縮壞掉時
    # && 鏈直接中斷，等於同時驗完整性），再用 here-string 查敏感檔；比原本三次
    # 解壓省事，也避免 grep 把 tar 的退出狀態蓋掉而讓壞包繞過檢查。
    if tar "${STATE_EXCLUDES[@]}" -czf "$STATE_OUT" "${state_targets[@]}" 2>>"$LOG_FILE" \
        && state_listing=$(tar -tzf "$STATE_OUT" 2>>"$LOG_FILE") \
        && ! grep -qiE 'google/|credentials|service_account|token\.json' <<<"$state_listing"; then
        log "✅ 狀態備份 $(du -h "$STATE_OUT" | awk '{print $1}')（${#state_targets[@]} 個目標）"
        while IFS= read -r old; do
            [ -n "$old" ] && rm -f "$BACKUP_DIR/$old" && log "🗑️  刪舊狀態包：$old"
        done < <(ls -1t "$BACKUP_DIR" 2>/dev/null | grep -E '^RED_state_backup_.*\.tar\.gz$' | tail -n +$((STATE_KEEP+1)))
    else
        log "❌ 狀態 tar 失敗（exit=$?）"
        rm -f "$STATE_OUT" 2>/dev/null
        STATE_FAILED=1
    fi
else
    log "⚠️ 沒有可備份的狀態目標"
fi

# ============ Part 3：異地推送（真正的災備，預設關閉）============
# 設 RED_OFFSITE_DIR=<Google Drive / 外接 / 雲端路徑> 才啟用。只推「狀態包」（~55M），
# 不推 chroma clone（衍生、100G、可重建）。⚠️ 狀態包不含 secrets/OAuth（那些走 launchd
# env 注入、不在 var/）——要把 secrets 也納入異地，是另一個安全決策，需明確開。
if [ -n "${RED_OFFSITE_DIR:-}" ] && [ -d "$RED_OFFSITE_DIR" ] && [ -f "$STATE_OUT" ]; then
    if cp "$STATE_OUT" "$RED_OFFSITE_DIR/" 2>>"$LOG_FILE"; then
        log "🌐 已推異地：$RED_OFFSITE_DIR/$(basename "$STATE_OUT")"
        while IFS= read -r old; do
            [ -n "$old" ] && rm -f "$RED_OFFSITE_DIR/$old"
        done < <(ls -1t "$RED_OFFSITE_DIR" 2>/dev/null | grep -E '^RED_state_backup_.*\.tar\.gz$' | tail -n +$((STATE_KEEP+1)))
    else
        log "❌ 異地推送失敗：$RED_OFFSITE_DIR"
    fi
elif [ -n "${RED_OFFSITE_DIR:-}" ]; then
    log "⚠️ RED_OFFSITE_DIR=$RED_OFFSITE_DIR 不存在，跳過異地推送"
fi

# ============ 報告 ============
# chroma clone 走 COW、共享區塊，du 會顯示「邏輯」大小（非實體佔用），故只報份數。
CHROMA_N=$(ls -1d "$CHROMA_CLONES_DIR"/chroma_db_* 2>/dev/null | wc -l | tr -d ' ')
# 用變數 + 條件，避免空輸入時 BSD xargs 仍跑一次 `du`（會誤量整個 repo 目錄）。
latest_state=$(ls -1 "$BACKUP_DIR"/RED_state_backup_*.tar.gz 2>/dev/null | tail -1)
STATE_SZ=""
[ -n "$latest_state" ] && STATE_SZ=$(du -h "$latest_state" 2>/dev/null | awk '{print $1}')
STATE_N=$(ls -1 "$BACKUP_DIR"/RED_state_backup_*.tar.gz 2>/dev/null | wc -l | tr -d ' ')
# 查備份碟本身的可用空間（BACKUP_DIR 可能在外接/其他分割區，別寫死系統卷）。
DISK_FREE=$(df -h "$BACKUP_DIR" 2>/dev/null | tail -1 | awk '{print $4}')
log "📦 chroma clone ${CHROMA_N} 份（COW 共享）｜狀態包 ${STATE_N} 份（最新 ${STATE_SZ:-?}）｜磁碟可用 ${DISK_FREE}"
# 無可取代的狀態包沒產出 = 這次備份失敗，要讓 launchd 看到非零退出（KeepAlive=false
# 不會因此 restart loop），否則監控誤判每週備份成功。
if [ "$STATE_FAILED" -ne 0 ]; then
    log "❌ 完成（狀態備份失敗 — 無可取代資料未產生／未推異地）"
    echo "" >> "$LOG_FILE"
    exit 1
fi
log "✅ 完成"
echo "" >> "$LOG_FILE"
