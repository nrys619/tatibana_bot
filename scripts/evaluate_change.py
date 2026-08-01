"""設定変更の効果を判定し、続けるか戻すかを決める.

Claudeは提案を10日以上放置する癖がある (実績あり)。人が覚えていなくても
ボット自身が「効いたのか」を判定できるようにする。

2026-08-01の変更: 損切りを 0.25% -> なし (99%)
  理由: 実機のストップ決済が16戦16敗-20,420円。板再生でも外す方が良かった。
  判定: ストップ決済が消えたか / 実質損益が改善したか / 大負けが増えていないか

usage:
  python scripts/evaluate_change.py                    # 現在の判定を表示
  python scripts/evaluate_change.py --json             # 機械可読 (日次レポート用)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime

DB = "data/trades.sqlite3"

# 変更の登録簿。日付・内容・判定の観点を残す
CHANGES = [
    {
        "id": "2026-08-01_no_stop",
        "date": "2026-08-01",
        "what": "損切りを 0.25% -> なし (トレーリングのみ)",
        "why": "実機のストップ決済が16戦16敗-20,420円。板再生でも外す方が良かった",
        "revert": "config.yaml の stop_pct を 99.0 -> 0.25 に戻す",
        "min_trades": 40,     # これだけ取引が貯まるまで判定しない
        "min_days": 7,
    },
]


def evaluate(conn: sqlite3.Connection, ch: dict) -> dict:
    q = conn.execute
    d0 = ch["date"]

    def agg(where: str, params: tuple) -> dict:
        r = q(f"""SELECT COUNT(*), COALESCE(SUM(COALESCE(pnl_net,pnl)),0),
                  SUM(CASE WHEN COALESCE(pnl_net,pnl)>0 THEN 1 ELSE 0 END),
                  COALESCE(MIN(COALESCE(pnl_net,pnl)),0)
                  FROM trades WHERE exit_ts IS NOT NULL AND {where}""", params).fetchone()
        n = r[0] or 0
        return {"n": n, "pnl": r[1], "win": (r[2] or 0) / n * 100 if n else 0,
                "worst": r[3], "avg": r[1] / n if n else 0}

    after = agg("substr(entry_ts,1,10) >= ?", (d0,))
    before = agg("substr(entry_ts,1,10) < ?", (d0,))
    # 変更で消えるはずのもの
    stops = q("""SELECT COUNT(*), COALESCE(SUM(COALESCE(pnl_net,pnl)),0) FROM trades
                 WHERE substr(entry_ts,1,10) >= ? AND exit_reason='stop'""", (d0,)).fetchone()
    days = q("""SELECT COUNT(DISTINCT substr(entry_ts,1,10)) FROM trades
                WHERE substr(entry_ts,1,10) >= ?""", (d0,)).fetchone()[0]

    ready = after["n"] >= ch["min_trades"] and days >= ch["min_days"]
    verdict, reason = "判定できない", ""
    if not ready:
        reason = (f"データ不足 (取引{after['n']}/{ch['min_trades']}件、"
                  f"{days}/{ch['min_days']}日)")
    else:
        improved = after["avg"] > before["avg"]
        no_stops = stops[0] == 0
        worse_tail = after["worst"] < before["worst"] * 1.5   # 大負けが1.5倍以上悪化
        if improved and not worse_tail:
            verdict, reason = "続行", "1取引あたりの損益が改善し、大負けも悪化していない"
        elif not improved and worse_tail:
            verdict, reason = "戻す", "損益が改善せず、大負けも悪化した"
        elif not improved:
            verdict, reason = "戻す", "1取引あたりの損益が改善しなかった"
        else:
            verdict, reason = "要判断", "損益は改善したが大負けが悪化している"
        if not no_stops:
            reason += f" ※ストップ決済が{stops[0]}件残っている (設定が効いていない疑い)"

    return {"id": ch["id"], "what": ch["what"], "date": d0, "days": days,
            "before": before, "after": after, "stops": {"n": stops[0], "pnl": stops[1]},
            "ready": ready, "verdict": verdict, "reason": reason, "revert": ch["revert"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    conn = sqlite3.connect(DB)
    results = [evaluate(conn, c) for c in CHANGES]

    if args.json:
        print(json.dumps(results, ensure_ascii=False, default=str))
        return

    for r in results:
        print("=" * 66)
        print(f"  設定変更の判定: {r['what']}")
        print(f"  ({r['date']} に変更 / {r['days']}営業日が経過)")
        print("=" * 66)
        b, a = r["before"], r["after"]
        print(f"{'':16}{'変更前':>12}{'変更後':>12}")
        print(f"{'取引数':16}{b['n']:>12,}{a['n']:>12,}")
        print(f"{'1取引あたり':16}{b['avg']:>+11.0f}円{a['avg']:>+11.0f}円")
        print(f"{'勝率':16}{b['win']:>11.0f}%{a['win']:>11.0f}%")
        print(f"{'最悪の1取引':16}{b['worst']:>+11.0f}円{a['worst']:>+11.0f}円")
        print(f"\n  ストップ決済: {r['stops']['n']}件 {r['stops']['pnl']:+,.0f}円"
              f"  (変更が効いていれば0件のはず)")
        print(f"\n  ▶ 判定: 【{r['verdict']}】 {r['reason']}")
        if r["verdict"] == "戻す":
            print(f"     戻し方: {r['revert']}")
        print()


if __name__ == "__main__":
    main()
