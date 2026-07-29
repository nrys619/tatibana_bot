"""場中のメインループ.

①②が作った監視リストの銘柄だけを対象に、
板ポーリング -> 特徴量 -> ④リスクチェック -> ③シグナル -> 執行 を回す。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, time as dtime

from tatibana_bot.api.client import MarketDataClient
from tatibana_bot.data.store import SnapshotRecorder, TradeLog
from tatibana_bot.engine.executor import Executor
from math import isfinite

from tatibana_bot.models import Board, Position, Side, WatchItem
from tatibana_bot.risk.anomaly import AnomalyDetector
from tatibana_bot.risk.limits import RiskLimits
from tatibana_bot.risk.sizing import SizingParams, position_size
from tatibana_bot.signals.orderbook import board_features
from tatibana_bot.signals.strategy import MicroStrategy
from tatibana_bot.signals.tape import TapeReader

logger = logging.getLogger(__name__)

# 東証の取引時間 (前場/後場)
SESSIONS = [(dtime(9, 0), dtime(11, 30)), (dtime(12, 30), dtime(15, 30))]
# 引け間際は新規を建てない
NO_NEW_ENTRY_AFTER = dtime(15, 0)


def is_market_open(now: datetime) -> bool:
    t = now.time()
    return any(start <= t <= end for start, end in SESSIONS)


def can_open_new(now: datetime) -> bool:
    return is_market_open(now) and now.time() < NO_NEW_ENTRY_AFTER


class LiveEngine:
    def __init__(
        self,
        market_data: MarketDataClient,
        executor: Executor,
        strategy: MicroStrategy,
        sizing: SizingParams,
        limits: RiskLimits,
        trade_log: TradeLog,
        watchlist: list[WatchItem],
        regime_multiplier: float = 1.0,
        max_hold_sec: int = 1800,
        poll_interval_sec: float = 1.0,
        recorder: SnapshotRecorder | None = None,
        watch_updater=None,
        scan_interval_sec: float = 300.0,
        max_watch: int | None = None,
        max_total_exposure: float | None = None,
        entry_windows: list[tuple[dtime, dtime]] | None = None,
        loss_cooldown_sec: float = 0.0,
        explore_strategy=None,
        explore_max_price: float = 3000.0,
        explore_daily_loss_cap: float = 5000.0,
        price_shock_bps: float = 300.0,
        record_codes: list[str] | None = None,
        adopted: list | None = None,   # 起動時照合で引き取った建玉 (Position, trade_id)
        slippage_bps: float = 2.0,     # 成行決済の滑り (片道)
        commission_jpy: float = 0.0,   # 1取引あたりの手数料
    ):
        self._md = market_data
        self._executor = executor
        self._strategy = strategy
        self._sizing = sizing
        self._limits = limits
        self._log = trade_log
        self._watch = {w.code: w for w in watchlist}
        self._regime_mult = regime_multiplier
        self._max_hold_sec = max_hold_sec
        self._poll_interval = poll_interval_sec
        self._recorder = recorder
        # 場中の監視リスト入れ替え (松井デイトレ適性ランキング等)
        self._watch_updater = watch_updater
        self._scan_interval = scan_interval_sec
        self._max_watch = max_watch or max(len(watchlist), 1)
        self._last_scan = time.monotonic()
        self._max_exposure = max_total_exposure
        self._entry_windows = entry_windows
        self._loss_cooldown = loss_cooldown_sec
        self._code_cooldown: dict[str, datetime] = {}  # E: 負けた銘柄の出禁期限
        # 探索モード: 緩い条件の「試し玉」(最小サイズ・専用損失上限)
        self._explore = explore_strategy
        if self._explore is not None:
            self._explore._stats = self._strategy._stats  # 観測を共有
        self._explore_max_price = explore_max_price
        self._explore_loss_cap = explore_daily_loss_cap
        self._explore_pnl_today = 0.0
        self._last_px: dict[str, tuple[float, int]] = {}  # code -> (価格, 異常連続数)

        self._tapes: dict[str, TapeReader] = {c: TapeReader() for c in self._watch}
        # 記録専用銘柄 (取引はしない、ML学習用の録画だけ)
        self._record_codes = [c for c in (record_codes or []) if c not in self._watch]
        self._record_tapes: dict[str, TapeReader] = {c: TapeReader() for c in self._record_codes}
        self._anomaly = AnomalyDetector(price_shock_bps=price_shock_bps)
        self._slippage_bps = slippage_bps
        self._commission = commission_jpy
        self._positions: dict[str, tuple[Position, int]] = {}  # code -> (pos, trade_id)
        for _pos, _tid in (adopted or []):
            self._positions[_pos.code] = (_pos, _tid)
            # 引き取った建玉の銘柄が監視リストに無いと板を取りに行かず、
            # 決済されないまま放置されてしまう。必ず監視に入れる。
            if _pos.code not in self._watch:
                self._watch[_pos.code] = WatchItem(code=_pos.code, ml_score=0.0)
                self._tapes[_pos.code] = TapeReader()

    # ------------------------------------------------------------------

    def run(self) -> None:
        codes = list(self._watch.keys())
        if not codes:
            logger.warning("watchlist is empty — nothing to do")
            return
        logger.info("engine start: mode=%s watchlist=%s regime_mult=%.2f",
                    self._executor.mode, codes, self._regime_mult)
        try:
            while True:
                now = datetime.now()
                if not is_market_open(now):
                    if now.time() > SESSIONS[-1][1]:
                        logger.info("market closed — engine stopping")
                        break
                    time.sleep(5)
                    continue
                if (self._watch_updater is not None
                        and time.monotonic() - self._last_scan >= self._scan_interval):
                    self._last_scan = time.monotonic()
                    self._refresh_watchlist()
                self._tick(list(self._watch.keys()), now)
                time.sleep(self._poll_interval)
        finally:
            self._close_all("engine_shutdown")

    # ------------------------------------------------------------------

    def _refresh_watchlist(self) -> None:
        """場中スキャンの結果で監視リストを入れ替える。保有中の銘柄は外さない."""
        try:
            candidates = self._watch_updater()
        except Exception:
            logger.warning("intraday watchlist scan failed — keeping current list",
                           exc_info=True)
            return
        if not candidates:
            return

        new: dict[str, WatchItem] = {}
        for code in self._positions:  # 保有銘柄は必ず残す
            if code in self._watch:
                new[code] = self._watch[code]
        for item in candidates:
            if len(new) >= self._max_watch:
                break
            if item.code not in new:
                new[item.code] = self._watch.get(item.code, item)

        added = set(new) - set(self._watch)
        removed = set(self._watch) - set(new)
        if not added and not removed:
            return
        for code in added:
            self._tapes[code] = TapeReader()
        for code in removed:
            self._tapes.pop(code, None)
            self._strategy.drop_code(code)
        self._watch = new
        logger.info("watchlist updated: +%s -%s -> %s",
                    sorted(added) or "-", sorted(removed) or "-", sorted(new))

    def _tick(self, codes: list[str], now: datetime) -> None:
        try:
            boards = self._md.get_boards(codes + self._record_codes)
        except Exception as e:
            # デモサーバーは高頻度で接続を切るため、全文トレースはログを圧迫する
            logger.warning("board fetch failed: %s", str(e)[:120])
            return

        for code, board in boards.items():
            # 場中スキャンで控え銘柄が監視に昇格することがあるため監視判定を優先
            if code in self._watch:
                self._process_board(code, board, now)
            elif code in self._record_tapes:
                self._record_only(code, board, now)

    def _record_only(self, code: str, board: Board, now: datetime) -> None:
        """記録専用銘柄: 取引せず、ML学習用のスナップショットだけ残す."""
        if self._recorder is None:
            return
        tape = self._record_tapes[code]
        tape.infer_ticks(board)
        price = board.last_price or board.mid
        if price is not None:
            self._recorder.record(
                code, now,
                {**board_features(board), **tape.features(), "last_price": price},
            )

    def _process_board(self, code: str, board: Board, now: datetime) -> None:
        tape = self._tapes[code]
        tape.infer_ticks(board)
        tape_feats = tape.features()

        # 異常価格ガード: 1tickで3%以上飛んだ価格はデータ不良とみなして無視する。
        # ただし6tick連続で同水準なら本物の急変として受け入れる。
        px_now = board.last_price or board.mid
        if px_now is not None:
            prev, streak = self._last_px.get(code, (px_now, 0))
            if prev > 0 and abs(px_now / prev - 1) > 0.03:
                if streak < 5:
                    self._last_px[code] = (prev, streak + 1)
                    logger.warning("glitch tick ignored: %s %.1f -> %.1f", code, prev, px_now)
                    return
                self._last_px[code] = (px_now, 0)  # 6tick続いたら新水準を受け入れ
            else:
                self._last_px[code] = (px_now, 0)

        # 戦略のローリング観測を更新 (保有中も含め毎tick)
        self._strategy.observe(board, tape_feats, ts=now)

        anomalies = self._anomaly.check(board)
        for a in anomalies:
            logger.warning("ANOMALY %s %s: %s", a.code, a.kind, a.detail)

        price = board.last_price or board.mid

        # スナップショット記録 (③のML学習データ)
        if self._recorder is not None and price is not None:
            self._recorder.record(
                code, now,
                {**board_features(board), **tape_feats, "last_price": price},
            )

        # --- 決済管理 ---
        held = self._positions.get(code)
        if held is not None and price is not None:
            position, trade_id = held
            reason = Executor.should_exit(position, price, now)
            if reason is None and anomalies:
                reason = f"anomaly:{anomalies[0].kind}"
            if reason is not None:
                pnl = self._executor.close_position(position, price, reason)
                cost = self._exit_cost(position, price, board)
                self._log.close_trade(trade_id, now, price, pnl, reason, cost=cost)
                del self._positions[code]
                if position.tag == "explore":
                    self._explore_pnl_today += pnl
                if pnl < 0 and self._loss_cooldown > 0:  # E: 負けた土俵で取り返さない
                    from datetime import timedelta
                    self._code_cooldown[code] = now + timedelta(seconds=self._loss_cooldown)
            return  # 保有中は新規判定しない

        # --- 新規エントリー判定 ---
        if anomalies or not can_open_new(now):
            return
        until = self._code_cooldown.get(code)
        if until is not None and now < until:
            return  # E: この銘柄は負け直後の出禁中
        # ③参加時間帯の限定 (出来高が集中する時間だけ戦う)
        if self._entry_windows and not any(
                s <= now.time() < e for s, e in self._entry_windows):
            return
        today = now.strftime("%Y-%m-%d")
        if not self._limits.check(
            self._log.today_realized_pnl(today),
            self._log.recent_results(10, day=today),  # 連敗は当日分だけ数える
        ):
            return

        signal = self._strategy.evaluate(board, tape_feats, ts=now)
        explore = False
        if signal is None and self._explore is not None:
            price_now = board.last_price or board.mid
            if (price_now is not None and price_now <= self._explore_max_price
                    and self._explore_pnl_today > -self._explore_loss_cap):
                signal = self._explore.evaluate(board, tape_feats, ts=now)
                if signal is not None:
                    explore = True
                    signal.reason = "explore | " + signal.reason
        if signal is None:
            return

        # ②のLLM解析がその銘柄に強い悪材料を出していたらロングしない (逆も同様)
        watch = self._watch[code]
        if watch.disclosure_sentiment == "bearish" and signal.side.value == "buy":
            self._log.log_signal(now, code, signal.side.value, signal.confidence,
                                 signal.reason + " | blocked by bearish disclosure", acted=False)
            return
        if watch.disclosure_sentiment == "bullish" and signal.side.value == "sell":
            self._log.log_signal(now, code, signal.side.value, signal.confidence,
                                 signal.reason + " | blocked by bullish disclosure", acted=False)
            return

        if explore:
            from tatibana_bot.risk.sizing import UNIT_SHARES
            quantity = UNIT_SHARES  # 試し玉は最小単位固定
        else:
            quantity = position_size(signal, self._sizing, self._regime_mult)
        if quantity > 0 and self._max_exposure is not None:
            exposure = sum(p.entry_price * p.quantity for p, _ in self._positions.values())
            if exposure + quantity * signal.entry_price > self._max_exposure:
                self._log.log_signal(now, code, signal.side.value, signal.confidence,
                                     signal.reason + " | exposure cap", acted=False)
                return
        if quantity <= 0:
            self._log.log_signal(now, code, signal.side.value, signal.confidence,
                                 signal.reason + " | size=0", acted=False)
            return

        # ①指値エントリー: 買いは最良買い気配、売りは最良売り気配に置く
        limit_px = None
        if signal.side == Side.BUY and board.bids:
            limit_px = board.bids[0].price
        elif signal.side == Side.SELL and board.asks:
            limit_px = board.asks[0].price

        position = self._executor.open_position(
            signal, quantity, self._max_hold_sec, limit_price=limit_px)
        if position is not None and explore:
            position.tag = "explore"
        if position is None:  # 指値が約定しなかった (取り逃しはゼロ円)
            self._log.log_signal(now, code, signal.side.value, signal.confidence,
                                 signal.reason + " | limit not filled", acted=False)
            return
        trade_id = self._log.open_trade(code, signal.side.value, quantity,
                                        now, position.entry_price, signal.reason)
        self._log.log_signal(now, code, signal.side.value, signal.confidence,
                             signal.reason, acted=True)
        self._positions[code] = (position, trade_id)

    _MAX_COST_BPS = 50.0  # 板の片側が空だとスプレッドが無限大になる。ここで頭打ち

    def _exit_cost(self, position: Position, price: float, board: Board | None) -> float:
        """決済にかかるコスト。

        エントリーは最良気配への指値なのでスプレッドを払わない (むしろ有利側で入る)。
        決済は成行で反対側に当てるため、半スプレッド+滑り を払う。
        """
        spread_bps = 0.0
        if board is not None:
            sp = board_features(board).get("spread_bps", 0.0)
            spread_bps = self._MAX_COST_BPS if not isfinite(sp) else min(sp, self._MAX_COST_BPS)
        rate = (spread_bps / 2 + self._slippage_bps) / 10000
        return abs(price) * position.quantity * rate + self._commission

    def _close_all(self, reason: str) -> None:
        if not self._positions:
            return
        try:
            boards = self._md.get_boards(list(self._positions.keys()))
        except Exception:
            boards = {}
        now = datetime.now()
        for code, (position, trade_id) in list(self._positions.items()):
            board = boards.get(code)
            price = (board.last_price or board.mid) if board else position.entry_price
            pnl = self._executor.close_position(position, price, reason)
            cost = self._exit_cost(position, price, board)
            self._log.close_trade(trade_id, now, price, pnl, reason, cost=cost)
            del self._positions[code]
