"""スイング戦略 A3: 下げ基調で売られすぎた大型株を買い、5日保有する.

バックテストの設定をそのまま写す。**検証で使った条件と1つでも違うと
検証結果は意味を失う**ので、数値はここに集約して他所で書き換えない。

検証結果 (2026-08-01 / JPX全3,703銘柄 / 40ヶ月 / 往復コスト0.05%込み):
  平均 +1.325% / 勝率59% / 累積+33.3% / 最大下落-13.4% / 期間3分割で全区間プラス
  大型株(TOPIX Large70)のみでも +1.139% / 累積+28.9% で成立
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class SwingParams:
    """バックテストで検証した条件そのもの。安易に変えないこと."""

    z_entry: float = -2.0          # 20日平均から何σ売られたら買うか
    hold_days: int = 5             # 何営業日持つか
    min_turnover: float = 1e9      # 売買代金の下限 (実際に注文が通る水準)
    market_ma: int = 25            # 地合い判定に使う指数の移動平均
    max_positions: int = 10        # 同時に持つ銘柄数の上限
    position_value: float = 200_000  # 1銘柄あたりの金額
    # 地合い: 前日の指数が25日平均より「下」のときだけ買う (押し目買い)
    require_down_market: bool = True


@dataclass
class SwingCandidate:
    code: str
    z20: float
    close: float
    turnover: float
    name: str = ""


def market_is_down(index_close: pd.Series, params: SwingParams) -> bool | None:
    """地合い判定。**前日までの情報だけ**を使う (当日終値はまだ分からない).

    None は判定不能 (データ不足)。
    """
    if len(index_close) < params.market_ma + 2:
        return None
    ma = index_close.rolling(params.market_ma).mean()
    # 前日の指数が移動平均より下 = 下げ基調
    prev_close, prev_ma = index_close.iloc[-1], ma.iloc[-1]
    if pd.isna(prev_ma):
        return None
    return bool(prev_close < prev_ma)


def find_candidates(bars: dict[str, pd.DataFrame], params: SwingParams,
                    names: dict[str, str] | None = None) -> list[SwingCandidate]:
    """条件を満たす銘柄を、売られすぎている順に返す.

    bars は code -> 日足 (open/high/low/close/volume、index は日付)。
    **最新の行が「昨日の終値」である前提**。買うのは翌営業日の寄り付き。
    """
    names = names or {}
    out: list[SwingCandidate] = []
    for code, d in bars.items():
        if len(d) < 25:
            continue
        c = d["close"]
        ma20, sd20 = c.rolling(20).mean(), c.rolling(20).std()
        if pd.isna(sd20.iloc[-1]) or sd20.iloc[-1] <= 0:
            continue
        z = float((c.iloc[-1] - ma20.iloc[-1]) / sd20.iloc[-1])
        turnover = float(c.iloc[-1] * d["volume"].iloc[-1])
        if z <= params.z_entry and turnover >= params.min_turnover:
            out.append(SwingCandidate(code=code, z20=z, close=float(c.iloc[-1]),
                                      turnover=turnover, name=names.get(code, "")))
    # より売られている順 (z が小さい順)
    out.sort(key=lambda x: x.z20)
    return out[:params.max_positions]


def shares_for(candidate: SwingCandidate, params: SwingParams) -> int:
    """1銘柄あたりの発注株数 (単元100株に丸める)."""
    n = int(params.position_value / candidate.close / 100) * 100
    return max(n, 0)
