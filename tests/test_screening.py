import numpy as np
import pandas as pd

from tatibana_bot.screening.features import FEATURE_COLUMNS, compute_features
from tatibana_bot.screening.labels import compute_labels


def make_daily(n=100, seed=0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 1000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    high = close * (1 + rng.uniform(0, 0.02, n))
    low = close * (1 - rng.uniform(0, 0.02, n))
    open_ = low + (high - low) * rng.uniform(0, 1, n)
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.bdate_range("2026-01-01", periods=n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def test_features_columns_and_index():
    df = make_daily()
    feats = compute_features(df)
    assert list(feats.columns) == FEATURE_COLUMNS
    assert feats.index.equals(df.index)
    # ウォームアップ期間後はNaNが無い
    assert not feats.iloc[60:].isna().any().any()


def test_features_use_only_past_data():
    """最終行の特徴量が未来データに依存しない (リーク防止)."""
    df = make_daily()
    feats_full = compute_features(df)
    feats_trunc = compute_features(df.iloc[:-1])
    pd.testing.assert_series_equal(
        feats_full.iloc[-2], feats_trunc.iloc[-1], check_names=False
    )


def test_labels_next_day_definition():
    df = make_daily(n=50)
    labels = compute_labels(df, range_threshold_pct=3.0, min_turnover_jpy=0)
    # 最終行は翌日が無いのでNaN
    assert np.isnan(labels.iloc[-1])
    # 手計算で1行検証
    i = 10
    next_range = (df["high"].iloc[i + 1] - df["low"].iloc[i + 1]) / df["close"].iloc[i] * 100
    assert labels.iloc[i] == float(next_range >= 3.0)


def test_labels_liquidity_filter():
    df = make_daily(n=50)
    # 売買代金の条件を満たせないほど高い閾値なら全て0
    labels = compute_labels(df, range_threshold_pct=0.0, min_turnover_jpy=1e18)
    assert (labels.dropna() == 0).all()
