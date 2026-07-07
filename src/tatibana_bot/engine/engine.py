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
from tatibana_bot.models import Board, Position, WatchItem
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
        self._max_watch = max(len(watchlist), 1)
        self._last_scan = time.monotonic()

        self._tapes: dict[str, TapeReader] = {c: TapeReader() for c in self._watch}
        self._anomaly = AnomalyDetector()
        self._positions: dict[str, tuple[Position, int]] = {}  # code -> (pos, trade_id)

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
        self._watch = new
        logger.info("watchlist updated: +%s -%s -> %s",
                    sorted(added) or "-", sorted(removed) or "-", sorted(new))

    def _tick(self, codes: list[str], now: datetime) -> None:
        try:
            boards = self._md.get_boards(codes)
        except Exception:
            logger.warning("board fetch failed", exc_info=True)
            return

        for code, board in boards.items():
            self._process_board(code, board, now)

    def _process_board(self, code: str, board: Board, now: datetime) -> None:
        tape = self._tapes[code]
        tape.infer_ticks(board)
        tape_feats = tape.features()

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
                self._log.close_trade(trade_id, now, price, pnl, reason)
                del self._positions[code]
            return  # 保有中は新規判定しない

        # --- 新規エントリー判定 ---
        if anomalies or not can_open_new(now):
            return
        today = now.strftime("%Y-%m-%d")
        if not self._limits.check(
            self._log.today_realized_pnl(today), self._log.recent_results(10)
        ):
            return

        signal = self._strategy.evaluate(board, tape_feats, ts=now)
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

        quantity = position_size(signal, self._sizing, self._regime_mult)
        if quantity <= 0:
            self._log.log_signal(now, code, signal.side.value, signal.confidence,
                                 signal.reason + " | size=0", acted=False)
            return

        position = self._executor.open_position(signal, quantity, self._max_hold_sec)
        trade_id = self._log.open_trade(code, signal.side.value, quantity,
                                        now, signal.entry_price, signal.reason)
        self._log.log_signal(now, code, signal.side.value, signal.confidence,
                             signal.reason, acted=True)
        self._positions[code] = (position, trade_id)

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
            self._log.close_trade(trade_id, now, price, pnl, reason)
            del self._positions[code]
