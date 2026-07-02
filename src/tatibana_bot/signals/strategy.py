"""板+歩み値からエントリーシグナルを出す戦略.

基本はルールベース (板の買い不均衡 + 歩み値の買い優勢) で、
学習済みの短期MLモデル (signals/model.py) があれば確信度の重み付けに使う。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tatibana_bot.models import Board, Side, Signal
from tatibana_bot.signals.orderbook import board_features


@dataclass
class StrategyParams:
    imbalance_entry: float = 0.3
    tape_ratio_entry: float = 0.6
    max_spread_bps: float = 20.0
    target_pct: float = 0.8
    stop_pct: float = 0.4


class MicroStrategy:
    def __init__(self, params: StrategyParams, ml_scorer=None):
        """ml_scorer: features dict -> 0..1 の確率を返す callable (任意)."""
        self._p = params
        self._ml = ml_scorer

    def evaluate(
        self, board: Board, tape_feats: dict[str, float], ts: datetime | None = None
    ) -> Signal | None:
        """板と歩み値の特徴量からシグナル判定。条件を満たさなければ None."""
        price = board.last_price or board.mid
        if price is None:
            return None

        ob = board_features(board)
        ts = ts or board.ts

        # スプレッドが開きすぎている銘柄はコスト負けするので見送り
        if ob["spread_bps"] > self._p.max_spread_bps:
            return None

        long_setup = (
            ob["imbalance"] >= self._p.imbalance_entry
            and tape_feats["buy_ratio"] >= self._p.tape_ratio_entry
            and ob["microprice_dev"] > 0
        )
        short_setup = (
            ob["imbalance"] <= -self._p.imbalance_entry
            and tape_feats["buy_ratio"] <= 1 - self._p.tape_ratio_entry
            and ob["microprice_dev"] < 0
        )
        if not long_setup and not short_setup:
            return None

        side = Side.BUY if long_setup else Side.SELL
        # ルールの強さから基礎確信度を作る
        strength = min(
            abs(ob["imbalance"]) / max(self._p.imbalance_entry, 1e-9),
            abs(tape_feats["buy_ratio"] - 0.5) / max(self._p.tape_ratio_entry - 0.5, 1e-9),
        )
        confidence = min(0.5 + 0.25 * strength, 0.9)

        # MLモデルがあれば確信度を混ぜる (モデルが弱気なら落とす)
        if self._ml is not None:
            ml_prob = float(self._ml({**ob, **tape_feats}))
            confidence = 0.5 * confidence + 0.5 * ml_prob
            if ml_prob < 0.5:
                return None

        sign = 1 if side == Side.BUY else -1
        return Signal(
            code=board.code,
            ts=ts,
            side=side,
            confidence=confidence,
            reason=(
                f"imbalance={ob['imbalance']:.2f} buy_ratio={tape_feats['buy_ratio']:.2f} "
                f"micro={ob['microprice_dev']:.1f}bp"
            ),
            entry_price=price,
            stop_price=price * (1 - sign * self._p.stop_pct / 100),
            target_price=price * (1 + sign * self._p.target_pct / 100),
        )
