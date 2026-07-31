"""平均回帰に「地合いフィルタ」を掛けたら実用になるかを検証する.

A3(20日平均から-2σ以下を買う)は 上げ相場+3.633% / 下げ相場-2.807% と
地合いで真っ二つに割れた。**下げ相場で買わなければ実用になるのでは？**

ただし「その日が上げ相場かどうか」は当日の終値を見ないと分からない = カンニング。
**前日までの情報だけ**で地合いを判定して検証する。

usage:
  python scripts/meanrev_regime.py --hold 5
"""

from __future__ import annotations

import argparse
import glob
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("regime")
COST_PCT = 0.05

RULES = {
    "A3 -2σ以下を買う": lambda d: d["z20"] <= -2,
    "A5 RSI30以下を買う": lambda d: d["rsi14"] <= 30,
    "A1 1日-5%以上を買う": lambda d: d["ret_1d"] <= -5,
    "A7 5日-10%以上を買う": lambda d: d["ret_5d"] <= -10,
}


def load(daily_dir: str) -> pd.DataFrame:
    rows = []
    for f in sorted(glob.glob(f"{daily_dir}/*.parquet")):
        d = pd.read_parquet(f).sort_index()
        if len(d) < 80:
            continue
        c, v, o = d["close"], d["volume"], d["open"]
        x = pd.DataFrame(index=d.index)
        x["code"] = Path(f).stem
        x["date"] = d.index
        x["open"] = o.to_numpy(dtype=float)
        x["close"] = c.to_numpy(dtype=float)
        x["turnover"] = (c * v).to_numpy(dtype=float)
        x["ret_1d"] = c.pct_change(1) * 100
        x["ret_5d"] = c.pct_change(5) * 100
        ma20, sd20 = c.rolling(20).mean(), c.rolling(20).std()
        x["z20"] = (c - ma20) / sd20
        diff = c.diff()
        up, dn = diff.clip(lower=0).rolling(14).mean(), (-diff.clip(upper=0)).rolling(14).mean()
        x["rsi14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
        rows.append(x)
    return pd.concat(rows, ignore_index=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily-dir", default="data/daily_all")
    ap.add_argument("--min-turnover", type=float, default=1e9)
    ap.add_argument("--hold", type=int, default=5)
    args = ap.parse_args()

    panel = load(args.daily_dir).sort_values(["code", "date"])
    g = panel.groupby("code")["open"]
    entry = g.shift(-1)
    panel["fwd"] = (g.shift(-1 - args.hold) / entry - 1) * 100
    panel = panel[panel["turnover"] >= args.min_turnover].dropna(subset=["fwd"])

    # --- 地合い: 全銘柄の等加重指数を作り、**前日までの情報だけ**で判定 ---
    idx = panel.groupby("date")["close"].mean().sort_index()
    ma25 = idx.rolling(25).mean()
    # 「前日の指数が25日平均より上か」= 当日の朝には分かっている情報
    up_market = (idx > ma25).shift(1)
    logger.info("上げ相場と判定された日: %d / %d日 (%.0f%%)",
                up_market.sum(), up_market.notna().sum(),
                up_market.mean() * 100)
    panel["up_market"] = panel["date"].map(up_market)

    print()
    print(f"【{args.hold}日保有】往復コスト{COST_PCT}%込み")
    print(f"{'ルール':<24}{'条件':<16}{'件数':>8}{'平均':>9}{'勝率':>7}"
          f"{'累積':>9}{'最大下落':>10}  期間3分割")
    print("-" * 100)
    for name, fn in RULES.items():
        base = panel[fn(panel).fillna(False)].copy()
        for label, sub in [("全部", base),
                           ("上げ相場のみ", base[base["up_market"] == True]),
                           ("下げ相場のみ", base[base["up_market"] == False])]:
            if len(sub) < 100:
                continue
            s = sub.copy()
            s["net"] = s["fwd"] - COST_PCT
            daily = s.groupby("date")["net"].mean() / args.hold
            curve = daily.cumsum()
            dd = float((curve - curve.cummax()).min())
            segs = np.array_split(np.argsort(s["date"].to_numpy()), 3)
            arr = s["net"].to_numpy()
            d3 = [float(arr[x].mean()) for x in segs]
            ok = "★" if curve.iloc[-1] > 0 and all(x > 0 for x in d3) else " "
            print(f"{name:<24}{label:<16}{len(s):>8,}{s['net'].mean():>+8.3f}%"
                  f"{(s['net']>0).mean()*100:>6.0f}%{curve.iloc[-1]:>+8.1f}%{dd:>9.1f}%"
                  f"  {' / '.join(f'{x:+.2f}' for x in d3)} {ok}")
        print()
    print("※★= 累積プラス かつ 期間3分割の全区間でプラス")
    print("※地合いは「前日の全銘柄平均が25日平均より上か」= 当日の朝に分かる情報のみ")


if __name__ == "__main__":
    main()
