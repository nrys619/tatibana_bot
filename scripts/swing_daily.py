"""スイング戦略を毎晩1回まわす (まずは記録だけ = paper).

デイトレとは完全に別建て。板は見ず、日足だけで判断する。
夜間バッチ(21:03)の後に走らせ、翌営業日の寄り付きで売買する想定で記録する。

**paper のうちは1円も動かさない。** 実機に出すかは、記録が貯まってから判断する。
デイトレでは「バックテストは黒字なのに実機は赤字」を経験しているので、
同じ轍を踏まないよう、まず「想定どおり売買できるか」を紙の上で確かめる。

usage:
  python scripts/swing_daily.py                 # 候補を選んで記録 (paper)
  python scripts/swing_daily.py --show          # 今の持ち玉と成績を見るだけ
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from tatibana_bot.swing.strategy import (
    SwingParams,
    find_candidates,
    market_is_down,
    shares_for,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("swing")

DB = "data/swing.sqlite3"
DAILY_DIR = Path("data/daily_all")
COST_PCT = 0.05

_SCHEMA = """
CREATE TABLE IF NOT EXISTS swing_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT,
    signal_date TEXT NOT NULL,      -- 条件を満たした日 (終値ベース)
    entry_date TEXT,                -- 実際に買った日 (翌営業日の寄り)
    entry_price REAL,
    quantity INTEGER,
    z20 REAL,
    planned_exit_date TEXT,         -- 5営業日後
    exit_date TEXT,
    exit_price REAL,
    pnl REAL,
    cost REAL,
    pnl_net REAL,
    mode TEXT NOT NULL DEFAULT 'paper'
);
CREATE TABLE IF NOT EXISTS swing_days (
    day TEXT PRIMARY KEY,
    market_down INTEGER,
    candidates INTEGER,
    note TEXT
);
"""


def db() -> sqlite3.Connection:
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def load_bars() -> dict[str, pd.DataFrame]:
    bars = {}
    for f in DAILY_DIR.glob("*.parquet"):
        d = pd.read_parquet(f).sort_index()
        if len(d) >= 30:
            bars[f.stem] = d
    return bars


def names() -> dict[str, str]:
    p = Path("data/universe_jpx.json")
    if not p.exists():
        return {}
    return {k: v.get("name", "") for k, v in json.loads(p.read_text()).items()}


def settle(conn: sqlite3.Connection, bars: dict[str, pd.DataFrame]) -> int:
    """保有期間を過ぎた玉を、その日の始値で決済したことにする."""
    n = 0
    for row in conn.execute("""SELECT id, code, entry_price, quantity, planned_exit_date
                               FROM swing_trades WHERE exit_date IS NULL
                               AND entry_date IS NOT NULL""").fetchall():
        tid, code, ep, qty, plan = row
        d = bars.get(code)
        if d is None:
            continue
        future = d[d.index > pd.Timestamp(plan)]
        if future.empty:
            continue                       # まだその日が来ていない
        px = float(future["open"].iloc[0])
        exit_date = future.index[0].date().isoformat()
        pnl = (px - ep) * qty
        cost = (ep + px) * qty * COST_PCT / 100 / 2
        conn.execute("""UPDATE swing_trades SET exit_date=?, exit_price=?, pnl=?,
                        cost=?, pnl_net=? WHERE id=?""",
                     (exit_date, px, pnl, cost, pnl - cost, tid))
        n += 1
    conn.commit()
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="記録を見るだけ")
    ap.add_argument("--live", action="store_true", help="実際に発注する (まだ使わない)")
    args = ap.parse_args()
    p = SwingParams()
    conn = db()

    if not DAILY_DIR.exists():
        raise SystemExit(f"{DAILY_DIR} がありません。build_universe.py --download を先に")

    bars = load_bars()
    nm = names()
    closed = settle(conn, bars)
    if closed:
        logger.info("保有期間を過ぎた %d件を決済しました", closed)

    if not args.show:
        # --- 地合い判定 (全銘柄の等加重指数、前日までの情報のみ) ---
        idx = pd.concat([d["close"] for d in bars.values()], axis=1).mean(axis=1).dropna()
        down = market_is_down(idx, p)
        today = date.today().isoformat()

        held = {r[0] for r in conn.execute(
            "SELECT code FROM swing_trades WHERE exit_date IS NULL")}
        room = p.max_positions - len(held)

        if down is None:
            note = "地合いを判定できない (データ不足)"
            cands = []
        elif p.require_down_market and not down:
            note = "上げ基調なので今日は買わない (この戦略は押し目買い)"
            cands = []
        elif room <= 0:
            note = f"すでに上限{p.max_positions}銘柄を保有中"
            cands = []
        else:
            cands = [c for c in find_candidates(bars, p, nm) if c.code not in held][:room]
            note = f"下げ基調。候補{len(cands)}銘柄"

        conn.execute("INSERT OR REPLACE INTO swing_days VALUES (?,?,?,?)",
                     (today, int(bool(down)) if down is not None else None,
                      len(cands), note))
        logger.info("%s: %s", today, note)

        for c in cands:
            qty = shares_for(c, p)
            if qty <= 0:
                continue
            # 翌営業日の寄り付きで買う想定。実際の始値が分かるまで entry は空
            plan = (pd.Timestamp(today) + pd.tseries.offsets.BDay(p.hold_days + 1))
            conn.execute("""INSERT INTO swing_trades
                (code, name, signal_date, quantity, z20, planned_exit_date, mode)
                VALUES (?,?,?,?,?,?,?)""",
                (c.code, c.name, today, qty, c.z20, plan.date().isoformat(),
                 "live" if args.live else "paper"))
            logger.info("  候補: %s %s  %.1fσ  %d株 (約%,.0f円)",
                        c.code, c.name, c.z20, qty, c.close * qty)
        conn.commit()

        # 翌営業日の始値が判明している未約定分を埋める
        for row in conn.execute("""SELECT id, code, signal_date FROM swing_trades
                                   WHERE entry_date IS NULL""").fetchall():
            tid, code, sig = row
            d = bars.get(code)
            if d is None:
                continue
            future = d[d.index > pd.Timestamp(sig)]
            if future.empty:
                continue
            conn.execute("UPDATE swing_trades SET entry_date=?, entry_price=? WHERE id=?",
                         (future.index[0].date().isoformat(),
                          float(future["open"].iloc[0]), tid))
        conn.commit()

    # --- 成績 ---
    n, net, wins = conn.execute("""SELECT COUNT(*), COALESCE(SUM(pnl_net),0),
        SUM(CASE WHEN pnl_net>0 THEN 1 ELSE 0 END) FROM swing_trades
        WHERE exit_date IS NOT NULL""").fetchone()
    print()
    print("=" * 58)
    print("  スイング戦略 (A3: 下げ基調で売られすぎた大型株を5日保有)")
    print("=" * 58)
    if n:
        print(f"  決済済み {n}件  実質 {net:+,.0f}円  勝率 {wins/n*100:.0f}%")
    else:
        print("  決済済みの取引はまだありません")
    held = list(conn.execute("""SELECT code, name, entry_date, entry_price, quantity,
        z20, planned_exit_date FROM swing_trades WHERE exit_date IS NULL
        ORDER BY signal_date"""))
    if held:
        print(f"\n  保有中 {len(held)}件:")
        for code, name, ed, ep, q, z, plan in held:
            state = f"{ed} @{ep:,.0f}円" if ed else "翌営業日の寄りで買う予定"
            print(f"    {code} {name[:14]:14} {z:+.1f}σ {q}株  {state}  → {plan}に決済")
    print("\n  ※paperモード: 実際には1円も動いていません")
    conn.close()


if __name__ == "__main__":
    main()
