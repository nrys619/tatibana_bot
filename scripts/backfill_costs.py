"""過去の取引に売買コストを遡って算入する.

2026-07-30 まで、実機の損益は (決済価格 - 建値) x 株数 しか記録しておらず、
スプレッドも滑りも引いていなかった。そのため報告してきた成績は実際より良かった。

決済時のスプレッドは板スナップショットに残っているので、それを使って実額を出す。
pnl (コスト抜き) はそのまま残し、cost と pnl_net を埋める。

usage:
  python scripts/backfill_costs.py --dry-run   # 変更せず結果だけ見る
  python scripts/backfill_costs.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from bisect import bisect_left
from math import isfinite
from pathlib import Path

DB = "data/trades.sqlite3"
SNAP_DIR = Path("data/snapshots")
SLIPPAGE_BPS = 2.0
MAX_COST_BPS = 50.0   # 板の片側が空だとスプレッドが無限大で記録される


def load_spreads(day: str) -> dict[str, tuple[list[str], list[float]]]:
    """その日のスナップショットから code -> (時刻の並び, スプレッド) を作る."""
    stem = day.replace("-", "")
    path = SNAP_DIR / f"{stem}.jsonl"
    gz = SNAP_DIR / f"{stem}.jsonl.gz"
    out: dict[str, tuple[list[str], list[float]]] = {}
    if not path.exists() and not gz.exists():
        return out
    import gzip
    opener = (lambda: gzip.open(gz, "rt")) if not path.exists() else path.open
    with opener() as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            code = r.get("code")
            sp = r.get("spread_bps")
            if code is None or sp is None:
                continue
            ts, sps = out.setdefault(code, ([], []))
            ts.append(r["ts"])
            sps.append(sp)
    return out


def spread_at(table: dict, code: str, ts: str) -> float | None:
    """決済時刻に最も近いスプレッド。無ければ None."""
    entry = table.get(code)
    if not entry:
        return None
    times, sps = entry
    i = min(bisect_left(times, ts), len(times) - 1)
    sp = sps[i]
    if sp is None or not isfinite(sp) or sp < 0:
        return MAX_COST_BPS
    return min(sp, MAX_COST_BPS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    rows = list(conn.execute(
        "SELECT id, code, quantity, exit_ts, exit_price, pnl, exit_reason FROM trades"
        " WHERE exit_ts IS NOT NULL AND cost IS NULL ORDER BY exit_ts"))
    if not rows:
        print("コスト未算入の取引はありません")
        return

    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r[3][:10], []).append(r)

    total_cost = total_gross = 0.0
    updates, no_snap = [], 0
    for day, day_rows in sorted(by_day.items()):
        table = load_spreads(day)
        for tid, code, qty, exit_ts, exit_px, pnl, reason in day_rows:
            if reason and reason.startswith("reconciled"):
                # 実際には約定していなかった行。コストも損益も0のまま
                updates.append((0.0, 0.0, tid))
                continue
            sp = spread_at(table, code, exit_ts)
            if sp is None:
                sp = 5.0          # スナップショットが無い日は控えめな既定値
                no_snap += 1
            cost = abs(exit_px or 0) * (qty or 0) * (sp / 2 + SLIPPAGE_BPS) / 10000
            total_cost += cost
            total_gross += pnl or 0.0
            updates.append((cost, (pnl or 0.0) - cost, tid))
        print(f"  {day}: {len(day_rows)}件")

    print(f"\n対象 {len(updates)}件 (スナップショット無しで既定値を使った: {no_snap}件)")
    print(f"コスト抜きの損益   : {total_gross:+,.0f}円")
    print(f"算入したコスト合計 : {total_cost:+,.0f}円  (1取引あたり平均 {total_cost/max(len(updates),1):,.0f}円)")
    print(f"実質の損益         : {total_gross - total_cost:+,.0f}円")

    if args.dry_run:
        print("\n--dry-run のため書き込みませんでした")
        return
    conn.executemany("UPDATE trades SET cost = ?, pnl_net = ? WHERE id = ?", updates)
    conn.commit()
    print("\n書き込み完了")


if __name__ == "__main__":
    main()
