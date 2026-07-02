"""地合い (レジーム) 判定.

日経平均等の指数日足から「今日はどういう日か」を分類し、
リスク量の倍率を決める。荒れ日はロットを絞る、が最大の目的。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from tatibana_bot.models import Regime

# レジームごとのポジションサイズ倍率
REGIME_RISK_MULTIPLIER = {
    Regime.TREND_UP: 1.0,
    Regime.TREND_DOWN: 0.8,   # 下げトレンドはショート主体になるが少し絞る
    Regime.RANGE: 0.7,
    Regime.VOLATILE: 0.3,     # 荒れ日はほぼ休む
}


@dataclass
class RegimeState:
    regime: Regime
    risk_multiplier: float
    realized_vol: float       # 年率換算ボラ
    trend_strength: float     # MA5とMA20の乖離 (%)


def classify_regime(
    index_df: pd.DataFrame,
    vol_threshold: float = 0.25,
    trend_threshold_pct: float = 1.0,
) -> RegimeState:
    """指数の日足 (close必須) からレジームを分類する.

    - 直近5日の実現ボラが年率 vol_threshold 超 -> VOLATILE
    - MA5 と MA20 の乖離が trend_threshold_pct 超 -> TREND_UP/DOWN
    - それ以外 -> RANGE
    """
    close = index_df["close"]
    if len(close) < 21:
        raise ValueError("need at least 21 days of index data")

    returns = close.pct_change().dropna()
    realized_vol = float(returns.tail(5).std() * np.sqrt(250))

    ma5 = float(close.rolling(5).mean().iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1])
    trend = (ma5 - ma20) / ma20 * 100

    if realized_vol > vol_threshold:
        regime = Regime.VOLATILE
    elif trend > trend_threshold_pct:
        regime = Regime.TREND_UP
    elif trend < -trend_threshold_pct:
        regime = Regime.TREND_DOWN
    else:
        regime = Regime.RANGE

    return RegimeState(
        regime=regime,
        risk_multiplier=REGIME_RISK_MULTIPLIER[regime],
        realized_vol=realized_vol,
        trend_strength=trend,
    )
