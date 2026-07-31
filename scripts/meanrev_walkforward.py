"""(A) 平均回帰: 「行き過ぎたら逆に張る」に優位があるかを検証する.

今のボットは順張り一本 (動いた方向に乗る)。方向を当てる試みは3連敗した:
  - ML第2ラウンド(板) AUC 0.49 / スイング(日足) 市場に負け / 実機のデイトレ 実質マイナス
一方、銘柄選定 (どれだけ動くか) は AUC 0.876 で本物だった。
「振れ幅は読めるが方向は読めない」なら、**行き過ぎの反動**を取る方が筋が良いかもしれない。
真逆をまだ一度も試していないので、ここで測る。

**カンニングをしない作り** (これまで3回やらかしたので厳守):
  - 将来リターンは**間引く前の連続した日付**で計算する
  - 特徴量は当日の終値まで。**翌日の始値で入り、H日後の始値で出る**
  - 往復コストを必ず引く
  - 「全銘柄を等しく持った場合」と比べる
  - 期間3分割で符号が揃うかを見る

usage:
  python scripts/meanrev_walkforward.py --daily-dir data/daily_all --min-turnover 1e9
"""

from __future__ import annotations

import argparse
import glob
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("meanrev")

COST_PCT = 0.05


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
        # --- 行き過ぎの度合い (すべて当日終値までの情報) ---
        x["ret_1d"] = c.pct_change(1) * 100
        x["ret_5d"] = c.pct_change(5) * 100
        ma20 = c.rolling(20).mean()
        sd20 = c.rolling(20).std()
        x["z20"] = (c - ma20) / sd20               # 20日平均から何σ離れたか
        x["from_ma20"] = (c / ma20 - 1) * 100      # 20日平均からの乖離率
        rng = (h - lo).rolling(14).mean()
        x["atr_pct"] = (rng / c * 100).to_numpy(dtype=float)
        # RSI(14)
        diff = c.diff()
        up = diff.clip(lower=0).rolling(14).mean()
        dn = (-diff.clip(upper=0)).rolling(14).mean()
        x["rsi14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
        # 当日の終値が日中レンジのどこか (0=安値引け, 1=高値引け)
        x["close_pos"] = ((c - lo) / (h - lo).replace(0, np.nan)).to_numpy(dtype=float)
        rows.append(x)
    return pd.concat(rows, ignore_index=True)


def add_fwd(panel: pd.DataFrame, holds: list[int]) -> pd.DataFrame:
    """翌日始値で入り、H日後の始値で出る。**間引く前**に計算すること."""
    panel = panel.sort_values(["code", "date"])
    g = panel.groupby("code")["open"]
    entry = g.shift(-1)
    for h in holds:
        panel[f"fwd_{h}"] = (g.shift(-1 - h) / entry - 1) * 100
    return panel


RULES = {
    "A1 大幅安を買う (1日で-5%以上)":        lambda d: d["ret_1d"] <= -5,
    "A2 大幅高を売る (1日で+5%以上)":        lambda d: d["ret_1d"] >= 5,
    "A3 20日平均から-2σ以下を買う":          lambda d: d["z20"] <= -2,
    "A4 20日平均から+2σ以上を売る":          lambda d: d["z20"] >= 2,
    "A5 RSI30以下を買う (売られすぎ)":       lambda d: d["rsi14"] <= 30,
    "A6 RSI70以上を売る (買われすぎ)":       lambda d: d["rsi14"] >= 70,
    "A7 5日で-10%以上下げたら買う":          lambda d: d["ret_5d"] <= -10,
    "A8 5日で+10%以上上げたら売る":          lambda d: d["ret_5d"] >= 10,
    "A9 安値引け(下位10%)を買う":            lambda d: d["close_pos"] <= 0.1,
    "A10 高値引け(上位10%)を売る":           lambda d: d["close_pos"] >= 0.9,
}
SHORT_RULES = {"A2", "A4", "A6", "A8", "A10"}   # 売り (リターンの符号を反転)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily-dir", default="data/daily_all")
    ap.add_argument("--min-turnover", type=float, default=1e9)
    ap.add_argument("--holds", type=int, nargs="+", default=[1, 3, 5])
    args = ap.parse_args()

    logger.info("読み込み中: %s", args.daily_dir)
    panel = add_fwd(load(args.daily_dir), args.holds)   # ← 間引く前に将来リターン
    before = len(panel)
    panel = panel[panel["turnover"] >= args.min_turnover]
    logger.info("売買代金 %.0f億円以上: %d行 -> %d行 (%d銘柄)",
                args.min_turnover / 1e8, before, len(panel), panel["code"].nunique())

    for hold in args.holds:
        lab = f"fwd_{hold}"
        d0 = panel.dropna(subset=[lab])
        base = d0[lab].mean() - COST_PCT
        print()
        print("=" * 84)
        print(f"  【{hold}日保有】  何もしない場合 (全銘柄を等しく持つ): {base:+.3f}%")
        print("=" * 84)
        print(f"{'ルール':<32}{'該当数':>8}{'平均損益':>10}{'市場との差':>11}"
              f"{'勝率':>7}  期間3分割")
        print("-" * 84)
        for name, fn in RULES.items():
            sel = d0[fn(d0).fillna(False)]
            if len(sel) < 100:
                print(f"{name:<32}{len(sel):>8}   該当が少なすぎる")
                continue
            sign = -1 if name.split()[0] in SHORT_RULES else 1
            r = sel[lab].to_numpy() * sign - COST_PCT
            segs = np.array_split(np.argsort(sel["date"].to_numpy()), 3)
            d3 = [float(r[s].mean()) for s in segs]
            ok = "★" if all(x > 0 for x in d3) and r.mean() > base else " "
            print(f"{name:<32}{len(sel):>8}{r.mean():>+9.3f}%{r.mean()-base:>+10.3f}%"
                  f"{(r > 0).mean()*100:>6.0f}%  "
                  f"{' / '.join(f'{x:+.2f}' for x in d3)} {ok}")
    print()
    print("※★= 市場に勝ち、かつ期間3分割の全区間でプラス (これ以外は信用しない)")
    print("※往復コスト0.05%を引いた後の数字")


if __name__ == "__main__":
    main()
