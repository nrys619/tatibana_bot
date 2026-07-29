"""取引記録と板スナップショットの永続化.

- TradeLog: トレードとシグナルを SQLite に記録する。
  ④リスクリミットの判定 (当日損益・連敗数) の入力にもなる。
- SnapshotRecorder: 板スナップショットを日別 JSONL に追記する (③のML学習データ)。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_ts TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_reason TEXT,
    exit_ts TEXT,
    exit_price REAL,
    pnl REAL,
    exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS shadow_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_ts TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_reason TEXT,
    blocked_by TEXT NOT NULL,
    exit_ts TEXT,
    exit_price REAL,
    pnl REAL,
    cost REAL,
    pnl_net REAL,
    exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT,
    acted INTEGER NOT NULL
);
"""


class TradeLog:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_SCHEMA)
        # 2026-07-30: 損益にコストを算入するため列を追加。
        # 既存の pnl は「コスト抜き」のまま残す (過去データとの比較を壊さないため)。
        # 実質の損益は pnl - COALESCE(cost, 0) で求める。
        for col, decl in [("cost", "REAL"), ("pnl_net", "REAL")]:
            try:
                self._conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # 既にある
        self._conn.commit()

    def open_trade(
        self,
        code: str,
        side: str,
        quantity: int,
        ts: datetime,
        entry_price: float,
        reason: str,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO trades (code, side, quantity, entry_ts, entry_price, entry_reason)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (code, side, quantity, ts.isoformat(), entry_price, reason),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def close_trade(
        self,
        trade_id: int,
        ts: datetime,
        exit_price: float,
        pnl: float,
        reason: str,
        cost: float = 0.0,
    ) -> None:
        """pnl はコスト抜きの値。cost を引いた実質損益を pnl_net に入れる."""
        self._conn.execute(
            "UPDATE trades SET exit_ts = ?, exit_price = ?, pnl = ?, exit_reason = ?,"
            " cost = ?, pnl_net = ? WHERE id = ?",
            (ts.isoformat(), exit_price, pnl, reason, cost, pnl - cost, trade_id),
        )
        self._conn.commit()

    def open_shadow(self, code: str, side: str, quantity: int, ts: datetime,
                    entry_price: float, reason: str, blocked_by: str) -> int:
        """見送った合図を「もし入っていたら」として記録する (実注文はしない)."""
        cur = self._conn.execute(
            "INSERT INTO shadow_trades (code, side, quantity, entry_ts, entry_price,"
            " entry_reason, blocked_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (code, side, quantity, ts.isoformat(), entry_price, reason, blocked_by))
        self._conn.commit()
        return int(cur.lastrowid)

    def close_shadow(self, shadow_id: int, ts: datetime, exit_price: float,
                     pnl: float, reason: str, cost: float = 0.0) -> None:
        self._conn.execute(
            "UPDATE shadow_trades SET exit_ts = ?, exit_price = ?, pnl = ?,"
            " exit_reason = ?, cost = ?, pnl_net = ? WHERE id = ?",
            (ts.isoformat(), exit_price, pnl, reason, cost, pnl - cost, shadow_id))
        self._conn.commit()

    def open_trades(self) -> list[dict]:
        """まだ決済されていない取引 (エンジンが落ちた等で宙に浮いた建玉の候補)."""
        cur = self._conn.execute(
            "SELECT id, code, side, quantity, entry_ts, entry_price, entry_reason"
            " FROM trades WHERE exit_ts IS NULL ORDER BY id")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def today_realized_pnl(self, day: str) -> float:
        """day ("YYYY-MM-DD") に決済したトレードの確定損益合計."""
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades"
            " WHERE pnl IS NOT NULL AND substr(exit_ts, 1, 10) = ?",
            (day,),
        )
        return float(cur.fetchone()[0])

    def recent_results(self, n: int, day: str | None = None) -> list[float]:
        """直近 n 件の決済済みトレード損益 (新しい順)。day指定でその日の分だけ."""
        if day:
            cur = self._conn.execute(
                "SELECT pnl FROM trades WHERE pnl IS NOT NULL"
                " AND substr(exit_ts, 1, 10) = ? ORDER BY id DESC LIMIT ?",
                (day, n),
            )
        else:
            cur = self._conn.execute(
                "SELECT pnl FROM trades WHERE pnl IS NOT NULL ORDER BY id DESC LIMIT ?",
                (n,),
            )
        return [float(row[0]) for row in cur.fetchall()]

    def log_signal(
        self,
        ts: datetime,
        code: str,
        side: str,
        confidence: float,
        reason: str,
        acted: bool,
    ) -> None:
        self._conn.execute(
            "INSERT INTO signals (ts, code, side, confidence, reason, acted)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ts.isoformat(), code, side, confidence, reason, int(acted)),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class SnapshotRecorder:
    def __init__(self, data_dir: str | Path):
        self._dir = Path(data_dir) / "snapshots"
        self._dir.mkdir(parents=True, exist_ok=True)

    def record(self, code: str, ts: datetime, features: dict) -> None:
        row = {"ts": ts.isoformat(), "code": code, **features}
        with open(self._dir / f"{ts:%Y%m%d}.jsonl", "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def load_day(self, day: str) -> pd.DataFrame:
        """day ("YYYYMMDD") のスナップショットを DataFrame で返す (なければ空)."""
        path = self._dir / f"{day}.jsonl"
        if not path.exists():
            return pd.DataFrame()
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_datetime(df["ts"])
        return df
