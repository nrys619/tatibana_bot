from datetime import datetime, timedelta

from tatibana_bot.models import Side
from tatibana_bot.signals.orderbook import board_features
from tatibana_bot.signals.strategy import MicroStrategy, StrategyParams
from tatibana_bot.signals.tape import TapeReader


def test_board_features_imbalance(make_board):
    # 買い板が厚い -> imbalance > 0
    board = make_board(bids=((999, 800), (998, 700)), asks=((1000, 100), (1001, 100)))
    feats = board_features(board)
    assert feats["imbalance"] > 0.5
    assert feats["spread_bps"] > 0
    # マイクロプライスは薄い側 (売り) に寄る -> 上方向の圧力
    assert feats["microprice_dev"] > 0


def test_board_features_empty_side(make_board):
    board = make_board(bids=(), asks=((1000, 100),))
    feats = board_features(board)
    assert feats["imbalance_top1"] == -1.0
    assert feats["spread_bps"] == float("inf")


def test_tape_reader_classifies_aggressor(make_board):
    tape = TapeReader(window_sec=60)
    ts = datetime(2026, 7, 1, 9, 30, 0)
    b1 = make_board(last_price=999.0, ts=ts)
    tape.infer_ticks(b1)
    # 前回のbest_ask(1000)以上で約定 -> 買い方主導
    b2 = make_board(last_price=1000.0, ts=ts + timedelta(seconds=1))
    ticks = tape.infer_ticks(b2)
    assert len(ticks) == 1
    assert ticks[0].buyer_initiated is True
    assert tape.features()["buy_ratio"] == 1.0


def test_tape_reader_evicts_old_ticks(make_board):
    tape = TapeReader(window_sec=10)
    ts = datetime(2026, 7, 1, 9, 30, 0)
    tape.infer_ticks(make_board(last_price=999.0, ts=ts))
    tape.infer_ticks(make_board(last_price=1000.0, ts=ts + timedelta(seconds=1)))
    # 11秒後の約定でウィンドウ外の古いティックは消える
    tape.infer_ticks(make_board(last_price=1001.0, ts=ts + timedelta(seconds=12)))
    assert tape.features()["tick_count"] == 1.0


def _strong_long_board(make_board):
    return make_board(
        bids=((999, 900), (998, 800), (997, 700)),
        asks=((1000, 100), (1001, 100), (1002, 100)),
        last_price=1000.0,
    )


def test_strategy_long_signal(make_board):
    strategy = MicroStrategy(StrategyParams(imbalance_entry=0.3, tape_ratio_entry=0.6))
    signal = strategy.evaluate(
        _strong_long_board(make_board),
        {"buy_ratio": 0.8, "tick_count": 20.0, "price_drift": 5.0},
    )
    assert signal is not None
    assert signal.side == Side.BUY
    assert signal.stop_price < signal.entry_price < signal.target_price
    assert 0.5 <= signal.confidence <= 0.9


def test_strategy_rejects_wide_spread(make_board):
    strategy = MicroStrategy(StrategyParams(max_spread_bps=5.0))
    # スプレッド 10bp (999/1000) -> 5bp上限で見送り
    signal = strategy.evaluate(
        _strong_long_board(make_board),
        {"buy_ratio": 0.9, "tick_count": 20.0, "price_drift": 5.0},
    )
    assert signal is None


def test_strategy_ml_veto(make_board):
    strategy = MicroStrategy(StrategyParams(), ml_scorer=lambda feats: 0.2)
    signal = strategy.evaluate(
        _strong_long_board(make_board),
        {"buy_ratio": 0.9, "tick_count": 20.0, "price_drift": 5.0},
    )
    assert signal is None  # MLが弱気ならエントリーしない
