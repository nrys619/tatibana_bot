from datetime import datetime

import pytest

from tatibana_bot.models import Board, BoardLevel


@pytest.fixture
def make_board():
    def _make(
        bids=((999.0, 500), (998.0, 300)),
        asks=((1000.0, 200), (1001.0, 300)),
        last_price=1000.0,
        code="7203",
        ts=None,
    ) -> Board:
        return Board(
            code=code,
            ts=ts or datetime(2026, 7, 1, 9, 30, 0),
            bids=[BoardLevel(p, q) for p, q in bids],
            asks=[BoardLevel(p, q) for p, q in asks],
            last_price=last_price,
        )

    return _make
