"""過去チャート(5分足)でのバックテスト — 方向系ルールの長期検証.

板データ(1秒)のバックテスト(scripts/backtest.py)の補完。
Yahoo Financeの5分足(過去約60日)を再生し、板情報なしで判定できる
ルール(順張り・ブレイクアウト・出来高急増・損切り/利確幅)を検証する。

usage: python scripts/backtest_bars.py [銘柄コード...]
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import pandas as pd

UNIT = 100
COST_BP = 5.0          # 往復コストの半分 (スプレッド+滑り, 片道bp)
MAX_HOLD_BARS = 6      # 30分 (5分足×6本)
SESSION_START = "09:00"
NO_ENTRY_AFTER = "15:00"


@dataclass
class BarVariant:
    name: str
    momentum_bars: int = 6      # 何本前と比べて順張り判定するか (6本=30分)
    volume_surge: float = 0.0   # 出来高が直近平均の何倍で入るか (0=条件なし)
    breakout: bool = False      # 当日高値(安値)更新で入る
    stop_pct: float = 0.4
    target_pct: float = 0.8
    maker_entry: bool = False   # ①指値: 入場コストゼロと仮定 (楽観)
    hours_filter: bool = False  # ③ 9:00-10:00 と 14:30-15:00 のみ新規
    trailing_pct: float = 0.0   # ⑤ ピークからの押しで決済 (0=無効)


def load_bars(code: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(f"{code}.T", interval="5m", period="60d",
                     auto_adjust=False, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    df.index = pd.to_datetime(df.index).tz_convert("Asia/Tokyo").tz_localize(None)
    return df.dropna()


def run(v: BarVariant, bars: pd.DataFrame) -> list[float]:
    trades: list[float] = []
    for day, g in bars.groupby(bars.index.date):
        g = g.between_time(SESSION_START, "15:30")
        closes = g["close"].to_numpy()
        volumes = g["volume"].to_numpy()
        highs = g["high"].to_numpy()
        lows = g["low"].to_numpy()
        times = g.index
        pos = None  # (side, entry_px, entry_i)
        for i in range(len(g)):
            px = closes[i]
            # --- 決済 ---
            if pos is not None:
                side, epx, ei, peak = pos
                sign = 1 if side == "buy" else -1
                stop = epx * (1 - sign * v.stop_pct / 100)
                exit_px = None
                hit_stop = lows[i] <= stop if side == "buy" else highs[i] >= stop
                if v.trailing_pct > 0:
                    peak = max(peak, highs[i]) if side == "buy" else min(peak, lows[i])
                    pos = (side, epx, ei, peak)
                    trail = peak * (1 - sign * v.trailing_pct / 100)
                    hit_trail = (lows[i] <= trail and peak > epx) if side == "buy" \
                        else (highs[i] >= trail and peak < epx)
                    if hit_stop:
                        exit_px = stop
                    elif hit_trail:
                        exit_px = trail
                    elif i == len(g) - 1:
                        exit_px = px
                else:
                    target = epx * (1 + sign * v.target_pct / 100)
                    hit_target = highs[i] >= target if side == "buy" else lows[i] <= target
                    if hit_stop:      # 同一バーで両方触れたら不利な方(損切り)を採用
                        exit_px = stop
                    elif hit_target:
                        exit_px = target
                    elif i - ei >= MAX_HOLD_BARS or i == len(g) - 1:
                        exit_px = px
                if exit_px is not None:
                    cost = exit_px * COST_BP / 10000
                    fill = exit_px - cost if side == "buy" else exit_px + cost
                    pnl = (fill - epx) * UNIT if side == "buy" else (epx - fill) * UNIT
                    trades.append(pnl)
                    pos = None
                continue
            # --- エントリー ---
            if i < max(v.momentum_bars, 3):  # 15分のウォームアップ
                continue
            if str(times[i].time()) >= NO_ENTRY_AFTER + ":00":
                continue
            if v.hours_filter:
                hhmm = times[i].strftime("%H:%M")
                if not ("09:00" <= hhmm < "10:00" or "14:30" <= hhmm < "15:00"):
                    continue
            mom = px - closes[i - v.momentum_bars]
            side = None
            if v.breakout:
                day_high, day_low = highs[:i].max(), lows[:i].min()
                if px >= day_high and mom > 0:
                    side = "buy"
                elif px <= day_low and mom < 0:
                    side = "sell"
            else:
                if mom > 0 and closes[i - 1] < px:
                    side = "buy"
                elif mom < 0 and closes[i - 1] > px:
                    side = "sell"
            if side is None:
                continue
            if v.volume_surge > 0:
                avg_vol = volumes[max(0, i - 12):i].mean()
                if volumes[i] < v.volume_surge * max(avg_vol, 1):
                    continue
            if v.maker_entry:
                epx = px
            else:
                cost = px * COST_BP / 10000
                epx = px + cost if side == "buy" else px - cost
            pos = (side, epx, i, epx)
    return trades


VARIANTS = [
    BarVariant("順張りのみ"),
    BarVariant("順+量+①指値", volume_surge=2.0, maker_entry=True),
    BarVariant("順+量+③時間帯", volume_surge=2.0, hours_filter=True, momentum_bars=3),
    BarVariant("順+量+⑤トレール", volume_surge=2.0, trailing_pct=0.3),
    BarVariant("順+量+①③", volume_surge=2.0, maker_entry=True, hours_filter=True,
               momentum_bars=3),
    BarVariant("順+量+①③⑤", volume_surge=2.0, maker_entry=True,
               hours_filter=True, trailing_pct=0.3, momentum_bars=3),
    BarVariant("ブレイク+量+①③⑤", breakout=True, volume_surge=2.0,
               maker_entry=True, hours_filter=True, trailing_pct=0.3, momentum_bars=3),
    BarVariant("ブレイク+量+①③ト.5", breakout=True, volume_surge=2.0,
               maker_entry=True, hours_filter=True, trailing_pct=0.5, momentum_bars=3),
    BarVariant("順張り+出来高2倍", volume_surge=2.0),
    BarVariant("ブレイクアウト", breakout=True),
    BarVariant("ブレイク+出来高2倍", breakout=True, volume_surge=2.0),
    BarVariant("ブレイク+量 RR3", breakout=True, volume_surge=2.0, target_pct=1.2),
    BarVariant("ブレイク+量 タイト", breakout=True, volume_surge=2.0,
               stop_pct=0.3, target_pct=0.6),
    BarVariant("順張り+量 RR3", volume_surge=2.0, target_pct=1.2),
]

DEFAULT_CODES = ["5802", "6526", "7012", "7013", "6522", "7203", "8306", "9432"]


def main() -> None:
    codes = sys.argv[1:] or DEFAULT_CODES
    print(f"銘柄: {codes} (5分足・過去60日)")
    all_bars = {}
    for c in codes:
        try:
            b = load_bars(c)
            if len(b):
                all_bars[c] = b
        except Exception as e:
            print(f"  {c}: 取得失敗 {e}")
    print(f"取得成功: {len(all_bars)}銘柄, 合計{sum(len(b) for b in all_bars.values())}本")
    header = f"{'バリアント':<20} 取引数   合計損益   平均/回  勝率"
    print(header); print("-" * len(header))
    for v in VARIANTS:
        trades = []
        for bars in all_bars.values():
            trades += run(v, bars)
        n = len(trades)
        wins = sum(1 for x in trades if x > 0)
        total = sum(trades)
        print(f"{v.name:<20} {n:5d} {total:+10.0f} {total / n if n else 0:+8.0f}"
              f"  {wins / n if n else 0:5.1%}")


if __name__ == "__main__":
    main()
