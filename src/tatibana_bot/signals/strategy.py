"""板+歩み値からエントリーシグナルを出す戦略.

基本はルールベース (板の買い不均衡 + 歩み値の買い優勢) で、
学習済みの短期MLモデル (signals/model.py) があれば確信度の重み付けに使う。

有名デイトレーダーの手法から機械化したフィルタ (config でON/OFF):
  trend_filter : cis流の順張り。5分前より価格が上の時だけ買う (下なら売りのみ)
  volume_surge : テスタ流。約定の勢いが平常時の surge_ratio 倍の時だけ入る
  absorption   : テスタ流の板読み。売り板が食われて減っている時だけ買う (逆も同様)
  breakout_only: cis流。本日高値(安値)圏のブレイクでのみ入る

いずれも銘柄ごとの直近 window_sec 秒の観測 (observe) を材料にする。
観測が min_history 件たまるまでは判定しない = 監視に入った直後は様子見。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
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
    trend_filter: bool = False
    volume_surge: bool = False
    absorption: bool = False
    breakout_only: bool = False
    surge_ratio: float = 2.0
    window_sec: float = 300.0
    min_history: int = 60


@dataclass
class _CodeStats:
    """銘柄ごとの場中ローリング観測 (順張り・吸収・出来高急増の材料)."""

    hist: deque = field(default_factory=deque)  # (epoch, price, ask_depth, bid_depth)
    session_high: float = 0.0
    session_low: float = 1e18
    tick_sum: float = 0.0
    tick_n: int = 0


class MicroStrategy:
    def __init__(self, params: StrategyParams, ml_scorer=None):
        """ml_scorer: features dict -> 0..1 の確率を返す callable (任意)."""
        self._p = params
        self._ml = ml_scorer
        self._stats: dict[str, _CodeStats] = {}

    def observe(self, board: Board, tape_feats: dict[str, float],
                ts: datetime | None = None) -> None:
        """毎tick呼ばれ、銘柄ごとの観測を更新する (保有中も含む)."""
        price = board.last_price or board.mid
        if price is None:
            return
        ob = board_features(board)
        t = (ts or board.ts or datetime.now()).timestamp()
        st = self._stats.setdefault(board.code, _CodeStats())
        st.session_high = max(st.session_high, price)
        st.session_low = min(st.session_low, price)
        st.tick_sum += tape_feats.get("tick_count", 0.0)
        st.tick_n += 1
        st.hist.append((t, price, ob.get("ask_depth", 0.0), ob.get("bid_depth", 0.0)))
        while st.hist and t - st.hist[0][0] > self._p.window_sec:
            st.hist.popleft()

    def drop_code(self, code: str) -> None:
        """監視から外れた銘柄の観測を破棄 (メモリ掃除)."""
        self._stats.pop(code, None)

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

        # --- 有名トレーダー由来のフィルタ群 ---
        if any((self._p.trend_filter, self._p.volume_surge,
                self._p.absorption, self._p.breakout_only)):
            st = self._stats.get(board.code)
            if st is None or len(st.hist) < self._p.min_history:
                return None  # 観測不足 (監視入り直後) は様子見
            _, px_old, ask_old, bid_old = st.hist[0]

            if self._p.trend_filter:  # cis: 上がっているものだけ買う
                if side == Side.BUY and not price > px_old:
                    return None
                if side == Side.SELL and not price < px_old:
                    return None
            if self._p.volume_surge:  # テスタ: 出来高(約定数)の急増時のみ
                avg_ticks = st.tick_sum / max(st.tick_n, 1)
                if tape_feats.get("tick_count", 0.0) < self._p.surge_ratio * max(avg_ticks, 0.5):
                    return None
            if self._p.absorption:  # テスタ: 反対側の板が食われている時のみ
                if side == Side.BUY and not (ask_old > 0 and ob["ask_depth"] < 0.7 * ask_old):
                    return None
                if side == Side.SELL and not (bid_old > 0 and ob["bid_depth"] < 0.7 * bid_old):
                    return None
            if self._p.breakout_only:  # cis: 本日高値/安値圏のみ
                if side == Side.BUY and price < st.session_high * 0.999:
                    return None
                if side == Side.SELL and price > st.session_low * 1.001:
                    return None

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
