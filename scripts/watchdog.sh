#!/bin/bash
# 場中に5分おきに走り、エンジンが動いていなければ起動し直す見張り番。
#
# systemd の Restart=on-failure だけでは不足する場面がある:
#   - タイマー自体が発火しなかった (サーバー再起動直後など)
#   - 短時間に連続失敗して systemd が諦めた (StartLimit)
#   - 立花APIが一時的に落ちていて朝の起動に失敗した
# Macでは見張り番が何度も救ってくれた実績があるので、VPSでも同じ役を置く。

set -u
REPO="/home/tatibana/tatibana_bot"
LOG="$REPO/logs/watchdog.log"
UNIT="tatibana-engine.service"
ALERT="$REPO/logs/ALERT.txt"

log() { echo "$(date '+%F %T') $1" >> "$LOG"; }

dow=$(date +%u); hhmm=$(date +%H%M)
# 平日 8:53-15:20 のみ (引け間際に起こしても意味がないので15:20で止める)
[ "$dow" -le 5 ] || exit 0
[ "$hhmm" -ge 0853 ] && [ "$hhmm" -le 1520 ] || exit 0

if systemctl is-active --quiet "$UNIT"; then
    rm -f "$ALERT"          # 動いているので警報を消す
    exit 0
fi

# 動いていない -> 起動を試みる
n=$(cat "$REPO/logs/.watchdog_tries" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$REPO/logs/.watchdog_tries"
log "エンジンが停止している — 起動を試みる (本日${n}回目)"

systemctl reset-failed "$UNIT" 2>/dev/null   # 諦め状態を解除してから
systemctl start "$UNIT"
sleep 20

if systemctl is-active --quiet "$UNIT"; then
    log "起動成功 (${n}回目で復帰)"
    rm -f "$ALERT"
else
    log "起動失敗 (${n}回目) — engine.log の末尾:"
    tail -5 "$REPO/logs/engine.log" >> "$LOG" 2>/dev/null
    # 3回連続で失敗したら人が気づけるよう警報ファイルを残す
    if [ "$n" -ge 3 ]; then
        {
            echo "エンジンの起動に${n}回失敗しています ($(date '+%F %T'))"
            echo "直近のログ:"
            tail -15 "$REPO/logs/engine.log" 2>/dev/null
        } > "$ALERT"
        log "★警報を書き出した: $ALERT"
    fi
fi
