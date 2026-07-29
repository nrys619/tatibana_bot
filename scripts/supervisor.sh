#!/bin/bash
# tatibana_bot 見張り番 (launchd KeepAlive で常駐):
#   - 平日の取引時間帯 (8:53-15:25) にエンジンが動いていなければ (引け後は日次記入も)
#     launchd のエンジンジョブ (com.tatibana.engine) を起動する
#   - エンジンが落ちたら60秒以内に自動再起動 (起動前に建玉の後始末)
#   - 引け後・夜間・週末は待機
# エンジンの生死は launchd に聞く (pgrepの誤認・bash直起動の即死問題を回避)。
# 実体は ~/.local/bin/tatibana_supervisor.sh。変更したら cp で同期すること。

set -u
REPO="$HOME/Desktop/立花証券/tatibana_bot"
VENV="$HOME/.venvs/tatibana_bot"
LOG="$REPO/logs/supervisor.log"
# Desktop は iCloud 同期下にあり、起動直後はまだアクセスできないことがある。
# その間ログが書けず障害が見えなくなるため、同期外の控えにも必ず残す。
FALLBACK_LOG="$HOME/.local/share/tatibana_bot/supervisor.log"
mkdir -p "$(dirname "$FALLBACK_LOG")" 2>/dev/null
UID_=$(id -u)

log() {
    local line="$(date '+%F %T') $1"
    echo "$line" >> "$FALLBACK_LOG" 2>/dev/null
    echo "$line" >> "$LOG" 2>/dev/null
}

engine_alive() {
    launchctl print "gui/$UID_/com.tatibana.engine" 2>/dev/null | grep -q "state = running"
}

mkdir -p "$REPO/logs"
log "supervisor started (pid $$)"

while true; do
    dow=$(date +%u)
    hhmm=$(date +%H%M)

    if [ "$dow" -le 5 ] && [ "$hhmm" -ge 0853 ] && [ "$hhmm" -le 1525 ]; then
        if ! engine_alive; then
            log "engine not running — cleanup & kickstart"
            chflags nohidden "$VENV"/lib/python*/site-packages/*.pth 2>/dev/null
            # 起動直後は iCloud が Desktop を用意できていないことがある。
            # 5分も待つと寄り付きを丸ごと逃すので、短い間隔で試し直す。
            if ! cd "$REPO"; then
                log "repo not reachable (iCloud未同期?) — 30秒後に再試行"
                sleep 30
                continue
            fi
            "$VENV/bin/python" scripts/repay_leftovers.py >> "$LOG" 2>&1
            launchctl kickstart "gui/$UID_/com.tatibana.engine" >> "$LOG" 2>&1
            sleep 8
            if engine_alive; then
                log "engine started via launchd"
                osascript -e 'display notification "見張りを開始しました (デモ口座)" with title "🤖 tatibana_bot" sound name "Glass"' 2>/dev/null
            else
                log "ENGINE START FAILED — engine.log末尾:"
                tail -5 "$REPO/logs/engine.log" >> "$LOG" 2>/dev/null
                osascript -e 'display notification "起動に失敗! Claudeに「ボット起動して」と伝えてください" with title "❌ tatibana_bot"' 2>/dev/null
            fi
        fi
    fi
    # 引け後の日次記入。launchdの時報はMacがスリープしていると鳴らないことがあるため、
    # 常駐している見張り番からも実行する。data/analyses/.posted/ の印で二重記入を防ぐ。
    if [ "$dow" -le 5 ] && [ "$hhmm" -ge 1540 ]; then
        if cd "$REPO" 2>/dev/null; then
            out=$("$VENV/bin/python" scripts/daily_report.py --catchup 5 2>&1)
            case "$out" in
                *すべて記入済み*) : ;;                       # 平常。ログを汚さない
                *) log "日次記入: $out" ;;
            esac
        fi
    fi

    sleep 60
done
