"""ポジションサイジング (固定リスク率ベース).

1トレードの許容損失 = 資金 × risk_per_trade × レジーム倍率 × 確信度スケール
株数 = 許容損失 / 1株あたりの損切り幅
"""

from __future__ import annotations

from dataclasses import dataclass

from tatibana_bot.models import Signal

UNIT_SHARES = 100  # 単元株数


@dataclass
class SizingParams:
    equity_jpy: float
    risk_per_trade: float = 0.005
    max_position_value: float = 300_000


def position_size(
    signal: Signal,
    params: SizingParams,
    regime_multiplier: float = 1.0,
) -> int:
    """シグナルに対する発注株数 (単元に丸め)。0なら見送り."""
    stop_distance = abs(signal.entry_price - signal.stop_price)
    if stop_distance <= 0:
        return 0

    # 確信度 0.5 -> x0.5, 0.9 -> x1.0 の線形スケール
    conf_scale = min(max((signal.confidence - 0.3) / 0.6, 0.0), 1.0)

    risk_budget = params.equity_jpy * params.risk_per_trade * regime_multiplier * conf_scale
    shares = int(risk_budget / stop_distance)

    # 建玉金額の上限
    max_by_value = int(params.max_position_value / signal.entry_price)
    shares = min(shares, max_by_value)

    # 単元に切り捨て
    return (shares // UNIT_SHARES) * UNIT_SHARES
