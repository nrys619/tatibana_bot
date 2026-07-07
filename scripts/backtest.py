"""板スナップショットを再生して戦略バリエーションの成績を測るバックテスト.

usage:
  python scripts/backtest.py                # data/snapshots/ の全日付
  python scripts/backtest.py 20260706 ...   # 日付指定

各バリアントは「現行ルール(imbalance+buy_ratio+microprice)」を土台に、
テスタ流・cis流のフィルタ/トリガーを足し引きしたもの。
約定は成行を想定し、半スプレッド+2bpの滑りをコストとして両側に課す。
"""

from __future__ import annotations

import json
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

UNIT = 100          # 1単元
SLIP_BP = 2.0       # 滑り (片道bp)
COOLDOWN_S = 60.0   # 決済後の再エントリー禁止秒数
MAX_HOLD_S = 1800.0


@dataclass
class Variant:
    name: str
    imbalance: float = 0.3
    tape_ratio: float = 0.6
    stop_pct: float = 0.4
    target_pct: float = 0.8
    max_spread_bps: float = 20.0
    trend_filter: bool = False      # cis: 5分前より上の時だけ買う (下も同様)
    breakout_only: bool = False     # cis: 本日高値(安値)圏でのみ入る
    volume_surge: bool = False      # テスタ: 約定が平常時の2倍以上の時だけ
    absorption: bool = False        # テスタ: 売り板が減っている時だけ買う (逆も)
    resilience: bool = False        # テスタ: 直近下げから即戻した銘柄だけ買う
    momentum_exit: bool = False     # cis: 5分モメンタムが逆転したら早期撤退
    maker_entry: bool = False       # ①指値エントリー: 入場コストをゼロと仮定 (楽観シナリオ)
    hours_filter: bool = False      # ③時間帯限定: 9:00-10:00 と 14:30-15:00 のみ新規
    trailing_pct: float = 0.0       # ⑤トレーリング: ピークからこの%押したら決済 (0=無効)
    surge_ratio: float = 2.0        # 出来高急増の倍率
    absorption_ratio: float = 0.7   # 吸収判定: 反対板がこの割合未満に減ったら
    confirm_ticks: int = 0          # A: 合図がこの秒数連続で点灯したら入る (0=即)
    min_range_pct: float = 0.0      # B: 直近5分の値幅がこの%未満の銘柄は見送り
    scratch_sec: float = 0.0        # C: この秒数たっても伸びない取引は±0撤退 (0=無効)
    skip_open_min: int = 0          # D: 寄りからこの分数は見送り
    loss_cooldown_sec: float = 0.0  # E: その銘柄で負けたらこの秒数出禁 (0=通常60s)


@dataclass
class CodeState:
    hist: deque = field(default_factory=lambda: deque())  # (t, price, ask_depth, bid_depth, ticks)
    session_high: float = 0.0
    session_low: float = 1e18
    tick_sum: float = 0.0
    tick_n: int = 0


def _max_price() -> float:
    """実機と同じ予算フィルタ (1単元が建玉上限に収まる株価) を config から算出."""
    try:
        from tatibana_bot.config import load_config
        return float(load_config().risk.max_position_value) / 100
    except Exception:
        return 3000.0


MAX_PRICE = _max_price()


def _load_day(path: Path) -> dict[str, list[dict]]:
    per_code: dict[str, list[dict]] = {}
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            per_code.setdefault(r["code"], []).append(r)
    # 実機が買えない値がさ株を除外 (代表価格で判定)
    for code in list(per_code):
        rows = per_code[code]
        prices = [r["last_price"] for r in rows if r.get("last_price")]
        if not prices or sorted(prices)[len(prices) // 2] > MAX_PRICE:
            del per_code[code]
            continue
        rows.sort(key=lambda r: r["ts"])
    return per_code


def _epoch(ts: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(ts).timestamp()


def run_variant(v: Variant, per_code: dict[str, list[dict]]) -> dict:
    trades: list[float] = []
    for code, rows in per_code.items():
        st = CodeState()
        pos = None
        cooldown_until = 0.0
        streak_side, streak_n = None, 0
        for r in rows:
            t = _epoch(r["ts"])
            px = r.get("last_price")
            if not px:
                continue
            st.session_high = max(st.session_high, px)
            st.session_low = min(st.session_low, px)
            st.tick_sum += r.get("tick_count", 0.0)
            st.tick_n += 1
            st.hist.append((t, px, r.get("ask_depth", 0.0), r.get("bid_depth", 0.0)))
            while st.hist and t - st.hist[0][0] > 300:
                st.hist.popleft()

            # --- 5分前の状態 ---
            oldest = st.hist[0]
            px_5m, ask_5m, bid_5m = oldest[1], oldest[2], oldest[3]
            win_prices = [h[1] for h in st.hist]
            mom_up = px > px_5m
            mom_dn = px < px_5m

            # --- 決済判定 ---
            if pos is not None:
                side, epx, et, stop, target, peak = pos
                exit_reason = None
                if v.scratch_sec > 0 and t - et >= v.scratch_sec:
                    move = (px - epx) / epx * 100 * (1 if side == "buy" else -1)
                    if move < 0.05:  # 5分たってほぼ伸びていない
                        exit_reason = "scratch"
                if v.trailing_pct > 0:  # ⑤利確目標なし、ピークからの押しで決済
                    if side == "buy":
                        peak = max(peak, px)
                        if px <= stop: exit_reason = "stop"
                        elif px <= peak * (1 - v.trailing_pct / 100) and peak > epx:
                            exit_reason = "trail"
                    else:
                        peak = min(peak, px)
                        if px >= stop: exit_reason = "stop"
                        elif px >= peak * (1 + v.trailing_pct / 100) and peak < epx:
                            exit_reason = "trail"
                    pos = (side, epx, et, stop, target, peak)
                elif side == "buy":
                    if px <= stop: exit_reason = "stop"
                    elif px >= target: exit_reason = "target"
                    elif v.momentum_exit and mom_dn and t - et > 60: exit_reason = "mom"
                else:
                    if px >= stop: exit_reason = "stop"
                    elif px <= target: exit_reason = "target"
                    elif v.momentum_exit and mom_up and t - et > 60: exit_reason = "mom"
                if exit_reason is None and t - et >= MAX_HOLD_S:
                    exit_reason = "time"
                if exit_reason:
                    cost = px * (r.get("spread_bps", 5) / 2 + SLIP_BP) / 10000
                    fill = px - cost if side == "buy" else px + cost
                    pnl = (fill - epx) * UNIT if side == "buy" else (epx - fill) * UNIT
                    trades.append(pnl)
                    pos = None
                    cd = COOLDOWN_S
                    if pnl < 0 and v.loss_cooldown_sec > 0:
                        cd = v.loss_cooldown_sec
                    cooldown_until = t + cd
                continue

            # --- エントリー判定 ---
            if t < cooldown_until or len(st.hist) < 60:
                continue
            if v.hours_filter:  # ③ 寄り後1時間 + 引け前 (14:30-15:00) のみ
                hhmm = r["ts"][11:16]
                if not ("09:00" <= hhmm < "10:00" or "14:30" <= hhmm < "15:00"):
                    continue
            if v.skip_open_min > 0:
                hhmmss = r["ts"][11:19]
                if hhmmss < f"09:{v.skip_open_min:02d}:00":
                    continue
            if v.min_range_pct > 0:
                rng = (max(win_prices) - min(win_prices)) / px * 100
                if rng < v.min_range_pct:
                    continue
            if r.get("spread_bps", 99) > v.max_spread_bps:
                continue
            imb, br, micro = r.get("imbalance", 0), r.get("buy_ratio", 0.5), r.get("microprice_dev", 0)
            long_ok = imb >= v.imbalance and br >= v.tape_ratio and micro > 0
            short_ok = imb <= -v.imbalance and br <= 1 - v.tape_ratio and micro < 0
            if not long_ok and not short_ok:
                streak_side, streak_n = None, 0
                continue
            side = "buy" if long_ok else "sell"
            if v.confirm_ticks > 0:
                if side == streak_side:
                    streak_n += 1
                else:
                    streak_side, streak_n = side, 1
                if streak_n < v.confirm_ticks:
                    continue

            if v.trend_filter:
                if side == "buy" and not mom_up: continue
                if side == "sell" and not mom_dn: continue
            if v.breakout_only:
                if side == "buy" and px < st.session_high * 0.999: continue
                if side == "sell" and px > st.session_low * 1.001: continue
            if v.volume_surge:
                avg_ticks = st.tick_sum / max(st.tick_n, 1)
                if r.get("tick_count", 0.0) < v.surge_ratio * max(avg_ticks, 0.5): continue
            if v.absorption:
                if side == "buy" and not (ask_5m > 0 and r.get("ask_depth", 0) < v.absorption_ratio * ask_5m): continue
                if side == "sell" and not (bid_5m > 0 and r.get("bid_depth", 0) < v.absorption_ratio * bid_5m): continue
            if v.resilience:
                lo, hi = min(win_prices), max(win_prices)
                if side == "buy" and not (lo < px * 0.9985 and px > lo * 1.001): continue
                if side == "sell" and not (hi > px * 1.0015 and px < hi * 0.999): continue

            if v.maker_entry:
                epx = px  # ①指値: コストゼロで約定と仮定 (取り逃しは考慮しない楽観値)
            else:
                cost = px * (r.get("spread_bps", 5) / 2 + SLIP_BP) / 10000
                epx = px + cost if side == "buy" else px - cost
            sign = 1 if side == "buy" else -1
            pos = (side, epx, t,
                   epx * (1 - sign * v.stop_pct / 100),
                   epx * (1 + sign * v.target_pct / 100),
                   epx)

        if pos is not None:  # 引けで強制決済
            side, epx, et, _, _, _ = pos
            px = rows[-1]["last_price"]
            pnl = (px - epx) * UNIT if side == "buy" else (epx - px) * UNIT
            trades.append(pnl)

    wins = [x for x in trades if x > 0]
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "total": sum(trades),
        "avg": sum(trades) / len(trades) if trades else 0.0,
        "worst": min(trades) if trades else 0.0,
    }


BASE_E = dict(absorption=True, volume_surge=True, trend_filter=True, imbalance=0.4)
FULL = dict(**BASE_E, maker_entry=True, max_spread_bps=8.0, hours_filter=True, trailing_pct=0.3)

def _relax(name, **over):
    kw = {**FULL, **over}
    return Variant(name, **kw)

LIVE = dict(trend_filter=True, volume_surge=True, imbalance=0.4, maker_entry=True,
            max_spread_bps=8.0, hours_filter=True, trailing_pct=0.3)

def _live(name, **over):
    return Variant(name, **{**LIVE, **over})

VARIANTS = [
    _live("★現ライブ設定 (B+E込み)", min_range_pct=0.3, loss_cooldown_sec=1800),
    _live("参考: B/Eなし"),
    _live("+A 3秒連続確認", confirm_ticks=3),
    _live("+B 値幅0.3%下限", min_range_pct=0.3),
    _live("+C 5分見切り", scratch_sec=300),
    _live("+D 寄り5分回避", skip_open_min=5),
    _live("+E 負け銘柄30分出禁", loss_cooldown_sec=1800),
    _live("+A+B", confirm_ticks=3, min_range_pct=0.3),
    _live("+A+B+C", confirm_ticks=3, min_range_pct=0.3, scratch_sec=300),
    _live("+A+B+C+D+E", confirm_ticks=3, min_range_pct=0.3, scratch_sec=300,
          skip_open_min=5, loss_cooldown_sec=1800),
    _relax("フル装備 (旧: 吸収あり)"),
    _relax("緩和a: 時間帯制限なし", hours_filter=False),
    _relax("緩和b: imbalance 0.3", imbalance=0.3),
    _relax("緩和c: 出来高1.5倍", surge_ratio=1.5),
    _relax("緩和d: 吸収0.85", absorption_ratio=0.85),
    _relax("緩和e: 吸収なし", absorption=False),
    _relax("緩和a+c", hours_filter=False, surge_ratio=1.5),
    _relax("緩和a+c+d", hours_filter=False, surge_ratio=1.5, absorption_ratio=0.85),
    _relax("緩和a+b+c+e", hours_filter=False, imbalance=0.3, surge_ratio=1.5, absorption=False),

    Variant("現行 (基準)"),
    Variant("E (今の設定)", **BASE_E),
    Variant("E+①指値", **BASE_E, maker_entry=True),
    Variant("E+②spread8", **BASE_E, max_spread_bps=8.0),
    Variant("E+③時間帯", **BASE_E, hours_filter=True),
    Variant("E+⑤トレール.3", **BASE_E, trailing_pct=0.3),
    Variant("E+①②③", **BASE_E, maker_entry=True, max_spread_bps=8.0, hours_filter=True),
    Variant("E+①②③⑤", **BASE_E, maker_entry=True, max_spread_bps=8.0,
            hours_filter=True, trailing_pct=0.3),
    Variant("順+量+①②③⑤", trend_filter=True, volume_surge=True, maker_entry=True,
            max_spread_bps=8.0, hours_filter=True, trailing_pct=0.3),
    Variant("厳選A: 順+量 imb.45", trend_filter=True, volume_surge=True,
            imbalance=0.45, tape_ratio=0.7),
    Variant("厳選B: A+広RR .6/1.2", trend_filter=True, volume_surge=True,
            imbalance=0.45, tape_ratio=0.7, stop_pct=0.6, target_pct=1.2),
    Variant("厳選C: A+早期撤退", trend_filter=True, volume_surge=True,
            imbalance=0.45, tape_ratio=0.7, momentum_exit=True),
    Variant("厳選D: ブレイク+量", breakout_only=True, volume_surge=True,
            imbalance=0.4),
    Variant("厳選E: 吸収+量+順", absorption=True, volume_surge=True,
            trend_filter=True, imbalance=0.4),
    Variant("厳選F: B+スプレッド8bp", trend_filter=True, volume_surge=True,
            imbalance=0.45, tape_ratio=0.7, stop_pct=0.6, target_pct=1.2,
            max_spread_bps=8.0),
    Variant("cis順張り", trend_filter=True),
    Variant("cisブレイク", breakout_only=True),
    Variant("cis順張り+早期撤退", trend_filter=True, momentum_exit=True),
    Variant("テスタ吸収", absorption=True),
    Variant("テスタ出来高急増", volume_surge=True),
    Variant("テスタ復元力", resilience=True),
    Variant("テスタ吸収+順張り", absorption=True, trend_filter=True),
    Variant("出来高急増+順張り", volume_surge=True, trend_filter=True),
    Variant("順張りRR3 (0.4/1.2)", trend_filter=True, target_pct=1.2),
    Variant("順張りタイト (0.3/0.6)", trend_filter=True, stop_pct=0.3, target_pct=0.6),
    Variant("全部乗せ", trend_filter=True, volume_surge=True, momentum_exit=True),
]


def main() -> None:
    snap_dir = Path("data/snapshots")
    days = sys.argv[1:] or sorted(p.stem for p in snap_dir.glob("*.jsonl"))
    day_data = {d: _load_day(snap_dir / f"{d}.jsonl") for d in days}
    print(f"対象日: {days}")
    header = f"{'バリアント':<24}" + "".join(f"{d[-4:]}損益 " for d in days) + "合計損益  取引数 勝率"
    print(header)
    print("-" * len(header))
    for v in VARIANTS:
        results = [run_variant(v, day_data[d]) for d in days]
        total = sum(r["total"] for r in results)
        n = sum(r["trades"] for r in results)
        wins = sum(r["win_rate"] * r["trades"] for r in results)
        wr = wins / n if n else 0.0
        cells = "".join(f"{r['total']:+8.0f} " for r in results)
        print(f"{v.name:<24}{cells}{total:+9.0f}  {n:5d} {wr:5.1%}")


if __name__ == "__main__":
    main()
