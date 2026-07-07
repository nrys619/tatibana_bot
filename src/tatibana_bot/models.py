"""ボット全体で共有するデータモデル."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Regime(str, Enum):
    """その日の地合い分類."""

    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    VOLATILE = "volatile"  # 荒れ日: リスクを絞る/休む


@dataclass
class BoardLevel:
    """板の1段 (価格と数量)."""

    price: float
    quantity: float


@dataclass
class Board:
    """ある時点の板スナップショット."""

    code: str
    ts: datetime
    bids: list[BoardLevel]  # 買い板 (高い順)
    asks: list[BoardLevel]  # 売り板 (安い順)
    last_price: float | None = None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0


@dataclass
class Tick:
    """歩み値の1約定."""

    code: str
    ts: datetime
    price: float
    quantity: float
    # True=買い方主導(アップティック/アスク約定), False=売り方主導, None=不明
    buyer_initiated: bool | None = None


@dataclass
class Signal:
    code: str
    ts: datetime
    side: Side
    confidence: float  # 0.0-1.0
    reason: str
    entry_price: float
    stop_price: float
    target_price: float


@dataclass
class Position:
    code: str
    side: Side
    quantity: int
    entry_price: float
    entry_ts: datetime
    stop_price: float
    target_price: float
    max_hold_sec: int
    trailing_pct: float = 0.0   # >0ならピークからこの%押しで決済 (targetの代わり)
    peak: float = 0.0           # トレーリング用の最良価格 (should_exitが更新)

    def pnl(self, current_price: float) -> float:
        sign = 1 if self.side == Side.BUY else -1
        return sign * (current_price - self.entry_price) * self.quantity


@dataclass
class WatchItem:
    """当日の監視銘柄 (①のML + ②のLLM解析の出力)."""

    code: str
    name: str = ""
    ml_score: float = 0.0
    disclosure_sentiment: str | None = None  # bullish / bearish / neutral
    disclosure_impact: int | None = None  # 1-5
    disclosure_summary: str | None = None
    notes: list[str] = field(default_factory=list)
