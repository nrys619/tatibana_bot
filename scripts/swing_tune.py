"""A3の保有期間と出口・空売り側を詰める (バックテスト4位・5位).

現状: 「下げ基調 x -2σ以下を買い、5日保有」で 累積+33.3% / 最大下落-13.4%
ここで試すこと:
  - 保有期間 2/3/5/7/10日
  - 出口を「z値が0に戻ったら売る」に変えた場合
  - 入口の閾値 -1.5σ / -2σ / -2.5σ / -3σ
  - 空売り側 (買われすぎを売る) を**上げ基調に限定**した場合
    ※前回は地合いを限定せずに全滅した

usage: python scripts/swing_tune.py
"""

from __future__ import annotations

import argparse
import glob
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("tune")
COST_PCT = 0.05


def load(daily_dir: str) -> pd.DataFrame:
    rows = []
    for f in sorted(glob.glob(f"{daily_dir}/*.parquet")):
        d = pd.read_parquet(f).sort_index()
        if len(d) < 60:
            continue
        c, v, o = d["close"], d["volume"], d["open"]
        x = pd.DataFrame(index=d.index)
        x["code"] = Path(f).stem
        x["date"] = d.index
        x["open"] = o.to_numpy(dtype=float)
        x["close"] = c.to_numpy(dtype=float)
        x["turnover"] = (c * v).to_numpy(dtype=float)
        ma20, sd20 = c.rolling(20).mean(), c.rolling(20).std()
        x["z20"] = ((c - ma20) / sd20).to_numpy(dtype=float)
        rows.append(x)
    return pd.concat(rows, ignore_index=True).sort_values(["code", "date"])


def stats(s: pd.DataFrame, col: str, hold: int, sign: int = 1) -> dict:
    r = s[col].to_numpy() * sign - COST_PCT
    daily = pd.Series(r, index=s["date"].to_numpy())
    daily = daily.groupby(level=0).mean() / hold
    curve = daily.sort_index().cumsum()
    segs = np.array_split(np.argsort(s["date"].to_numpy()), 3)
    return {"n": len(s), "avg": r.mean(), "win": (r > 0).mean() * 100,
            "cum": float(curve.iloc[-1]) if len(curve) else 0.0,
            "dd": float((curve - curve.cummax()).min()) if len(curve) else 0.0,
            "d3": [float(r[x].mean()) for x in segs]}


def line(label: str, m: dict) -> None:
    ok = "★" if m["cum"] > 0 and all(x > 0 for x in m["d3"]) else " "
    print(f"{label:<30}{m['n']:>8,}{m['avg']:>+8.3f}%{m['win']:>6.0f}%"
          f"{m['cum']:>+8.1f}%{m['dd']:>9.1f}%  "
          f"{' / '.join(f'{x:+.2f}' for x in m['d3'])} {ok}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily-dir", default="data/daily_all")
    ap.add_argument("--min-turnover", type=float, default=1e9)
    args = ap.parse_args()

    panel = load(args.daily_dir)
    holds = [2, 3, 5, 7, 10]
    g = panel.groupby("code")["open"]
    entry = g.shift(-1)
    for h in holds:
        panel[f"fwd_{h}"] = (g.shift(-1 - h) / entry - 1) * 100
    panel = panel[panel["turnover"] >= args.min_turnover]

    idx = panel.groupby("date")["close"].mean().sort_index()
    down = (idx < idx.rolling(25).mean()).shift(1)
    panel["down_market"] = panel["date"].map(down)
    logger.info("%d銘柄 / %d行", panel["code"].nunique(), len(panel))

    hdr = f"{'条件':<30}{'件数':>8}{'平均':>9}{'勝率':>7}{'累積':>9}{'最大下落':>10}  期間3分割"
    print("\n" + "=" * 96)
    print("  【保有期間を変える】 下げ基調 x -2σ以下を買う")
    print("=" * 96); print(hdr); print("-" * 96)
    sel = panel[(panel["z20"] <= -2) & (panel["down_market"] == True)]
    for h in holds:
        s = sel.dropna(subset=[f"fwd_{h}"])
        line(f"{h}日保有", stats(s, f"fwd_{h}", h))

    print("\n" + "=" * 96)
    print("  【入口の閾値を変える】 下げ基調 / 5日保有")
    print("=" * 96); print(hdr); print("-" * 96)
    for z in [-1.5, -2.0, -2.5, -3.0]:
        s = panel[(panel["z20"] <= z) & (panel["down_market"] == True)].dropna(subset=["fwd_5"])
        if len(s) >= 300:
            line(f"{z}σ以下を買う", stats(s, "fwd_5", 5))

    print("\n" + "=" * 96)
    print("  【空売り側】 買われすぎを売る (前回は地合いを限定せず全滅した)")
    print("=" * 96); print(hdr); print("-" * 96)
    for z in [2.0, 2.5, 3.0]:
        for mk, lbl in [(True, "下げ基調"), (False, "上げ基調"), (None, "全部")]:
            s = panel[panel["z20"] >= z]
            if mk is not None:
                s = s[s["down_market"] == mk]
            s = s.dropna(subset=["fwd_5"])
            if len(s) >= 300:
                line(f"+{z}σ以上を売る x {lbl}", stats(s, "fwd_5", 5, sign=-1))

    print("\n※★= 累積プラス かつ 期間3分割の全区間プラス / 往復コスト0.05%込み")


if __name__ == "__main__":
    main()
