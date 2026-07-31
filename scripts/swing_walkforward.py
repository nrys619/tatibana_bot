"""保有期間を伸ばしたら勝てるのか、日足でウォークフォワード検証する.

デイトレ側は板スナップショット20日分しか無く、MLも予測力ゼロだった。
一方で日足は675銘柄x146日あり、翌日の値動きを当てるスクリーニングモデルは
AUC 0.95 と機能している。「その当てる力を、数日保有で現金化できるか」を測る。

**カンニングをしない作り**:
  - ある時点より後のデータは一切学習に使わない (ウォークフォワード)
  - 一定期間ごとにモデルを作り直し、その先の未知期間だけで採点する
  - 往復コストを必ず引く
  - 比較対象は「全銘柄を等しく持った場合」(= 市場そのもの)。
    これに勝てなければ、銘柄を選ぶ意味がない。

usage:
  python scripts/swing_walkforward.py
  python scripts/swing_walkforward.py --holds 1 3 5 10 --top 10
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
logger = logging.getLogger("swing")

COST_PCT = 0.05      # 往復コスト (実測 約128円/25万円)


def load_panel(daily_dir: str = "data/daily", min_turnover: float = 0.0) -> pd.DataFrame:
    """全銘柄の特徴量と将来リターンを1枚の表にする (日付 x 銘柄).

    daily_dir に data/daily_all を渡すと、JPXの上場銘柄一覧から集めた
    「後から選んだのではない」母集団で検証できる。
    """
    rows = []
    for f in sorted(glob.glob(f"{daily_dir}/*.parquet")):
        code = Path(f).stem
        d = pd.read_parquet(f).sort_index()
        if len(d) < 80:
            continue
        feats = compute_features(d)
        feats["code"] = code
        feats["date"] = d.index
        feats["close"] = d["close"].to_numpy(dtype=float)
        feats["open"] = d["open"].to_numpy(dtype=float)
        rows.append(feats)
    panel = pd.concat(rows, ignore_index=True)
    panel = panel.dropna(subset=FEATURE_COLUMNS)
    if min_turnover > 0:
        # 売買代金が細すぎる銘柄は、実際には注文が通らない/滑るので除外する。
        # (全上場銘柄には1日数百万円しか動かない銘柄も多く含まれる)
        panel = panel[panel["turnover_jpy"] >= min_turnover]
    return panel


def add_forward_returns(panel: pd.DataFrame, holds: list[int]) -> pd.DataFrame:
    """H日後までのリターン (%) を足す.

    **翌日の始値で買い、その H日後の始値で売る。**
    特徴量は当日の終値までの情報で作るので、同じ終値で買う想定にすると
    「終値を知ってから終値で買う」= 実装不可能なカンニングになる。
    最初にこれをやってしまい、1日保有で年率159%という嘘の数字が出た。
    """
    panel = panel.sort_values(["code", "date"])
    g = panel.groupby("code")["open"]
    entry = g.shift(-1)                      # 翌日の始値で入る
    for h in holds:
        exit_px = g.shift(-1 - h)            # そのH日後の始値で出る
        panel[f"fwd_{h}"] = (exit_px / entry - 1) * 100
    return panel


def walk_forward(panel: pd.DataFrame, hold: int, top: int,
                 train_days: int, step_days: int) -> dict:
    """一定期間ごとに学習し直し、その先の未知期間だけで採点する."""
    import lightgbm as lgb

    label = f"fwd_{hold}"
    dates = np.array(sorted(panel["date"].unique()))
    picks, base, when, n_models = [], [], [], 0

    i = train_days
    while i + step_days <= len(dates):
        train_end = dates[i - 1]
        test_dates = dates[i:i + step_days]
        # 学習に使えるのは「H日後まで判明している」行だけ (未来を覗かない)
        cutoff = dates[max(i - 1 - hold, 0)]
        tr = panel[(panel["date"] <= cutoff)].dropna(subset=[label])
        te = panel[panel["date"].isin(test_dates)].dropna(subset=[label])
        if len(tr) < 2000 or te.empty:
            i += step_days
            continue

        model = lgb.train(
            {"objective": "regression", "metric": "l2", "learning_rate": 0.05,
             "num_leaves": 31, "min_data_in_leaf": 100, "feature_fraction": 0.8,
             "verbose": -1, "seed": 42},
            lgb.Dataset(tr[FEATURE_COLUMNS], label=tr[label]),
            num_boost_round=200,
        )
        n_models += 1
        te = te.copy()
        te["pred"] = model.predict(te[FEATURE_COLUMNS])
        # 日ごとに上位 top 銘柄を買った場合の平均リターン
        for d, g in te.groupby("date"):
            if len(g) < top * 2:
                continue
            sel = g.nlargest(top, "pred")
            picks.append(sel[label].mean() - COST_PCT)
            base.append(g[label].mean() - COST_PCT)
            when.append(d)
        i += step_days

    picks_a, base_a = np.array(picks), np.array(base)
    if picks_a.size == 0:
        return {"n": 0}
    # 保有H日ごとに乗り換える前提で年率換算 (年間の営業日を245日とする)
    trades_per_year = 245 / hold
    return {
        "n": int(picks_a.size), "models": n_models,
        "選んだ銘柄の平均": float(picks_a.mean()),
        "全銘柄の平均": float(base_a.mean()),
        "差": float(picks_a.mean() - base_a.mean()),
        "勝ちの割合": float((picks_a > 0).mean()),
        "年率換算": float(picks_a.mean() * trades_per_year),
        "全銘柄の年率": float(base_a.mean() * trades_per_year),
        # 期間を3分割して安定しているか見る (1区間の当たりに支えられていないか)
        "区間別の差": [float(np.mean(picks_a[s]) - np.mean(base_a[s]))
                       for s in np.array_split(np.arange(picks_a.size), 3)],
        "区間": [f"{pd.Timestamp(when[s[0]]).date()}〜{pd.Timestamp(when[s[-1]]).date()}"
                 for s in np.array_split(np.arange(picks_a.size), 3)],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holds", type=int, nargs="+", default=[1, 3, 5, 10, 20])
    ap.add_argument("--top", type=int, default=10, help="毎回買う銘柄数")
    ap.add_argument("--train-days", type=int, default=60, help="最初の学習に使う日数")
    ap.add_argument("--step-days", type=int, default=10, help="モデルを作り直す間隔")
    ap.add_argument("--ex-ante-universe", action="store_true",
                    help="最初から存在した銘柄だけに絞る (後から選ばれた偏りを排除)")
    ap.add_argument("--daily-dir", default="data/daily",
                    help="data/daily_all を指定するとJPX全銘柄で検証する")
    ap.add_argument("--min-turnover", type=float, default=0.0,
                    help="1日の売買代金の下限 (円)。細い銘柄は実際には売買できない")
    args = ap.parse_args()

    logger.info("日足を読み込み中... (%s)", args.daily_dir)
    panel = add_forward_returns(load_panel(args.daily_dir, args.min_turnover), args.holds)
    if args.ex_ante_universe:
        # この銘柄群は「後で松井のランキングに載ったから収集された」ため、
        # 動いた銘柄が選ばれている偏りがある。データの最初から存在する銘柄だけに
        # 絞れば、その偏りをかなり落とせる。
        first = panel["date"].min()
        keep = set(panel.loc[panel["date"] <= first + pd.Timedelta(days=5), "code"])
        before = panel["code"].nunique()
        panel = panel[panel["code"].isin(keep)]
        logger.info("最初から存在した銘柄に限定: %d -> %d銘柄", before, panel["code"].nunique())
    logger.info("%d銘柄 / %d日 / %d行", panel["code"].nunique(),
                panel["date"].nunique(), len(panel))

    print(f"\n往復コスト {COST_PCT}% を引いた後の成績 (上位{args.top}銘柄を買って H日保有)")
    print(f"{'保有':<7}{'回数':>6}{'選んだ銘柄':>12}{'全銘柄':>10}{'差':>9}"
          f"{'勝率':>8}{'年率換算':>11}{'市場の年率':>12}")
    print("-" * 78)
    for h in args.holds:
        m = walk_forward(panel, h, args.top, args.train_days, args.step_days)
        if not m.get("n"):
            print(f"{h:>3}日   データ不足")
            continue
        print(f"{h:>3}日{'':<3}{m['n']:>6}{m['選んだ銘柄の平均']:>11.3f}%"
              f"{m['全銘柄の平均']:>9.3f}%{m['差']:>+8.3f}%"
              f"{m['勝ちの割合']*100:>7.0f}%{m['年率換算']:>10.1f}%{m['全銘柄の年率']:>11.1f}%")
        segs = " / ".join(f"{x:+.3f}%" for x in m["区間別の差"])
        print(f"        期間3分割の差: {segs}"
              f"  {'★全区間プラス' if all(x > 0 for x in m['区間別の差']) else '← 符号が揃わない'}")
    print("\n※「差」がプラスでなければ、銘柄を選ぶ意味がない (市場をそのまま持てばよい)")
    print("※年率換算は乗り換えを繰り返した場合の単純合計。複利や建玉制約は考慮していない")


if __name__ == "__main__":
    main()
