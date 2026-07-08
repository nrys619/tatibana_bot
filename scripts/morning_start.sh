#!/bin/bash
# 平日朝の自動起動 (launchd から呼ばれる):
#   venv修復 -> 前日の残り物掃除 -> 場中エンジン起動
# 土日祝は東証が閉まっているためエンジンが自動で何もせず終了する。

set -u
REPO="$HOME/Desktop/立花証券/tatibana_bot"
VENV="$HOME/.venvs/tatibana_bot"
LOG="$REPO/logs/launchd_morning.log"
cd "$REPO" || exit 1
mkdir -p logs

echo "===== $(date '+%F %T') morning_start" >> "$LOG"

# iCloud同期の隠しフラグ対策 (importが壊れる既知問題)
chflags nohidden "$VENV"/lib/python*/site-packages/*.pth 2>/dev/null

# 二重起動ガード
if pgrep -f run_live.py > /dev/null; then
    echo "engine already running — skip" >> "$LOG"
    exit 0
fi

# 土曜(6)日曜(7)はスキップ
dow=$(date +%u)
if [ "$dow" -ge 6 ]; then
    echo "weekend — skip" >> "$LOG"
    exit 0
fi

"$VENV/bin/python" scripts/repay_leftovers.py >> "$LOG" 2>&1
nohup "$VENV/bin/python" scripts/run_live.py >> "logs/live_$(date +%Y%m%d).log" 2>&1 &
disown
sleep 5
if pgrep -f run_live.py > /dev/null; then
    echo "engine started" >> "$LOG"
    osascript -e 'display notification "見張りを開始しました (デモ口座)" with title "🤖 tatibana_bot" sound name "Glass"' 2>/dev/null
else
    echo "ENGINE START FAILED" >> "$LOG"
    osascript -e 'display notification "自動起動に失敗! Claudeに「ボット起動して」と伝えてください" with title "❌ tatibana_bot" sound name "Basso"' 2>/dev/null
fi
