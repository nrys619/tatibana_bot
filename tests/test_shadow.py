"""見送った買いを「もし入っていたら」として記録する仕組みのテスト.

「下げ相場では買わない」という判断が正しかったのかを測る唯一の手段なので、
記録が漏れたり実注文が飛んだりしないことを固定する。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tatibana_bot.data.store import TradeLog
from tatibana_bot.engine.engine import LiveEngine
from tatibana_bot.engine.executor import Executor
from tatibana_bot.models import Side, WatchItem
from tatibana_bot.risk.limits import LimitParams, RiskLimits
from tatibana_bot.risk.sizing import SizingParams
from tatibana_bot.signals.strategy import MicroStrategy, StrategyParams


class FakeMarketData:
    def __init__(self, boards):
        self.boards = boards

    def get_boards(self, codes):
        return {c: b for c, b in self.boards.items() if c in codes}


def _strong_long(make_board, px: float, ts):
    return make_board(bids=((px - 1, 900), (px - 2, 800), (px - 3, 700)),
                      asks=((px, 100), (px + 1, 100), (px + 2, 100)),
                      last_price=px, ts=ts, code="7203")


def _engine(tmp_path, allow_buy: bool):
    log = TradeLog(tmp_path / "t.sqlite3")
    return LiveEngine(
        market_data=FakeMarketData({}),
        executor=Executor("paper", None, trailing_pct=0.0),
        strategy=MicroStrategy(StrategyParams(imbalance_entry=0.3, tape_ratio_entry=0.6,
                                              min_history=1)),
        sizing=SizingParams(equity_jpy=2_000_000, max_position_value=1_500_000),
        limits=RiskLimits(LimitParams(equity_jpy=2_000_000, daily_loss_limit=0.5,
                                      max_consecutive_losses=999)),
        trade_log=log,
        watchlist=[WatchItem(code="7203", ml_score=0.9)],
        max_hold_sec=1800,
        allow_buy=allow_buy,
    ), log


# エンジンは板の変化から歩み値を自分で作る。買い方主導と判定させるには
# 「前回の最良売り気配以上で約定」= 価格が上がっていく必要がある。
RAMP = [1000.0 + i for i in range(8)]


def _feed(engine, make_board, prices, ts0):
    for i, px in enumerate(prices):
        b = _strong_long(make_board, px, ts0 + timedelta(seconds=i))
        engine._process_board("7203", b, ts0 + timedelta(seconds=i))


def test_blocked_buy_is_recorded_and_no_real_trade(tmp_path, make_board):
    """買いを止めた日は、実取引ゼロ・影の取引に記録が残る."""
    engine, log = _engine(tmp_path, allow_buy=False)
    ts0 = datetime(2026, 7, 29, 9, 30, 0)
    _feed(engine, make_board, RAMP, ts0)

    assert log.open_trades() == []                        # 実物は建っていない
    shadow = list(log._conn.execute("SELECT code, side, blocked_by FROM shadow_trades"))
    assert shadow == [("7203", "buy", "falling_market")]
    sig = list(log._conn.execute("SELECT reason, acted FROM signals"))
    assert any("buy blocked" in r and a == 0 for r, a in sig)


def test_shadow_closes_with_same_rules(tmp_path, make_board):
    """影の取引は実物と同じ決済ルールで閉じ、コストも引かれる."""
    engine, log = _engine(tmp_path, allow_buy=False)
    ts0 = datetime(2026, 7, 29, 9, 30, 0)
    _feed(engine, make_board, RAMP, ts0)
    # 損切り(0.4%)より下へ落とす -> 影の建玉が stop で閉じる
    engine._process_board("7203", _strong_long(make_board, 980.0, ts0 + timedelta(seconds=30)),
                          ts0 + timedelta(seconds=30))

    row = list(log._conn.execute(
        "SELECT exit_reason, pnl, cost, pnl_net FROM shadow_trades"))[0]
    reason, pnl, cost, net = row
    assert reason == "stop"
    assert pnl < 0                       # 買った直後に下がったので負け
    assert cost > 0                      # コストが引かれている
    assert net == pytest.approx(pnl - cost)


def test_buy_allowed_makes_real_trade_not_shadow(tmp_path, make_board):
    """買いを許可している日は、実取引が建ち影の記録はできない."""
    engine, log = _engine(tmp_path, allow_buy=True)
    ts0 = datetime(2026, 7, 29, 9, 30, 0)
    _feed(engine, make_board, RAMP, ts0)

    assert len(log.open_trades()) == 1
    assert log.open_trades()[0]["side"] == "buy"
    assert list(log._conn.execute("SELECT * FROM shadow_trades")) == []


def test_shadow_not_duplicated_for_same_code(tmp_path, make_board):
    """同じ銘柄で合図が連続しても影の建玉は1つだけ."""
    engine, log = _engine(tmp_path, allow_buy=False)
    ts0 = datetime(2026, 7, 29, 9, 30, 0)
    _feed(engine, make_board, RAMP, ts0)
    n = list(log._conn.execute("SELECT COUNT(*) FROM shadow_trades"))[0][0]
    assert n == 1
