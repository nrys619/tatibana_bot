"""注文執行. paper (発注せずログのみ) / live (立花APIで実発注) の2モード.

maker_entry=True のとき、liveの新規は指値で入る:
  1. 買いなら最良買い気配、売りなら最良売り気配に指値を置く
  2. fill_timeout_sec 待って約定しなければ取消して見送り (取り逃しはゼロ円)
これで成行のスプレッド+滑りコスト (往復約10bp) をほぼゼロにする。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from tatibana_bot.api.orders import OrderGateway
from tatibana_bot.models import Position, Side, Signal

logger = logging.getLogger(__name__)

FILLED_STATUS = "10"  # sOrderStatusCode: 全部約定


class Executor:
    def __init__(
        self,
        mode: str,
        gateway: OrderGateway | None = None,
        maker_entry: bool = False,
        fill_timeout_sec: float = 8.0,
        trailing_pct: float = 0.0,
    ):
        if mode not in ("paper", "live"):
            raise ValueError(f"unknown mode: {mode}")
        if mode == "live" and gateway is None:
            raise ValueError("live mode requires an OrderGateway")
        self._mode = mode
        self._gateway = gateway
        self._maker = maker_entry
        self._fill_timeout = fill_timeout_sec
        self._trailing_pct = trailing_pct

    @property
    def mode(self) -> str:
        return self._mode

    def _wait_fill(self, order_number: str) -> bool:
        """注文が全部約定するまで fill_timeout_sec 待つ."""
        deadline = time.monotonic() + self._fill_timeout
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                o = self._gateway.order_status(order_number)
            except Exception:
                logger.warning("order status check failed", exc_info=True)
                continue
            if o and str(o.get("sOrderStatusCode")) == FILLED_STATUS:
                return True
        return False

    def open_position(
        self,
        signal: Signal,
        quantity: int,
        max_hold_sec: int,
        limit_price: float | None = None,
    ) -> Position | None:
        """建玉を作る。maker指値が約定しなかった場合は None (見送り)."""
        entry_price = signal.entry_price
        if self._mode == "live":
            use_limit = self._maker and limit_price is not None
            resp = self._gateway.new_margin_order(
                signal.code, signal.side, quantity,
                price=limit_price if use_limit else None,
            )
            logger.info("LIVE order sent (%s): %s",
                        "limit" if use_limit else "market", resp)
            if use_limit:
                num = str(resp.get("sOrderNumber", ""))
                day = str(resp.get("sEigyouDay", ""))
                if not self._wait_fill(num):
                    try:
                        self._gateway.cancel_order(num, day)
                        logger.info("limit order not filled — cancelled: %s %s",
                                    signal.code, num)
                    except Exception:
                        logger.warning("cancel failed (may have filled)", exc_info=True)
                    # 取消直前に約定していた場合だけ建玉として扱う
                    o = None
                    try:
                        o = self._gateway.order_status(num)
                    except Exception:
                        pass
                    if not (o and str(o.get("sOrderStatusCode")) == FILLED_STATUS):
                        return None
                entry_price = limit_price
        else:
            if self._maker and limit_price is not None:
                entry_price = limit_price  # paperでも指値価格で約定したと仮定
            logger.info("[paper] %s %s x%d @~%.1f (stop=%.1f target=%.1f)",
                        signal.side.value, signal.code, quantity,
                        entry_price, signal.stop_price, signal.target_price)

        sign = 1 if signal.side == Side.BUY else -1
        stop = entry_price * (signal.stop_price / signal.entry_price)
        target = entry_price * (signal.target_price / signal.entry_price)
        return Position(
            code=signal.code,
            side=signal.side,
            quantity=quantity,
            entry_price=entry_price,
            entry_ts=signal.ts,
            stop_price=stop,
            target_price=target,
            max_hold_sec=max_hold_sec,
            trailing_pct=self._trailing_pct,
            peak=entry_price,
        )

    def close_position(self, position: Position, price: float, reason: str) -> float:
        exit_side = Side.SELL if position.side == Side.BUY else Side.BUY
        if self._mode == "live":
            resp = self._gateway.repay_margin_order(position.code, exit_side, position.quantity)
            logger.info("LIVE close sent: %s", resp)
        pnl = position.pnl(price)
        logger.info("[%s] close %s x%d @~%.1f pnl=%.0f (%s)",
                    self._mode, position.code, position.quantity, price, pnl, reason)
        return pnl

    @staticmethod
    def should_exit(position: Position, price: float, now: datetime) -> str | None:
        """決済条件の判定。理由文字列 or None。トレーリング有効時はpeakを更新する."""
        if position.side == Side.BUY:
            if price <= position.stop_price:
                return "stop"
            if position.trailing_pct > 0:
                position.peak = max(position.peak, price)
                if (position.peak > position.entry_price
                        and price <= position.peak * (1 - position.trailing_pct / 100)):
                    return "trail"
            elif price >= position.target_price:
                return "target"
        else:
            if price >= position.stop_price:
                return "stop"
            if position.trailing_pct > 0:
                position.peak = min(position.peak, price)
                if (position.peak < position.entry_price
                        and price >= position.peak * (1 + position.trailing_pct / 100)):
                    return "trail"
            elif price <= position.target_price:
                return "target"
        if (now - position.entry_ts).total_seconds() >= position.max_hold_sec:
            return "time"
        return None
