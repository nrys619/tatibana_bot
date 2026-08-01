"""同業種の中での出遅れ・独歩安を狙えるか (バックテスト3位: 未使用の情報).

JPXの一覧には33業種の分類が入っている。今まで一度も使っていない。
「同業他社が上がっているのに1社だけ下げている」は有力な歪みとして知られる。

usage: python scripts/sector_relative.py
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("sector")
COST_PCT = 0.05


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily-dir", default="data/daily_all")
    ap.add_argument("--min-turnover", type=float, default=1e9)
    ap.add_argument("--holds", type=int, nargs="+", default=[1, 5])
    args = ap.parse_args()

    uni = json.loads(Path("data/universe_jpx.json").read_text())
    rows = []
    for f in sorted(glob.glob(f"{args.daily_dir}/*.parquet")):
        code = Path(f).stem
        d = pd.read_parquet(f).sort_index()
        if len(d) < 60:
            continue
        c, v, o = d["close"], d["volume"], d["open"]
        x = pd.DataFrame(index=d.index)
        x["code"] = code
        x["date"] = d.index
        x["open"] = o.to_numpy(dtype=float)
        x["turnover"] = (c * v).to_numpy(dtype=float)
        x["ret_1d"] = (c.pct_change(1) * 100).to_numpy(dtype=float)
        x["ret_5d"] = (c.pct_change(5) * 100).to_numpy(dtype=float)
        x["sector"] = uni.get(code, {}).get("sector", "?")
        rows.append(x)
    panel = pd.concat(rows, ignore_index=True).sort_values(["code", "date"])

    g = panel.groupby("code")["open"]
    entry = g.shift(-1)
    for h in args.holds:
        panel[f"fwd_{h}"] = (g.shift(-1 - h) / entry - 1) * 100
    panel = panel[panel["turnover"] >= args.min_turnover].dropna(subset=["ret_1d", "ret_5d"])

    # 同業種の平均に対する相対 (その日・その業種の中での位置)
    for col in ("ret_1d", "ret_5d"):
        sec = panel.groupby(["date", "sector"])[col].transform("mean")
        cnt = panel.groupby(["date", "sector"])[col].transform("size")
        panel[f"rel_{col}"] = panel[col] - sec
        panel["sector_n"] = cnt
    panel = panel[panel["sector_n"] >= 5]     # 業種内に5銘柄以上ある日だけ
    logger.info("%d銘柄 / %d行 / %d業種", panel["code"].nunique(), len(panel),
                panel["sector"].nunique())

    RULES = {
        "業種平均より5%以上出遅れ(1日)を買う": lambda d: d["rel_ret_1d"] <= -5,
        "業種平均より3%以上出遅れ(1日)を買う": lambda d: d["rel_ret_1d"] <= -3,
        "業種平均より10%以上出遅れ(5日)を買う": lambda d: d["rel_ret_5d"] <= -10,
        "業種平均より5%以上先行(1日)を売る": lambda d: d["rel_ret_1d"] >= 5,
        "業種平均より10%以上先行(5日)を売る": lambda d: d["rel_ret_5d"] >= 10,
    }
    SHORT = {"業種平均より5%以上先行(1日)を売る", "業種平均より10%以上先行(5日)を売る"}

    for hold in args.holds:
        lab = f"fwd_{hold}"
        d0 = panel.dropna(subset=[lab])
        base = d0[lab].mean() - COST_PCT
        print()
        print("=" * 92)
        print(f"  【{hold}日保有】 何もしない場合: {base:+.3f}%")
        print("=" * 92)
        print(f"{'ルール':<38}{'件数':>9}{'平均':>9}{'市場との差':>11}{'勝率':>7}  期間3分割")
        print("-" * 92)
        for name, fn in RULES.items():
            sel = d0[fn(d0).fillna(False)]
            if len(sel) < 300:
                print(f"{name:<38}{len(sel):>9}  該当が少なすぎる")
                continue
            sign = -1 if name in SHORT else 1
            r = sel[lab].to_numpy() * sign - COST_PCT
            segs = np.array_split(np.argsort(sel["date"].to_numpy()), 3)
            d3 = [float(r[s].mean()) for s in segs]
            ok = "★" if all(x > 0 for x in d3) and r.mean() > base else " "
            print(f"{name:<38}{len(sel):>9,}{r.mean():>+8.3f}%{r.mean()-base:>+10.3f}%"
                  f"{(r > 0).mean()*100:>6.0f}%  {' / '.join(f'{x:+.2f}' for x in d3)} {ok}")
    print("\n※★= 市場に勝ち、かつ期間3分割の全区間でプラス")


if __name__ == "__main__":
    main()
