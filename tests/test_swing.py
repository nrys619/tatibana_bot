"""スイング戦略のテスト.

**バックテストで検証した条件と実装が1つでもズレたら、検証結果は意味を失う。**
デイトレ側では「シミュと実機の相関0.59」という乖離に苦しんだので、
スイングでは最初から条件を固定する。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tatibana_bot.swing.strategy import (
    SwingParams,
    find_candidates,
    market_is_down,
    shares_for,
)


def _bars(closes: list[float], volume: float = 5_000_000) -> pd.DataFrame:
    idx = pd.date_range("2026-06-01", periods=len(closes), freq="D")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99,
                         "close": c, "volume": volume}, index=idx)


def test_finds_oversold_stock():
    """20日平均から-2σ以下まで売られた銘柄を拾う."""
    # 平常時は1000円前後 -> 最後に急落 (売買代金は45億円で基準を満たす)
    crashed = [1000.0] * 24 + [1005.0, 997.0, 1004.0, 998.0, 900.0]
    bars = {"7203": _bars(crashed)}
    got = find_candidates(bars, SwingParams(), {"7203": "トヨタ"})
    assert [c.code for c in got] == ["7203"]
    assert got[0].z20 <= -2.0
    assert got[0].name == "トヨタ"


def test_ignores_stock_that_is_not_oversold():
    """普通に推移している銘柄は拾わない."""
    bars = {"7203": _bars([1000.0 + (i % 5) for i in range(30)])}
    assert find_candidates(bars, SwingParams()) == []


def test_ignores_illiquid_stock():
    """売買代金が細い銘柄は、実際には注文が通らないので除外する."""
    flat = [1000.0] * 24 + [1005.0, 997.0, 1004.0, 998.0, 900.0]
    # 出来高100株 -> 売買代金9万円 (基準10億円に遠く及ばない)
    bars = {"7203": _bars(flat, volume=100)}
    assert find_candidates(bars, SwingParams()) == []


def test_limits_number_of_positions():
    """同時に持つ銘柄数の上限を守る."""
    flat = [1000.0] * 24 + [1005.0, 997.0, 1004.0, 998.0, 900.0]
    bars = {f"{i:04d}": _bars(flat) for i in range(30)}
    got = find_candidates(bars, SwingParams(max_positions=10))
    assert len(got) == 10


def test_sorted_by_most_oversold():
    """より売られている銘柄が先に来る."""
    base = [1000.0] * 24 + [1005.0, 997.0, 1004.0, 998.0]
    bars = {"A": _bars(base + [900.0]), "B": _bars(base + [800.0])}
    got = find_candidates(bars, SwingParams(max_positions=2))
    assert [c.code for c in got] == ["B", "A"]     # Bの方が売られている


def test_market_down_uses_only_past_information():
    """地合い判定は前日までの情報だけを使う (当日終値を見たらカンニング)."""
    idx = pd.Series([100.0] * 30, index=pd.date_range("2026-06-01", periods=30))
    assert market_is_down(idx, SwingParams()) is False      # 平均と同じ = 下げ基調でない

    falling = pd.Series(np.linspace(120, 90, 30),
                        index=pd.date_range("2026-06-01", periods=30))
    assert market_is_down(falling, SwingParams()) is True   # 下げ基調

    short = pd.Series([100.0] * 5, index=pd.date_range("2026-06-01", periods=5))
    assert market_is_down(short, SwingParams()) is None      # データ不足は判定しない


def test_shares_rounded_to_unit():
    """発注株数は単元(100株)に丸める."""
    from tatibana_bot.swing.strategy import SwingCandidate
    p = SwingParams(position_value=200_000)
    assert shares_for(SwingCandidate("x", -2.5, 1000.0, 1e9), p) == 200
    assert shares_for(SwingCandidate("x", -2.5, 3000.0, 1e9), p) == 0   # 1単元に満たない
    assert shares_for(SwingCandidate("x", -2.5, 1900.0, 1e9), p) == 100


def test_params_match_backtest():
    """**検証で使った条件から勝手に変わっていないこと** (最重要)."""
    p = SwingParams()
    assert p.z_entry == -2.0        # 20日平均から-2σ
    assert p.hold_days == 5         # 5営業日保有
    assert p.min_turnover == 1e9    # 売買代金10億円以上
    assert p.require_down_market is True   # 下げ基調のときだけ
