"""異常検知: 想定外の板・値動きを検知して即撤退/停止の判断材料にする."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from tatibana_bot.models import Board
from tatibana_bot.signals.orderbook import board_features


@dataclass
class Anomaly:
    code: str
    ts: datetime
    kind: str
    detail: str


class AnomalyDetector:
    """銘柄ごとに板の履歴を持ち、異常イベントを検知する.

    検知対象:
      board_vanish : 片側の板が消えた (ストップ方向への張り付き等)
      depth_drop   : 板の厚みが直近平均から急減 (流動性蒸発)
      price_shock  : 短時間の急変動
      spread_blowout: スプレッド急拡大
    """

    def __init__(
        self,
        window_sec: int = 120,
        depth_drop_ratio: float = 0.3,
        price_shock_bps: float = 100.0,
        spread_blowout_bps: float = 50.0,
    ):
        self._window = timedelta(seconds=window_sec)
        self._depth_drop_ratio = depth_drop_ratio
        self._price_shock_bps = price_shock_bps
        self._spread_blowout_bps = spread_blowout_bps
        # code -> deque[(ts, total_depth, price)]
        self._history: dict[str, deque] = {}

    def check(self, board: Board) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        feats = board_features(board)
        ts = board.ts

        if not board.bids or not board.asks:
            side = "bid" if not board.bids else "ask"
            anomalies.append(Anomaly(board.code, ts, "board_vanish",
                                     f"{side} side of book is empty"))

        if feats["spread_bps"] != float("inf") and feats["spread_bps"] >= self._spread_blowout_bps:
            anomalies.append(Anomaly(board.code, ts, "spread_blowout",
                                     f"spread={feats['spread_bps']:.0f}bps"))

        hist = self._history.setdefault(board.code, deque())
        total_depth = feats["bid_depth"] + feats["ask_depth"]
        price = board.last_price or board.mid

        # ウィンドウ内の履歴と比較
        cutoff = ts - self._window
        while hist and hist[0][0] < cutoff:
            hist.popleft()

        if hist:
            avg_depth = sum(h[1] for h in hist) / len(hist)
            if avg_depth > 0 and total_depth < avg_depth * self._depth_drop_ratio:
                anomalies.append(Anomaly(
                    board.code, ts, "depth_drop",
                    f"depth {total_depth:.0f} < {self._depth_drop_ratio:.0%} of avg {avg_depth:.0f}",
                ))
            if price is not None:
                old_price = hist[0][2]
                if old_price:
                    move_bps = abs(price - old_price) / old_price * 10000
                    if move_bps >= self._price_shock_bps:
                        anomalies.append(Anomaly(
                            board.code, ts, "price_shock",
                            f"moved {move_bps:.0f}bps in {self._window.seconds}s",
                        ))

        hist.append((ts, total_depth, price))
        return anomalies
