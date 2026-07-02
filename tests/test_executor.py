from datetime import datetime, timedelta

from tatibana_bot.engine.engine import can_open_new, is_market_open
from tatibana_bot.engine.executor import Executor
from tatibana_bot.models import Position, Side, Signal


def _signal() -> Signal:
    return Signal(
        code="7203", ts=datetime(2026, 7, 1, 9, 30), side=Side.BUY,
        confidence=0.8, reason="test",
        entry_price=1000.0, stop_price=996.0, target_price=1008.0,
    )


def test_paper_executor_roundtrip():
    executor = Executor("paper")
    pos = executor.open_position(_signal(), quantity=100, max_hold_sec=600)
    assert pos.quantity == 100
    pnl = executor.close_position(pos, price=1008.0, reason="target")
    assert pnl == (1008.0 - 1000.0) * 100


def test_should_exit_conditions():
    pos = Position(code="7203", side=Side.BUY, quantity=100,
                   entry_price=1000.0, entry_ts=datetime(2026, 7, 1, 9, 30),
                   stop_price=996.0, target_price=1008.0, max_hold_sec=600)
    now = pos.entry_ts + timedelta(seconds=10)
    assert Executor.should_exit(pos, 1000.0, now) is None
    assert Executor.should_exit(pos, 996.0, now) == "stop"
    assert Executor.should_exit(pos, 1008.0, now) == "target"
    assert Executor.should_exit(pos, 1000.0, pos.entry_ts + timedelta(seconds=601)) == "time"


def test_should_exit_short_side():
    pos = Position(code="7203", side=Side.SELL, quantity=100,
                   entry_price=1000.0, entry_ts=datetime(2026, 7, 1, 9, 30),
                   stop_price=1004.0, target_price=992.0, max_hold_sec=600)
    now = pos.entry_ts + timedelta(seconds=10)
    assert Executor.should_exit(pos, 1004.0, now) == "stop"
    assert Executor.should_exit(pos, 992.0, now) == "target"


def test_market_hours():
    assert is_market_open(datetime(2026, 7, 1, 10, 0))
    assert not is_market_open(datetime(2026, 7, 1, 12, 0))   # 昼休み
    assert is_market_open(datetime(2026, 7, 1, 15, 15))
    assert not is_market_open(datetime(2026, 7, 1, 16, 0))
    assert can_open_new(datetime(2026, 7, 1, 14, 59))
    assert not can_open_new(datetime(2026, 7, 1, 15, 10))    # 引け間際は新規なし
