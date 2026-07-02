"""スクリーニングモデル (LightGBM) の学習.

時系列データなのでシャッフルせず、末尾20%を検証に使う。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from tatibana_bot.screening.features import FEATURE_COLUMNS, compute_features
from tatibana_bot.screening.labels import compute_labels

logger = logging.getLogger(__name__)


def build_dataset(
    bars: dict[str, pd.DataFrame],
    range_threshold_pct: float,
    min_turnover_jpy: float,
) -> pd.DataFrame:
    """全銘柄の (特徴量, ラベル) を縦に連結した学習テーブルを作る."""
    frames = []
    for code, df in bars.items():
        feats = compute_features(df)
        feats["label"] = compute_labels(df, range_threshold_pct, min_turnover_jpy)
        feats["code"] = code
        feats["date"] = df.index
        frames.append(feats)
    table = pd.concat(frames, ignore_index=True)
    table = table.dropna(subset=FEATURE_COLUMNS + ["label"])
    return table.sort_values("date").reset_index(drop=True)


def train_model(table: pd.DataFrame, model_path: str | Path) -> dict:
    """LightGBMで学習し、モデルと検証AUCを返す."""
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    split = int(len(table) * 0.8)
    train, valid = table.iloc[:split], table.iloc[split:]

    train_set = lgb.Dataset(train[FEATURE_COLUMNS], label=train["label"])
    valid_set = lgb.Dataset(valid[FEATURE_COLUMNS], label=valid["label"])

    params = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "verbose": -1,
        "seed": 42,
    }
    model = lgb.train(
        params,
        train_set,
        num_boost_round=500,
        valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )

    auc = float(roc_auc_score(valid["label"], model.predict(valid[FEATURE_COLUMNS])))
    logger.info("screener trained: valid AUC=%.4f n_train=%d n_valid=%d",
                auc, len(train), len(valid))

    path = Path(model_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(path))
    return {"auc": auc, "n_train": len(train), "n_valid": len(valid),
            "best_iteration": model.best_iteration}
