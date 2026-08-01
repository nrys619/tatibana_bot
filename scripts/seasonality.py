"""曜日・月・月内の位置に偏りがあるかを調べる (バックテスト2位: すぐ試せる).

全銘柄の翌日リターンを、曜日/月/月内の位置で切って比べる。
効くなら「その日は買わない」の1行で実装できる。

**カンニング対策**: 翌日の始値で入り、H日後の始値で出る。往復コストを引く。
期間3分割で符号が揃うかを見る (揃わなければノイズ)。

usage: python scripts/seasonality.py
"""

from __future__ import annotations

import argparse
import glob
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("season")
COST_PCT = 0.05
WD = ["月", "火", "水", "木", "金"]


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
        x["turnover"] = (c * v).to_numpy(dtype=float)
        rows.append(x)
    return pd.concat(rows, ignore_index=True)


def show(title: str, groups: list[tuple[str, pd.DataFrame]], base: float,
         hold: int) -> None:
    print(f"\n  --- {title} ---")
    print(f"  {'区分':<14}{'件数':>10}{'平均':>9}{'市場との差':>11}{'勝率':>7}  期間3分割")
    for label, g in groups:
        if len(g) < 500:
            continue
        r = g["net"].to_numpy()
        segs = np.array_split(np.argsort(g["date"].to_numpy()), 3)
        d3 = [float(r[s].mean()) for s in segs]
        same = all(x > 0 for x in d3) or all(x < 0 for x in d3)
        mark = "★" if same else " "
        print(f"  {label:<14}{len(g):>10,}{r.mean():>+8.3f}%{r.mean()-base:>+10.3f}%"
              f"{(r > 0).mean()*100:>6.0f}%  {' / '.join(f'{x:+.3f}' for x in d3)} {mark}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily-dir", default="data/daily_all")
    ap.add_argument("--min-turnover", type=float, default=1e9)
    ap.add_argument("--holds", type=int, nargs="+", default=[1, 5])
    args = ap.parse_args()

    panel = load(args.daily_dir).sort_values(["code", "date"])
    g = panel.groupby("code")["open"]
    entry = g.shift(-1)
    for h in args.holds:
        panel[f"fwd_{h}"] = (g.shift(-1 - h) / entry - 1) * 100
    panel = panel[panel["turnover"] >= args.min_turnover]
    # 買う日 = signal_date の翌営業日。曜日は「買う日」で切る
    panel["buy_date"] = panel.groupby("code")["date"].shift(-1)
    panel = panel.dropna(subset=["buy_date"])
    panel["wd"] = pd.to_datetime(panel["buy_date"]).dt.weekday
    panel["month"] = pd.to_datetime(panel["buy_date"]).dt.month
    panel["dom"] = pd.to_datetime(panel["buy_date"]).dt.day
    logger.info("%d銘柄 / %d行", panel["code"].nunique(), len(panel))

    for hold in args.holds:
        d = panel.dropna(subset=[f"fwd_{hold}"]).copy()
        d["net"] = d[f"fwd_{hold}"] - COST_PCT
        base = d["net"].mean()
        print()
        print("=" * 82)
        print(f"  【{hold}日保有】 全体の平均: {base:+.3f}%  ({len(d):,}件)")
        print("=" * 82)
        show("買う日の曜日", [(WD[i], d[d["wd"] == i]) for i in range(5)], base, hold)
        show("買う日の月", [(f"{m}月", d[d["month"] == m]) for m in range(1, 13)],
             base, hold)
        show("月内の位置", [("上旬(1-10日)", d[d["dom"] <= 10]),
                            ("中旬(11-20日)", d[(d["dom"] > 10) & (d["dom"] <= 20)]),
                            ("下旬(21日-)", d[d["dom"] > 20])], base, hold)
    print("\n※★= 期間3分割で符号が揃った (揃わないものはノイズ)")
    print("※往復コスト0.05%を引いた後")


if __name__ == "__main__":
    main()
