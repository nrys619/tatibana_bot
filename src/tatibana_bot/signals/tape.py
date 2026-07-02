"""歩み値 (約定履歴) から特徴量を計算する.

板情報から直接歩み値が取れない場合は、連続する板スナップショットの
現在値変化からティックを推定する (infer_ticks)。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

from tatibana_bot.models import Board, Tick


class TapeReader:
    """直近ウィンドウの約定から買い圧力を推定する."""

    def __init__(self, window_sec: int = 60):
        self._window = timedelta(seconds=window_sec)
        self._ticks: deque[Tick] = deque()
        self._last_board: Board | None = None

    def infer_ticks(self, board: Board) -> list[Tick]:
        """板スナップショットの差分からティックを推定して取り込む.

        現在値が直前の最良売気配以上で約定していれば買い方主導、
        最良買気配以下なら売り方主導とみなす (Lee-Ready法の簡易版)。
        """
        new_ticks: list[Tick] = []
        prev = self._last_board
        self._last_board = board
        if prev is None or board.last_price is None:
            return new_ticks
        if prev.last_price == board.last_price:
            return new_ticks

        buyer_initiated: bool | None = None
        if prev.best_ask is not None and board.last_price >= prev.best_ask:
            buyer_initiated = True
        elif prev.best_bid is not None and board.last_price <= prev.best_bid:
            buyer_initiated = False

        tick = Tick(
            code=board.code,
            ts=board.ts,
            price=board.last_price,
            quantity=1.0,  # スナップショット差分では数量不明のため件数ベース
            buyer_initiated=buyer_initiated,
        )
        self.add_tick(tick)
        new_ticks.append(tick)
        return new_ticks

    def add_tick(self, tick: Tick) -> None:
        self._ticks.append(tick)
        self._evict(tick.ts)

    def _evict(self, now: datetime) -> None:
        cutoff = now - self._window
        while self._ticks and self._ticks[0].ts < cutoff:
            self._ticks.popleft()

    def features(self) -> dict[str, float]:
        """直近ウィンドウの歩み値特徴量.

          buy_ratio    : 買い方主導の約定比率 (0-1, 0.5が中立)
          tick_count   : ウィンドウ内の約定数 (活況度)
          price_drift  : ウィンドウ内の価格変化率 (bp)
        """
        ticks = [t for t in self._ticks if t.buyer_initiated is not None]
        n_all = len(self._ticks)
        if not ticks:
            return {"buy_ratio": 0.5, "tick_count": float(n_all), "price_drift": 0.0}

        buys = sum(1 for t in ticks if t.buyer_initiated)
        first, last = self._ticks[0].price, self._ticks[-1].price
        drift = (last - first) / first * 10000 if first > 0 else 0.0
        return {
            "buy_ratio": buys / len(ticks),
            "tick_count": float(n_all),
            "price_drift": drift,
        }
