#!/bin/bash
# tatibana_bot 見張り番 (launchd KeepAlive で常駐):
#   - 平日の取引時間帯 (8:53-15:25) にエンジンが動いていなければ起動する
#   - エンジンが落ちたら60秒以内に自動復活 (起動前に必ず建玉の後始末を実行)
#   - 引け後・夜間・週末は何もせず待機
# 実体は ~/.local/bin/tatibana_supervisor.sh (Desktop配下はTCC保護のため)。
# このファイルを変更したら: cp scripts/supervisor.sh ~/.local/bin/tatibana_supervisor.sh

set -u
REPO="$HOME/Desktop/立花証券/tatibana_bot"
VENV="$HOME/.venvs/tatibana_bot"
LOG="$REPO/logs/supervisor.log"

log() { echo "$(date '+%F %T') $1" >> "$LOG"; }

mkdir -p "$REPO/logs"
log "supervisor started (pid $$)"

while true; do
    dow=$(date +%u)          # 1=月 ... 7=日
    hhmm=$(date +%H%M)

    if [ "$dow" -le 5 ] && [ "$hhmm" -ge 0853 ] && [ "$hhmm" -le 1525 ]; then
        if ! pgrep -f run_live.py > /dev/null; then
            log "engine not running — starting (cleanup first)"
            chflags nohidden "$VENV"/lib/python*/site-packages/*.pth 2>/dev/null
            cd "$REPO" || { log "repo missing!"; sleep 300; continue; }
            "$VENV/bin/python" scripts/repay_leftovers.py >> "$LOG" 2>&1
            nohup "$VENV/bin/python" scripts/run_live.py >> "logs/live_$(date +%Y%m%d).log" 2>&1 &
            disown
            sleep 8
            if pgrep -f run_live.py > /dev/null; then
                log "engine started"
                osascript -e 'display notification "見張りを開始しました (デモ口座)" with title "🤖 tatibana_bot" sound name "Glass"' 2>/dev/null
            else
                log "ENGINE START FAILED"
                osascript -e 'display notification "起動に失敗! Claudeに「ボット起動して」と伝えてください" with title "❌ tatibana_bot"' 2>/dev/null
            fi
        fi
    fi
    sleep 60
done
