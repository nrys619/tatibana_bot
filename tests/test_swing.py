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


# --- 空売り側 (2026-08-01追加) ---

def test_finds_overbought_for_short():
    """上げ基調のときは、買われすぎ(+2.5σ以上)を探す."""
    spiked = [1000.0] * 24 + [995.0, 1003.0, 996.0, 1002.0, 1100.0]
    bars = {"7203": _bars(spiked)}
    got = find_candidates(bars, SwingParams(), {"7203": "トヨタ"}, side="sell")
    assert [c.code for c in got] == ["7203"]
    assert got[0].z20 >= 2.5
    assert got[0].side == "sell"
    # 同じ銘柄を買い側で探しても引っかからない
    assert find_candidates(bars, SwingParams(), side="buy") == []


def test_short_sorted_by_most_overbought():
    """売りは、より買われている銘柄が先に来る (買いとは逆順)."""
    base = [1000.0] * 24 + [995.0, 1003.0, 996.0, 1002.0]
    bars = {"A": _bars(base + [1100.0]), "B": _bars(base + [1200.0])}
    got = find_candidates(bars, SwingParams(max_positions=2), side="sell")
    assert [c.code for c in got] == ["B", "A"]     # Bの方が買われている


def test_short_pnl_sign_is_inverted():
    """**売りは値下がりが利益**。ここを間違えると全部の符号が逆になる."""
    import sqlite3
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import swing_daily

    conn = sqlite3.connect(":memory:")
    conn.executescript(swing_daily._SCHEMA)
    # 1000円で売建 -> 900円まで下がった = 100円 x 100株 = +10,000円の利益
    conn.execute("""INSERT INTO swing_trades
        (code, name, side, signal_date, entry_date, entry_price, quantity,
         z20, planned_exit_date) VALUES
        ('7203','トヨタ','sell','2026-08-01','2026-08-03',1000.0,100,2.6,'2026-08-08')""")
    conn.commit()
    idx = pd.date_range("2026-08-03", periods=8, freq="D")
    d = pd.DataFrame({"open": 900.0, "high": 910.0, "low": 890.0,
                      "close": 900.0, "volume": 1e6}, index=idx)
    swing_daily.settle(conn, {"7203": d})
    pnl, net = conn.execute("SELECT pnl, pnl_net FROM swing_trades").fetchone()
    assert pnl == pytest.approx(10_000.0)   # 下がって利益 (買いなら-10,000円)
    assert net < pnl                        # コストが引かれている


def test_short_params_match_backtest():
    """空売り側も検証条件から変わっていないこと."""
    p = SwingParams()
    assert p.z_entry_short == 2.5      # +2.5σ以上を売る
    assert p.enable_short is True      # 上げ基調では売りに回る
