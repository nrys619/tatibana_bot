from datetime import datetime

from tatibana_bot.data.store import SnapshotRecorder, TradeLog


def test_trade_log_roundtrip(tmp_path):
    log = TradeLog(tmp_path / "trades.sqlite3")
    ts = datetime(2026, 7, 1, 9, 30)

    trade_id = log.open_trade("7203", "buy", 100, ts, 1000.0, "test entry")
    log.close_trade(trade_id, ts.replace(hour=10), 1008.0, 800.0, "target")

    assert log.today_realized_pnl("2026-07-01") == 800.0
    assert log.recent_results(5) == [800.0]
    log.close()


def test_trade_log_signals(tmp_path):
    log = TradeLog(tmp_path / "trades.sqlite3")
    log.log_signal(datetime(2026, 7, 1, 9, 30), "7203", "buy", 0.8, "test", acted=True)
    cur = log._conn.execute("SELECT code, acted FROM signals")
    assert cur.fetchall() == [("7203", 1)]
    log.close()


def test_snapshot_recorder_roundtrip(tmp_path):
    recorder = SnapshotRecorder(tmp_path)
    ts = datetime.now()
    recorder.record("7203", ts, {"imbalance": 0.5, "last_price": 1000.0})
    recorder.record("7203", ts, {"imbalance": -0.2, "last_price": 999.0})

    df = recorder.load_day(f"{ts:%Y%m%d}")
    assert len(df) == 2
    assert set(["ts", "code", "imbalance", "last_price"]).issubset(df.columns)
