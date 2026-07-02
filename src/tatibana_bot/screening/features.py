"""スクリーニングML用の特徴量 (日足ベース、翌日を予測する).

すべて「その日の引け時点で計算できる値」だけを使うこと (リーク防止)。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

FEATURE_COLUMNS = [
    "ret_1d", "ret_5d", "ret_20d",
    "range_pct", "range_pct_ma5",
    "gap_pct",
    "volume_ratio_5d", "volume_ratio_20d",
    "turnover_jpy",
    "volatility_20d",
    "rsi_14",
    "close_vs_ma20",
    "high_low_position",
]


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """日足 (open/high/low/close/volume) から特徴量を計算.

    戻り値は df と同じ index を持ち、FEATURE_COLUMNS を列に持つ。
    """
    out = pd.DataFrame(index=df.index)
    close, high, low, open_, vol = (
        df["close"], df["high"], df["low"], df["open"], df["volume"]
    )

    out["ret_1d"] = close.pct_change(1)
    out["ret_5d"] = close.pct_change(5)
    out["ret_20d"] = close.pct_change(20)

    # 日中値幅 (デイトレのしやすさに直結する)
    prev_close = close.shift(1)
    out["range_pct"] = (high - low) / prev_close * 100
    out["range_pct_ma5"] = out["range_pct"].rolling(5).mean()

    out["gap_pct"] = (open_ - prev_close) / prev_close * 100

    vol_ma5 = vol.rolling(5).mean()
    vol_ma20 = vol.rolling(20).mean()
    out["volume_ratio_5d"] = vol / vol_ma5
    out["volume_ratio_20d"] = vol / vol_ma20

    out["turnover_jpy"] = close * vol

    out["volatility_20d"] = close.pct_change().rolling(20).std() * np.sqrt(250)

    # RSI(14)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi_14"] = 100 - 100 / (1 + rs)

    ma20 = close.rolling(20).mean()
    out["close_vs_ma20"] = (close - ma20) / ma20 * 100

    # 60日高安の中でどの位置か (0=安値圏, 1=高値圏)
    hi60 = high.rolling(60).max()
    lo60 = low.rolling(60).min()
    out["high_low_position"] = (close - lo60) / (hi60 - lo60).replace(0, np.nan)

    return out
