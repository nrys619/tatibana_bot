"""学習済みスクリーニングモデルで翌日の監視候補をランキングする."""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from tatibana_bot.models import WatchItem
from tatibana_bot.screening.features import FEATURE_COLUMNS, compute_features

logger = logging.getLogger(__name__)


def rank_universe(
    bars: dict[str, pd.DataFrame],
    model_path: str | Path,
    top_n: int = 5,
    min_turnover_jpy: float = 1e9,
) -> list[WatchItem]:
    """各銘柄の最新日の特徴量でスコアリングし、上位 top_n を返す."""
    import lightgbm as lgb

    model = lgb.Booster(model_file=str(model_path))
    rows, codes = [], []
    for code, df in bars.items():
        feats = compute_features(df).iloc[-1]
        if feats[FEATURE_COLUMNS].isna().any():
            continue
        # 流動性フィルタ: 直近の売買代金が細い銘柄はデイトレ対象外
        if feats["turnover_jpy"] < min_turnover_jpy:
            continue
        rows.append(feats[FEATURE_COLUMNS])
        codes.append(code)

    if not rows:
        logger.warning("no candidates passed the liquidity filter")
        return []

    X = pd.DataFrame(rows, columns=FEATURE_COLUMNS)
    scores = model.predict(X)
    ranked = sorted(zip(codes, scores), key=lambda t: t[1], reverse=True)

    items = [WatchItem(code=code, ml_score=float(score)) for code, score in ranked[:top_n]]
    logger.info("ranked watchlist: %s",
                [(w.code, round(w.ml_score, 3)) for w in items])
    return items
