#!/bin/bash
# tatibana_bot 見張り番 (launchd KeepAlive で常駐):
#   - 平日の取引時間帯 (8:53-15:25) にエンジンが動いていなければ
#     launchd のエンジンジョブ (com.tatibana.engine) を起動する
#   - エンジンが落ちたら60秒以内に自動再起動 (起動前に建玉の後始末)
#   - 引け後・夜間・週末は待機
# エンジンの生死は launchd に聞く (pgrepの誤認・bash直起動の即死問題を回避)。
# 実体は ~/.local/bin/tatibana_supervisor.sh。変更したら cp で同期すること。

set -u
REPO="$HOME/Desktop/立花証券/tatibana_bot"
VENV="$HOME/.venvs/tatibana_bot"
LOG="$REPO/logs/supervisor.log"
UID_=$(id -u)

log() { echo "$(date '+%F %T') $1" >> "$LOG"; }

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
            cd "$REPO" || { log "repo missing!"; sleep 300; continue; }
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
    sleep 60
done
