"""平均回帰の優位が「上場廃止バイアス」で作られていないかを調べる.

data/daily_all はJPXの**現在の**上場銘柄一覧から作った。倒産・上場廃止した
銘柄は載っていない。「売られすぎた株を買う」戦略にとって最大の敵は倒産なので、
この偏りは**この戦略に最も有利な方向**に効く。

直接は測れない (廃止銘柄のデータが無い) ので、間接的に見る:
  1. 財務的に危うい性質の銘柄 (株価が極端に安い、時価総額が小さい) を段階的に除外
     -> 除外しても優位が残るなら、倒産銘柄の寄与は小さいと言える
  2. 大型株だけに絞る -> 倒産リスクがほぼ無い母集団での成績
  3. 「買った後に上場廃止で消えた」に近い最悪ケース (-100%) を、
     該当のうち何%に混ぜたら優位が消えるかを逆算する

usage:
  python scripts/meanrev_survivorship.py
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
logger = logging.getLogger("surv")
COST_PCT = 0.05


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
        ma20, sd20 = c.rolling(20).mean(), c.rolling(20).std()
        x["z20"] = (c - ma20) / sd20
        rows.append(x)
    return pd.concat(rows, ignore_index=True)


def stats(s: pd.DataFrame, hold: int) -> dict:
    s = s.copy()
    s["net"] = s["fwd"] - COST_PCT
    daily = s.groupby("date")["net"].mean() / hold
    curve = daily.cumsum()
    segs = np.array_split(np.argsort(s["date"].to_numpy()), 3)
    arr = s["net"].to_numpy()
    return {"n": len(s), "avg": s["net"].mean(), "win": (s["net"] > 0).mean() * 100,
            "cum": float(curve.iloc[-1]) if len(curve) else 0.0,
            "dd": float((curve - curve.cummax()).min()) if len(curve) else 0.0,
            "d3": [float(arr[x].mean()) for x in segs]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily-dir", default="data/daily_all")
    ap.add_argument("--min-turnover", type=float, default=1e9)
    ap.add_argument("--hold", type=int, default=5)
    args = ap.parse_args()

    uni = json.loads(Path("data/universe_jpx.json").read_text())
    panel = load(args.daily_dir).sort_values(["code", "date"])
    g = panel.groupby("code")["open"]
    entry = g.shift(-1)
    panel["fwd"] = (g.shift(-1 - args.hold) / entry - 1) * 100
    panel = panel[panel["turnover"] >= args.min_turnover].dropna(subset=["fwd"])

    idx = panel.groupby("date")["close"].mean().sort_index()
    panel["up_market"] = panel["date"].map((idx > idx.rolling(25).mean()).shift(1))
    panel["market"] = panel["code"].map(lambda c: uni.get(c, {}).get("market", "?"))
    panel["size"] = panel["code"].map(lambda c: uni.get(c, {}).get("size", "?"))

    # A3を下げ基調で使う = 現時点で最有望
    sel = panel[(panel["z20"] <= -2) & (panel["up_market"] == False)]
    logger.info("対象: A3(-2σ以下を買う) x 下げ基調 = %d件", len(sel))

    print()
    print(f"【A3 × 下げ基調 / {args.hold}日保有】上場廃止バイアスの影響を測る")
    print(f"{'絞り込み':<30}{'件数':>8}{'平均':>9}{'勝率':>7}{'累積':>9}{'最大下落':>10}  期間3分割")
    print("-" * 100)

    filters = [
        ("そのまま (全銘柄)", sel),
        ("株価500円以上", sel[sel["close"] >= 500]),
        ("株価1000円以上", sel[sel["close"] >= 1000]),
        ("株価2000円以上", sel[sel["close"] >= 2000]),
        ("プライム市場のみ", sel[sel["market"] == "プライム（内国株式）"]),
        ("TOPIX Large70以上", sel[sel["size"].isin(["TOPIX Core30", "TOPIX Large70"])]),
        ("TOPIX Mid400以上", sel[sel["size"].isin(
            ["TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"])]),
        ("売買代金50億円以上", sel[sel["turnover"] >= 5e9]),
        ("売買代金100億円以上", sel[sel["turnover"] >= 1e10]),
    ]
    for label, sub in filters:
        if len(sub) < 100:
            print(f"{label:<30}{len(sub):>8}  該当が少なすぎる")
            continue
        m = stats(sub, args.hold)
        ok = "★" if m["cum"] > 0 and all(x > 0 for x in m["d3"]) else " "
        print(f"{label:<30}{m['n']:>8,}{m['avg']:>+8.3f}%{m['win']:>6.0f}%"
              f"{m['cum']:>+8.1f}%{m['dd']:>9.1f}%  "
              f"{' / '.join(f'{x:+.2f}' for x in m['d3'])} {ok}")

    # --- 廃止銘柄を混ぜたらどこで壊れるか ---
    print()
    print("【耐性テスト】買った銘柄のうち X% が上場廃止で -100% になったと仮定")
    base = stats(sel, args.hold)
    print(f"  実際の平均: {base['avg']:+.3f}%")
    for pct in [0.1, 0.3, 0.5, 1.0, 1.5, 2.0]:
        adj = base["avg"] * (1 - pct / 100) + (-100 - COST_PCT) * (pct / 100)
        mark = " ← ここで優位が消える" if adj <= 0 else ""
        print(f"  {pct:>4.1f}%が-100%になると: {adj:+.3f}%{mark}")
    print("\n  ※参考: 日本の上場企業の年間廃止率はおよそ1-2%程度 (倒産以外の理由も含む)")
    print("  　　　 5日保有なら1回あたりの遭遇確率はその1/50程度になる")


if __name__ == "__main__":
    main()
