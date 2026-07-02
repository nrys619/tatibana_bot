"""板・歩み値スナップショットからの短期MLモデル.

engine が record_snapshots=true で貯めた特徴量スナップショット
(data/snapshots/YYYYMMDD.jsonl) を教師データにして、
「Nスナップショット先に価格が上がっているか」を予測する。

運用フロー:
  1. まずルールベースのみで paper 運用しつつスナップショットを収集
  2. 数週間分たまったら train_intraday_model() で学習
  3. models/intraday.txt が存在すれば engine が自動でML重み付けを有効化
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

SNAPSHOT_FEATURES = [
    "imbalance", "imbalance_top1", "spread_bps", "microprice_dev",
    "bid_depth", "ask_depth", "buy_ratio", "tick_count", "price_drift",
]


def build_intraday_dataset(
    snapshots: pd.DataFrame, horizon: int = 30, min_move_bps: float = 5.0
) -> pd.DataFrame:
    """スナップショットDFに「horizon個先で価格が+min_move_bps以上か」のラベルを付ける."""
    frames = []
    for code, g in snapshots.groupby("code"):
        g = g.sort_values("ts").reset_index(drop=True)
        if "last_price" not in g.columns:
            continue
        future = g["last_price"].shift(-horizon)
        move_bps = (future - g["last_price"]) / g["last_price"] * 10000
        g["label"] = (move_bps >= min_move_bps).astype(float)
        g.loc[future.isna(), "label"] = float("nan")
        frames.append(g)
    if not frames:
        return pd.DataFrame()
    table = pd.concat(frames, ignore_index=True)
    cols = [c for c in SNAPSHOT_FEATURES if c in table.columns]
    return table.dropna(subset=cols + ["label"])


def train_intraday_model(table: pd.DataFrame, model_path: str | Path) -> dict:
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    cols = [c for c in SNAPSHOT_FEATURES if c in table.columns]
    split = int(len(table) * 0.8)
    train, valid = table.iloc[:split], table.iloc[split:]

    model = lgb.train(
        {
            "objective": "binary",
            "metric": "auc",
            "learning_rate": 0.05,
            "num_leaves": 15,   # 特徴量が少ないので浅く
            "verbose": -1,
            "seed": 42,
        },
        lgb.Dataset(train[cols], label=train["label"]),
        num_boost_round=300,
        valid_sets=[lgb.Dataset(valid[cols], label=valid["label"])],
        callbacks=[lgb.early_stopping(30, verbose=False)],
    )
    auc = float(roc_auc_score(valid["label"], model.predict(valid[cols])))
    Path(model_path).parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(model_path))
    logger.info("intraday model trained: AUC=%.4f n=%d", auc, len(table))
    return {"auc": auc, "n": len(table)}


def load_scorer(model_path: str | Path):
    """学習済みモデルを features dict -> prob の callable として返す。無ければ None."""
    path = Path(model_path)
    if not path.exists():
        return None
    import lightgbm as lgb

    model = lgb.Booster(model_file=str(path))
    feature_names = model.feature_name()

    def scorer(features: dict[str, float]) -> float:
        row = pd.DataFrame([[features.get(name, 0.0) for name in feature_names]],
                           columns=feature_names)
        return float(model.predict(row)[0])

    return scorer
