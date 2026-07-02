"""スクリーニングMLの教師ラベル.

「翌営業日がデイトレ向きだったか」を過去データから機械的に定義する:
  - 翌日の日中値幅 (high-low)/当日終値 が閾値以上
  - かつ翌日の売買代金が最低ラインを超える (流動性)
"""

from __future__ import annotations

import pandas as pd


def compute_labels(
    df: pd.DataFrame,
    range_threshold_pct: float = 3.0,
    min_turnover_jpy: float = 1e9,
) -> pd.Series:
    """df の各行に対して「翌日がトレード向きなら1」のラベルを返す."""
    next_high = df["high"].shift(-1)
    next_low = df["low"].shift(-1)
    next_turnover = (df["close"] * df["volume"]).shift(-1)

    next_range_pct = (next_high - next_low) / df["close"] * 100
    label = (
        (next_range_pct >= range_threshold_pct)
        & (next_turnover >= min_turnover_jpy)
    ).astype(float)
    # 翌日データが無い最終行は NaN にして学習から除外
    label[next_high.isna()] = float("nan")
    label.name = "label"
    return label
