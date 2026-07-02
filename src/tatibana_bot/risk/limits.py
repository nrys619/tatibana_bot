"""日次リスクリミット (キルスイッチ).

- 日次損失が上限に達したら当日の新規エントリー停止
- 連敗数が上限に達したら停止 (メンタル/ロジック崩壊の検知)
一度発動したら当日中は解除しない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class LimitParams:
    equity_jpy: float
    daily_loss_limit: float = 0.02
    max_consecutive_losses: int = 4


class RiskLimits:
    def __init__(self, params: LimitParams):
        self._p = params
        self._halted = False
        self._halt_reason = ""

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def _halt(self, reason: str) -> None:
        if not self._halted:
            self._halted = True
            self._halt_reason = reason
            logger.warning("KILL SWITCH: trading halted (%s)", reason)

    def check(self, realized_pnl_today: float, recent_results: list[float]) -> bool:
        """新規エントリー可能なら True.

        recent_results は新しい順の直近トレード損益。
        """
        if self._halted:
            return False

        loss_limit = -self._p.equity_jpy * self._p.daily_loss_limit
        if realized_pnl_today <= loss_limit:
            self._halt(
                f"daily loss {realized_pnl_today:.0f} <= limit {loss_limit:.0f}"
            )
            return False

        streak = 0
        for pnl in recent_results:
            if pnl < 0:
                streak += 1
            else:
                break
        if streak >= self._p.max_consecutive_losses:
            self._halt(f"{streak} consecutive losses")
            return False

        return True
