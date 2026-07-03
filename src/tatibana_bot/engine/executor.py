"""注文執行. paper (発注せずログのみ) / live (立花APIで実発注) の2モード."""

from __future__ import annotations

import logging
from datetime import datetime

from tatibana_bot.api.orders import OrderGateway
from tatibana_bot.models import Position, Side, Signal

logger = logging.getLogger(__name__)


class Executor:
    def __init__(self, mode: str, gateway: OrderGateway | None = None):
        if mode not in ("paper", "live"):
            raise ValueError(f"unknown mode: {mode}")
        if mode == "live" and gateway is None:
            raise ValueError("live mode requires an OrderGateway")
        self._mode = mode
        self._gateway = gateway

    @property
    def mode(self) -> str:
        return self._mode

    def open_position(self, signal: Signal, quantity: int, max_hold_sec: int) -> Position:
        if self._mode == "live":
            # デイトレは信用新規で建てる (空売り可・現物の差金決済規制も回避)
            resp = self._gateway.new_margin_order(signal.code, signal.side, quantity)
            logger.info("LIVE order sent: %s", resp)
        else:
            logger.info("[paper] %s %s x%d @~%.1f (stop=%.1f target=%.1f)",
                        signal.side.value, signal.code, quantity,
                        signal.entry_price, signal.stop_price, signal.target_price)
        return Position(
            code=signal.code,
            side=signal.side,
            quantity=quantity,
            entry_price=signal.entry_price,  # paperでは約定価格=シグナル価格と仮定
            entry_ts=signal.ts,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            max_hold_sec=max_hold_sec,
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
        """決済条件の判定。理由文字列 or None."""
        if position.side == Side.BUY:
            if price <= position.stop_price:
                return "stop"
            if price >= position.target_price:
                return "target"
        else:
            if price >= position.stop_price:
                return "stop"
            if price <= position.target_price:
                return "target"
        if (now - position.entry_ts).total_seconds() >= position.max_hold_sec:
            return "time"
        return None
