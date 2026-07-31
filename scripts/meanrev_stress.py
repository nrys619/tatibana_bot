"""平均回帰(A7など)が急落局面で耐えるかを調べる.

「5日で-10%以上下げたら買う」は落ちるナイフを掴む戦略で、
**下げ相場で大怪我するのが最大の弱点**。平均が良くても、
一度の暴落で全部吹き飛ぶなら採用できない。

ここで見るもの:
  1. 市場全体が下げている局面だけを抜き出したときの成績
  2. 最悪の1取引・最悪の月がどれくらいか
  3. 資金曲線の最大下落幅 (どこまで含み損に耐える必要があるか)
  4. 同時に何銘柄が条件に該当するか (暴落時は一斉に該当して集中投資になる)

usage:
  python scripts/meanrev_stress.py --rule A7
"""

from __future__ import annotations

import argparse
import glob
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("stress")

COST_PCT = 0.05

RULES = {
    "A7": ("5日で-10%以上下げたら買う", lambda d: d["ret_5d"] <= -10),
    "A3": ("20日平均から-2σ以下を買う", lambda d: d["z20"] <= -2),
    "A5": ("RSI30以下を買う", lambda d: d["rsi14"] <= 30),
    "A1": ("1日で-5%以上下げたら買う", lambda d: d["ret_1d"] <= -5),
}


def load(daily_dir: str) -> pd.DataFrame:
    rows = []
    for f in sorted(glob.glob(f"{daily_dir}/*.parquet")):
        d = pd.read_parquet(f).sort_index()
        if len(d) < 80:
            continue
        c, h, lo, o, v = d["close"], d["high"], d["low"], d["open"], d["volume"]
        x = pd.DataFrame(index=d.index)
        x["code"] = Path(f).stem
        x["date"] = d.index
        x["open"] = o.to_numpy(dtype=float)
        x["turnover"] = (c * v).to_numpy(dtype=float)
        x["ret_1d"] = c.pct_change(1) * 100
        x["ret_5d"] = c.pct_change(5) * 100
        ma20, sd20 = c.rolling(20).mean(), c.rolling(20).std()
        x["z20"] = (c - ma20) / sd20
        diff = c.diff()
        up = diff.clip(lower=0).rolling(14).mean()
        dn = (-diff.clip(upper=0)).rolling(14).mean()
        x["rsi14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
        rows.append(x)
    return pd.concat(rows, ignore_index=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily-dir", default="data/daily_all")
    ap.add_argument("--min-turnover", type=float, default=1e9)
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--rules", nargs="+", default=["A7", "A3", "A5", "A1"])
    args = ap.parse_args()

    panel = load(args.daily_dir).sort_values(["code", "date"])
    g = panel.groupby("code")["open"]
    entry = g.shift(-1)
    panel["fwd"] = (g.shift(-1 - args.hold) / entry - 1) * 100    # 間引く前に計算
    panel = panel[panel["turnover"] >= args.min_turnover].dropna(subset=["fwd"])
    logger.info("%d銘柄 / %d行", panel["code"].nunique(), len(panel))

    # 市場全体の動き = その日の全銘柄の平均リターン (地合いの代理)
    mkt = panel.groupby("date")["fwd"].mean()

    for key in args.rules:
        desc, fn = RULES[key]
        sel = panel[fn(panel).fillna(False)].copy()
        sel["net"] = sel["fwd"] - COST_PCT
        print()
        print("=" * 76)
        print(f"  {key}: {desc}   ({args.hold}日保有 / 該当{len(sel):,}件)")
        print("=" * 76)
        print(f"  平均 {sel['net'].mean():+.3f}%  勝率 {(sel['net']>0).mean()*100:.0f}%")

        # 1) 地合い別
        print("\n  --- 地合い別 (その日の全銘柄平均リターンで3分割) ---")
        q = mkt.quantile([1/3, 2/3])
        for label, lo_, hi_ in [("下げ相場", -99, q.iloc[0]),
                                ("横ばい", q.iloc[0], q.iloc[1]),
                                ("上げ相場", q.iloc[1], 99)]:
            days = mkt[(mkt > lo_) & (mkt <= hi_)].index
            s = sel[sel["date"].isin(days)]
            if len(s):
                print(f"    {label:<8} {len(s):>7,}件  平均{s['net'].mean():+.3f}%"
                      f"  勝率{(s['net']>0).mean()*100:>3.0f}%"
                      f"  最悪{s['net'].min():+.1f}%")

        # 2) 最悪の月
        print("\n  --- 月別の成績 (最悪の5ヶ月) ---")
        m = sel.groupby(sel["date"].dt.to_period("M"))["net"].agg(["mean", "count"])
        for period, r in m.nsmallest(5, "mean").iterrows():
            print(f"    {period}  平均{r['mean']:+.2f}%  ({int(r['count'])}件)")
        print(f"    ※月別でマイナスだったのは {(m['mean']<0).sum()}/{len(m)}ヶ月")

        # 3) 同時該当数 (暴落時に集中投資になっていないか)
        per_day = sel.groupby("date").size()
        print(f"\n  --- 1日あたりの該当銘柄数 ---")
        print(f"    中央値{per_day.median():.0f}銘柄 / 最大{per_day.max():.0f}銘柄"
              f" ({per_day.idxmax().date()})")
        worst_day = sel.groupby("date")["net"].mean().nsmallest(3)
        print("    最悪の日 (その日に該当した銘柄の平均):")
        for d, v in worst_day.items():
            n = per_day.get(d, 0)
            print(f"      {d.date()}  {v:+.2f}%  ({n}銘柄が該当)")

        # 4) 資金曲線の最大下落幅 (毎日等金額で回した想定)
        daily = sel.groupby("date")["net"].mean() / args.hold   # 保有日数で分散
        curve = daily.cumsum()
        dd = (curve - curve.cummax()).min()
        print(f"\n  --- 資金曲線 (毎日等金額で回した想定) ---")
        print(f"    累積 {curve.iloc[-1]:+.1f}%  最大下落幅 {dd:.1f}%")


if __name__ == "__main__":
    main()
