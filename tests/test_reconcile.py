"""起動時照合のテスト.

2026-07-17 と 07-28 に、宙に浮いた建玉が誰にも管理されないまま残る事故が
2回起きた。ここが壊れると同じ事故が再発するので、経路ごとに固定する。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tatibana_bot.data.store import TradeLog
from tatibana_bot.engine.reconcile import reconcile_startup
from tatibana_bot.models import Side


class FakeGateway:
    def __init__(self, positions):
        self._positions = positions

    def margin_positions(self):
        return self._positions


def _margin(code: str, kubun: str, qty: int) -> dict:
    return {"sOrderIssueCode": code, "sOrderBaibaiKubun": kubun,
            "sTategyokuSuryou": str(qty)}


@pytest.fixture
def log(tmp_path):
    return TradeLog(tmp_path / "t.sqlite3")


def _reconcile(gw, log):
    return reconcile_startup(gw, log, stop_pct=0.25, target_pct=0.8,
                             max_hold_sec=1800, trailing_pct=0.3,
                             now=datetime(2026, 7, 29, 9, 0))


def test_adopts_position_that_still_exists(log):
    """実口座に建玉が残っていれば、エンジンの管理下に引き取る."""
    tid = log.open_trade("3697", "buy", 100, datetime(2026, 7, 28, 14, 58), 955.8, "explore")
    adopted = _reconcile(FakeGateway([_margin("3697", "3", 100)]), log)

    assert len(adopted) == 1
    pos, got_id = adopted[0]
    assert got_id == tid
    assert pos.code == "3697" and pos.side == Side.BUY and pos.quantity == 100
    assert pos.entry_price == 955.8
    assert pos.stop_price < pos.entry_price < pos.target_price  # 買いの損切りは下
    assert pos.tag == "adopted"
    assert len(log.open_trades()) == 1   # 引き取った分は未決済のまま (エンジンが閉じる)


def test_closes_phantom_when_no_position(log):
    """実口座に無い未決済は、帳簿を正して集計から除外できる印を残す."""
    log.open_trade("3697", "buy", 100, datetime(2026, 7, 28, 14, 58), 955.8, "explore")
    adopted = _reconcile(FakeGateway([]), log)

    assert adopted == []
    assert log.open_trades() == []          # 幽霊行は残らない
    rows = list(log._conn.execute(
        "SELECT exit_reason, pnl FROM trades WHERE code='3697'"))
    assert rows[0][0] == "reconciled(no_position)"
    assert rows[0][1] == 0.0                # 損益不明なので0。理由で除外できる


def test_sell_side_and_partial_quantity(log):
    """売建も扱える。株数が足りない建玉は引き取らない (別物の可能性がある)."""
    log.open_trade("5801", "sell", 200, datetime(2026, 7, 28, 10, 0), 2719.5, "explore")
    adopted = _reconcile(FakeGateway([_margin("5801", "1", 100)]), log)  # 100株しかない
    assert adopted == []

    log2_id = log.open_trade("5802", "sell", 100, datetime(2026, 7, 28, 10, 0), 2632.0, "x")
    adopted = _reconcile(FakeGateway([_margin("5802", "1", 100)]), log)
    assert [tid for _, tid in adopted] == [log2_id]
    pos = adopted[0][0]
    assert pos.side == Side.SELL
    assert pos.stop_price > pos.entry_price > pos.target_price  # 売りは損切りが上


def test_gateway_failure_must_not_touch_the_books(log):
    """建玉照会に失敗したら帳簿を書き換えない.

    失敗を「建玉なし」と誤認すると、実在する建玉の帳簿を消してしまい、
    その建玉は二度と管理下に戻らない。何もしないのが正しい。
    """
    class Broken:
        def margin_positions(self):
            raise RuntimeError("API down")

    log.open_trade("3697", "buy", 100, datetime(2026, 7, 28, 14, 58), 955.8, "x")
    adopted = _reconcile(Broken(), log)
    assert adopted == []
    assert len(log.open_trades()) == 1   # 未決済のまま残す (手動確認に回す)
