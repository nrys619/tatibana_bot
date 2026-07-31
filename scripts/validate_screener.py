"""毎晩の銘柄選定モデルが本物かを、偏りのない母集団で測り直す.

夜間バッチのモデルは valid AUC 0.95 と出ているが、その学習・検証に使った
675銘柄は「後で松井のランキングに載ったから収集された」= 動いた銘柄だけの
母集団だった。AUCが高いのは当たり前かもしれない。

ここでは JPX の上場銘柄一覧から集めた data/daily_all (約3,700銘柄) を使い、
**時間で分割して** 測り直す。

ラベルは夜間バッチと同じ「翌日の日中値幅が label_range_pct 以上か」
(= デイトレしやすい銘柄か) と、実利に近い「翌日の値動きの大きさ」の両方を見る。

usage:
  python scripts/validate_screener.py
  python scripts/validate_screener.py --daily-dir data/daily   # 従来の偏った母集団と比較
"""

from __future__ import annotations

import argparse
import glob
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from tatibana_bot.screening.features import FEATURE_COLUMNS, compute_features

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("screener")


def load(daily_dir: str, min_turnover: float, label_range_pct: float) -> pd.DataFrame:
    rows = []
    for f in sorted(glob.glob(f"{daily_dir}/*.parquet")):
        d = pd.read_parquet(f).sort_index()
        if len(d) < 80:
            continue
        feats = compute_features(d)
        feats["code"] = Path(f).stem
        feats["date"] = d.index
        # 翌日の日中値幅 (夜間バッチのラベルと同じ考え方)
        nxt_range = ((d["high"] - d["low"]) / d["close"].shift(1) * 100).shift(-1)
        feats["label"] = (nxt_range >= label_range_pct).astype(float)
        feats.loc[nxt_range.isna(), "label"] = np.nan
        # 翌日の値動きの大きさ (実際に取れる値幅)
        feats["next_range_pct"] = nxt_range
        rows.append(feats)
    p = pd.concat(rows, ignore_index=True).dropna(subset=FEATURE_COLUMNS + ["label"])
    if min_turnover > 0:
        p = p[p["turnover_jpy"] >= min_turnover]
    return p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily-dir", default="data/daily_all")
    ap.add_argument("--min-turnover", type=float, default=1e9)
    ap.add_argument("--label-range-pct", type=float, default=3.0)
    ap.add_argument("--valid-frac", type=float, default=0.3, help="末尾の何割を検証に回すか")
    ap.add_argument("--top", type=int, default=30, help="実際に選ぶ銘柄数")
    args = ap.parse_args()

    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    logger.info("読み込み中: %s (売買代金 %.0f億円以上)", args.daily_dir, args.min_turnover / 1e8)
    p = load(args.daily_dir, args.min_turnover, args.label_range_pct)
    logger.info("%d銘柄 / %d日 / %d行", p["code"].nunique(), p["date"].nunique(), len(p))

    dates = np.array(sorted(p["date"].unique()))
    split = dates[int(len(dates) * (1 - args.valid_frac))]
    tr, te = p[p["date"] < split], p[p["date"] >= split]
    logger.info("学習: 〜%s (%d行) / 検証: %s〜 (%d行、一度も学習に使わない)",
                pd.Timestamp(split).date(), len(tr), pd.Timestamp(split).date(), len(te))

    model = lgb.train(
        {"objective": "binary", "metric": "auc", "learning_rate": 0.05,
         "num_leaves": 31, "min_data_in_leaf": 100, "verbose": -1, "seed": 42},
        lgb.Dataset(tr[FEATURE_COLUMNS], label=tr["label"]),
        num_boost_round=300)
    pred = model.predict(te[FEATURE_COLUMNS])
    auc = roc_auc_score(te["label"], pred)

    base = te["label"].mean()
    print()
    print("=" * 62)
    print(f"  検証AUC: {auc:.4f}   (0.5=まぐれ / 1.0=完璧)")
    print(f"  何もしない場合の的中率: {base * 100:.1f}%")
    print("=" * 62)

    # 実運用と同じ「毎日 上位N銘柄を選ぶ」形で採点する
    te = te.copy()
    te["pred"] = pred
    hits, ranges, bases, brs = [], [], [], []
    for _, g in te.groupby("date"):
        if len(g) < args.top * 2:
            continue
        sel = g.nlargest(args.top, "pred")
        hits.append(sel["label"].mean())
        ranges.append(sel["next_range_pct"].mean())
        bases.append(g["label"].mean())
        brs.append(g["next_range_pct"].mean())
    if hits:
        h, b = np.array(hits), np.array(bases)
        r, rb = np.array(ranges), np.array(brs)
        print(f"\n毎日 上位{args.top}銘柄を選んだ場合 ({len(hits)}日で採点):")
        print(f"  的中率      : 選んだ {h.mean()*100:.1f}%  / 全銘柄 {b.mean()*100:.1f}%"
              f"  → 差 {(h.mean()-b.mean())*100:+.1f}ポイント")
        print(f"  翌日の値幅  : 選んだ {r.mean():.2f}%  / 全銘柄 {rb.mean():.2f}%"
              f"  → 差 {r.mean()-rb.mean():+.2f}%")
        segs = np.array_split(np.arange(len(h)), 3)
        d3 = [float((h[s] - b[s]).mean() * 100) for s in segs]
        print(f"  期間3分割の差: {' / '.join(f'{x:+.1f}pt' for x in d3)}"
              f"  {'★全区間プラス' if all(x > 0 for x in d3) else '← 符号が揃わない'}")

    imp = sorted(zip(FEATURE_COLUMNS, model.feature_importance("gain")),
                 key=lambda x: -x[1])[:5]
    print(f"\n効いた特徴量: {[k for k, _ in imp]}")


if __name__ == "__main__":
    main()
