"""実機とシミュレーションの突き合わせ検査 (毎日の成績表で実行).

今日の板記録を探索モード相当の緩い基準でふるいにかけ、
「合図が出るはずだった回数」と「実機が実際に記録した判断回数」を比較する。
大きなズレ = 実機のどこかに新しい足かせ(バグ)がある兆候。

usage: python scripts/reconcile.py [YYYYMMDD]
exit code: 0=正常 / 2=要調査 (ズレ検出)
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path


def expected_signals(day: str, since_hhmm: str = "09:00") -> list[tuple[str, str]]:
    """探索モード基準で「出るはずだった合図」の時刻と銘柄を数える."""
    path = Path(f"data/snapshots/{day}.jsonl")
    if not path.exists():
        return []
    hists: dict[str, deque] = defaultdict(deque)
    ticksum: dict[str, float] = defaultdict(float)
    tickn: dict[str, int] = defaultdict(int)
    hits: list[tuple[str, str]] = []
    last_hit: dict[str, float] = {}

    with open(path) as f:
        for line in f:
            r = json.loads(line)
            code, px = r["code"], r.get("last_price")
            if not px:
                continue
            ts = r["ts"]
            t = datetime.fromisoformat(ts).timestamp()
            h = hists[code]
            h.append((t, px))
            while h and t - h[0][0] > 300:
                h.popleft()
            ticksum[code] += r.get("tick_count", 0)
            tickn[code] += 1
            if ts[11:16] < since_hhmm:
                continue
            imb = r.get("imbalance", 0)
            br = r.get("buy_ratio", 0.5)
            micro = r.get("microprice_dev", 0)
            long_ok = imb >= 0.35 and br >= 0.6 and micro > 0
            short_ok = imb <= -0.35 and br <= 0.4 and micro < 0
            if not (long_ok or short_ok):
                continue
            if px > 3000 or r.get("spread_bps", 99) > 8 or len(h) < 60:
                continue
            prices = [p for _, p in h]
            if (max(prices) - min(prices)) / px * 100 < 0.3:
                continue
            px_old = h[0][1]
            if (long_ok and not px > px_old) or (short_ok and not px < px_old):
                continue
            avg = ticksum[code] / max(tickn[code], 1)
            if r.get("tick_count", 0) < 1.2 * max(avg, 0.5):
                continue
            if t - last_hit.get(code, 0) < 60:  # 同一銘柄の連続点灯は1回と数える
                continue
            last_hit[code] = t
            hits.append((ts[11:19], code))
    return hits


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else f"{date.today():%Y%m%d}"
    day_iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"

    hits = expected_signals(day)
    conn = sqlite3.connect("data/trades.sqlite3")
    actual = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE ts LIKE ?", (day_iso + "%",)
    ).fetchone()[0]
    acted_codes = {r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM signals WHERE ts LIKE ?", (day_iso + "%",))}
    conn.close()

    # 記録専用銘柄は取引しないので「出るはずだった合図」から除外する
    # (ただし場中スキャンで監視に昇格した=実機の判断が残っている銘柄は数える)
    rl_path = Path("data/recordlist.json")
    if rl_path.exists():
        record_only = set(json.loads(rl_path.read_text())) - acted_codes
        skipped = [h for h in hits if h[1] in record_only]
        hits = [h for h in hits if h[1] not in record_only]
        if skipped:
            print(f"(記録専用銘柄の合図 {len(skipped)}回は取引対象外のため除外)")

    print(f"=== 実機とシミュの突き合わせ ({day_iso}) ===")
    print(f"出るはずだった合図(探索基準・重複除去): {len(hits)}回")
    for ts, code in hits[:10]:
        print(f"  {ts} {code}")
    print(f"実機が記録した判断: {actual}回")

    if len(hits) >= 3 and actual == 0:
        print("⚠️ 要調査: 合図が出るはずなのに実機の判断がゼロ。新しい足かせ(バグ)の疑い。")
        sys.exit(2)
    print("判定: 正常範囲 (実機は探索基準より厳しい本命基準も併用しているため、多少の差は正常)")


if __name__ == "__main__":
    main()
