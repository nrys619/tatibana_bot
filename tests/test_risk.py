from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from tatibana_bot.models import Regime, Side, Signal
from tatibana_bot.risk.anomaly import AnomalyDetector
from tatibana_bot.risk.limits import LimitParams, RiskLimits
from tatibana_bot.risk.regime import classify_regime
from tatibana_bot.risk.sizing import SizingParams, position_size


def make_index(daily_returns) -> pd.DataFrame:
    close = 30000 * np.exp(np.cumsum(daily_returns))
    return pd.DataFrame({"close": close},
                        index=pd.bdate_range("2026-01-01", periods=len(close)))


def test_regime_trend_up():
    state = classify_regime(make_index([0.005] * 40))
    assert state.regime == Regime.TREND_UP
    assert state.risk_multiplier == 1.0


def test_regime_volatile():
    rng = np.random.default_rng(1)
    state = classify_regime(make_index(rng.normal(0, 0.03, 40)))
    assert state.regime == Regime.VOLATILE
    assert state.risk_multiplier < 0.5


def test_regime_range():
    state = classify_regime(make_index([0.0002, -0.0002] * 20))
    assert state.regime == Regime.RANGE


def _signal(entry=1000.0, stop=996.0, confidence=0.9) -> Signal:
    return Signal(
        code="7203", ts=datetime(2026, 7, 1, 9, 30), side=Side.BUY,
        confidence=confidence, reason="test",
        entry_price=entry, stop_price=stop, target_price=1008.0,
    )


def test_position_size_basic():
    params = SizingParams(equity_jpy=1_000_000, risk_per_trade=0.005,
                          max_position_value=10_000_000)
    # リスク予算 5000円 / 損切り幅 4円 = 1250株 -> 単元丸めで1200株
    assert position_size(_signal(), params) == 1200


def test_position_size_capped_by_value():
    params = SizingParams(equity_jpy=10_000_000, risk_per_trade=0.01,
                          max_position_value=300_000)
    # 建玉上限 300,000/1000 = 300株
    assert position_size(_signal(), params) == 300


def test_position_size_zero_when_no_stop_distance():
    params = SizingParams(equity_jpy=1_000_000)
    assert position_size(_signal(stop=1000.0), params) == 0


def test_position_size_scales_with_regime():
    params = SizingParams(equity_jpy=1_000_000, risk_per_trade=0.005,
                          max_position_value=10_000_000)
    full = position_size(_signal(), params, regime_multiplier=1.0)
    reduced = position_size(_signal(), params, regime_multiplier=0.3)
    assert reduced < full


def test_limits_daily_loss_halts_and_sticks():
    limits = RiskLimits(LimitParams(equity_jpy=1_000_000, daily_loss_limit=0.02))
    assert limits.check(realized_pnl_today=-10_000, recent_results=[]) is True
    assert limits.check(realized_pnl_today=-20_000, recent_results=[]) is False
    assert limits.halted
    # 損益が戻っても当日中は解除されない
    assert limits.check(realized_pnl_today=0, recent_results=[]) is False


def test_limits_consecutive_losses():
    limits = RiskLimits(LimitParams(equity_jpy=1_000_000, max_consecutive_losses=3))
    assert limits.check(0, recent_results=[-1.0, -1.0, 5.0, -1.0]) is True
    assert limits.check(0, recent_results=[-1.0, -1.0, -1.0, 5.0]) is False


def test_anomaly_board_vanish(make_board):
    detector = AnomalyDetector()
    anomalies = detector.check(make_board(bids=(), asks=((1000, 100),)))
    assert any(a.kind == "board_vanish" for a in anomalies)


def test_anomaly_price_shock(make_board):
    detector = AnomalyDetector(price_shock_bps=50)
    ts = datetime(2026, 7, 1, 9, 30)
    detector.check(make_board(last_price=1000.0, ts=ts))
    anomalies = detector.check(
        make_board(last_price=1010.0, ts=ts + timedelta(seconds=30),
                   bids=((1009, 100),), asks=((1010, 100),))
    )
    assert any(a.kind == "price_shock" for a in anomalies)


def test_anomaly_depth_drop(make_board):
    detector = AnomalyDetector(depth_drop_ratio=0.3)
    ts = datetime(2026, 7, 1, 9, 30)
    for i in range(5):
        detector.check(make_board(
            bids=((999, 1000),), asks=((1000, 1000),),
            ts=ts + timedelta(seconds=i),
        ))
    anomalies = detector.check(make_board(
        bids=((999, 100),), asks=((1000, 100),),
        ts=ts + timedelta(seconds=5),
    ))
    assert any(a.kind == "depth_drop" for a in anomalies)
