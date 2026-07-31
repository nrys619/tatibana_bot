"""板スナップショットを再生して戦略バリエーションの成績を測るバックテスト.

usage:
  python scripts/backtest.py                # data/snapshots/ の全日付
  python scripts/backtest.py 20260706 ...   # 日付指定

各バリアントは「現行ルール(imbalance+buy_ratio+microprice)」を土台に、
テスタ流・cis流のフィルタ/トリガーを足し引きしたもの。
約定は成行を想定し、半スプレッド+2bpの滑りをコストとして両側に課す。
"""

from __future__ import annotations

import gzip
import json
import sys
from collections import deque
from math import isfinite
from dataclasses import dataclass, field
from pathlib import Path

_HOLD_SECS: list[float] = []
UNIT = 100          # 1単元
MAX_COST_BPS = 50.0  # 板の片側が空だと spread_bps=inf で記録される。コストはここで頭打ちにする


def _spread_bps(r: dict) -> float:
    """記録されたスプレッド。異常値(inf/NaN/負)は上限値に丸める."""
    sp = r.get("spread_bps", 5.0)
    if sp is None or not isfinite(sp) or sp < 0:
        return MAX_COST_BPS
    return min(sp, MAX_COST_BPS)


def _live_qty(v: "Variant", side: str, px: float, imb: float, br: float) -> int:
    """実機と同じ発注株数を返す。0なら見送り。

    実機のコード (tatibana_bot.risk.sizing.position_size) をそのまま呼ぶ。
    書き写すとズレるため、意図的に import して使う。
    """
    from datetime import datetime

    from tatibana_bot.models import Side, Signal
    from tatibana_bot.risk.sizing import SizingParams, position_size

    strength = min(abs(imb) / max(v.imbalance, 1e-9),
                   abs(br - 0.5) / max(v.tape_ratio - 0.5, 1e-9))
    conf = min(0.5 + 0.25 * strength, 0.9)
    sign = 1 if side == "buy" else -1
    sg = Signal(ts=datetime(2000, 1, 1), code="x",
                side=Side.BUY if side == "buy" else Side.SELL,
                confidence=conf, reason="", entry_price=px,
                stop_price=px * (1 - sign * v.stop_pct / 100),
                target_price=px * (1 + sign * v.target_pct / 100))
    return position_size(
        sg, SizingParams(equity_jpy=v.equity_jpy,
                         max_position_value=v.max_position_value), v.regime_mult)
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
    vol_stop: bool = False          # F: 損切り/トレール幅を直近5分の変動幅に連動させる
    ml_veto: float = 0.0            # ML: 方向確率がこの値未満なら見送り (0=無効)
    side_filter: str = ""           # G: "sell"=売りのみ / "buy"=買いのみ ("" = 両方)
    # --- 実機再現 (Phase 0): 既定は全て無効 = 従来と同じ挙動 ---
    explore: bool = False           # 探索モード: 本命が不成立なら緩い条件で試し玉
    explore_imbalance: float = 0.35
    explore_surge: float = 1.2
    explore_max_price: float = 3000.0
    respect_watchlist: bool = False  # その時刻に実機が監視していた銘柄だけ売買する
    fill_timeout_sec: float = 0.0    # ①指値の待ち時間。>0 なら刺さらなければ見送り
    live_sizing: bool = False        # 実機と同じ建玉サイズ計算を使う (100株固定をやめる)
    equity_jpy: float = 2_000_000
    max_position_value: float = 1_500_000
    regime_mult: float = 1.0         # 地合いによるサイズ倍率 (実機ログから取る)
    anomaly: bool = False            # 実機の異常検知 (板の急減/急変動なら逃げる・入らない)
    anomaly_window_sec: float = 120.0
    depth_drop_ratio: float = 0.3
    price_shock_bps: float = 300.0
    spread_blowout_bps: float = 50.0
    max_total_exposure: float = 0.0  # 建玉総額の上限 (0=無制限)。実機は600万円
    gross_pnl: bool = False          # 決済コストを引かない (実機DBの記録方式に合わせる時だけ)


@dataclass
class CodeState:
    pos: tuple | None = None
    pos_tag: str = "main"
    pos_qty: int = UNIT
    cooldown_until: float = 0.0
    streak_side: str | None = None
    streak_n: int = 0
    anom: deque = field(default_factory=lambda: deque())  # (t, total_depth, price)
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


def _snap_path(day_or_path):
    """圧縮済みならそちらを返す (.jsonl / .jsonl.gz を透過的に扱う)."""
    p = Path(day_or_path) if not isinstance(day_or_path, Path) else day_or_path
    if p.suffix != ".gz" and not p.exists():
        gz = Path(str(p) + ".gz")
        if gz.exists():
            return gz
    return p


def _load_day(path: Path) -> dict[str, list[dict]]:
    per_code: dict[str, list[dict]] = {}
    path = _snap_path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as f:
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


_SCORER = None

def _get_scorer():
    global _SCORER
    if _SCORER is None:
        from tatibana_bot.signals.model import load_scorer
        _SCORER = load_scorer("/tmp/intraday_candidate.txt") or (lambda f: 0.5)
    return _SCORER


def run_variant(v: Variant, per_code: dict[str, list[dict]],
                day: str | None = None, timeline=None) -> dict:
    global _HOLD_SECS
    _HOLD_SECS = []
    trades: list[float] = []
    tags: list[str] = []
    # 実機は1本のループで全銘柄を時刻順に処理する。建玉総額の上限を効かせるには
    # 銘柄をまたいだ同時保有を把握する必要があるため、ここでも時刻順に混ぜて回す。
    states: dict[str, CodeState] = {c: CodeState() for c in per_code}
    stream = sorted(
        ((_epoch(r["ts"]), code, i, r)
         for code, rows in per_code.items() for i, r in enumerate(rows)),
        key=lambda x: x[0],
    )
    for t, code, i, r in stream:
            rows = per_code[code]
            st = states[code]
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

            # --- 異常検知 (実機の AnomalyDetector と同じ条件) ---
            anom_hit = False
            if v.anomaly:
                depth = r.get("bid_depth", 0.0) + r.get("ask_depth", 0.0)
                sp = r.get("spread_bps", 0.0)
                if sp and sp != float("inf") and sp >= v.spread_blowout_bps:
                    anom_hit = True
                while st.anom and t - st.anom[0][0] > v.anomaly_window_sec:
                    st.anom.popleft()
                if st.anom:
                    avg_depth = sum(h[1] for h in st.anom) / len(st.anom)
                    if avg_depth > 0 and depth < avg_depth * v.depth_drop_ratio:
                        anom_hit = True
                    old_px = st.anom[0][2]
                    if old_px and abs(px - old_px) / old_px * 10000 >= v.price_shock_bps:
                        anom_hit = True
                st.anom.append((t, depth, px))

            # --- 5分前の状態 ---
            oldest = st.hist[0]
            px_5m, ask_5m, bid_5m = oldest[1], oldest[2], oldest[3]
            win_prices = [h[1] for h in st.hist]
            mom_up = px > px_5m
            mom_dn = px < px_5m

            # --- 決済判定 ---
            if st.pos is not None:
                side, epx, et, stop, target, peak = st.pos
                exit_reason = None
                if v.scratch_sec > 0 and t - et >= v.scratch_sec:
                    move = (px - epx) / epx * 100 * (1 if side == "buy" else -1)
                    if move < 0.05:  # 5分たってほぼ伸びていない
                        exit_reason = "scratch"
                if v.trailing_pct > 0:  # ⑤利確目標なし、ピークからの押しで決済
                    trail = v.trailing_pct
                    if v.vol_stop:
                        rng5 = (max(win_prices) - min(win_prices)) / max(px, 1) * 100
                        trail = min(max(0.4 * rng5, 0.25), 1.0)
                    if side == "buy":
                        peak = max(peak, px)
                        if px <= stop: exit_reason = "stop"
                        elif px <= peak * (1 - trail / 100) and peak > epx:
                            exit_reason = "trail"
                    else:
                        peak = min(peak, px)
                        if px >= stop: exit_reason = "stop"
                        elif px >= peak * (1 + trail / 100) and peak < epx:
                            exit_reason = "trail"
                    st.pos = (side, epx, et, stop, target, peak)
                elif side == "buy":
                    if px <= stop: exit_reason = "stop"
                    elif px >= target: exit_reason = "target"
                    elif v.momentum_exit and mom_dn and t - et > 60: exit_reason = "mom"
                else:
                    if px >= stop: exit_reason = "stop"
                    elif px <= target: exit_reason = "target"
                    elif v.momentum_exit and mom_up and t - et > 60: exit_reason = "mom"
                if exit_reason is None and anom_hit:
                    exit_reason = "anomaly"
                if exit_reason is None and t - et >= MAX_HOLD_S:
                    exit_reason = "time"
                if exit_reason:
                    cost = 0.0 if v.gross_pnl else px * (_spread_bps(r) / 2 + SLIP_BP) / 10000
                    fill = px - cost if side == "buy" else px + cost
                    q = st.pos_qty
                    pnl = (fill - epx) * q if side == "buy" else (epx - fill) * q
                    trades.append(pnl)
                    tags.append(st.pos_tag)
                    _HOLD_SECS.append(t - et)
                    st.pos = None
                    cd = COOLDOWN_S
                    if pnl < 0 and v.loss_cooldown_sec > 0:
                        cd = v.loss_cooldown_sec
                    st.cooldown_until = t + cd
                continue

            # --- エントリー判定 ---
            if anom_hit:
                continue
            if t < st.cooldown_until or len(st.hist) < 60:
                continue
            if v.respect_watchlist and timeline is not None and day is not None:
                # 記録専用銘柄や、その時刻に監視から外れていた銘柄は実機は売買できない
                if code not in timeline.codes_at(day, r["ts"][11:19]):
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
            avg_ticks = st.tick_sum / max(st.tick_n, 1)

            def _gate(imb_th, surge, use_abs, use_brk):
                """条件を満たせば "buy"/"sell"、だめなら None. 本命と探索で共用."""
                if imb >= imb_th and br >= v.tape_ratio and micro > 0:
                    s = "buy"
                elif imb <= -imb_th and br <= 1 - v.tape_ratio and micro < 0:
                    s = "sell"
                else:
                    return None
                if v.side_filter and s != v.side_filter:
                    return None
                if v.trend_filter:
                    if s == "buy" and not mom_up: return None
                    if s == "sell" and not mom_dn: return None
                if use_brk:
                    if s == "buy" and px < st.session_high * 0.999: return None
                    if s == "sell" and px > st.session_low * 1.001: return None
                if v.volume_surge:
                    if r.get("tick_count", 0.0) < surge * max(avg_ticks, 0.5): return None
                if use_abs:
                    if s == "buy" and not (ask_5m > 0 and r.get("ask_depth", 0) < v.absorption_ratio * ask_5m): return None
                    if s == "sell" and not (bid_5m > 0 and r.get("bid_depth", 0) < v.absorption_ratio * bid_5m): return None
                if v.resilience:
                    lo, hi = min(win_prices), max(win_prices)
                    if s == "buy" and not (lo < px * 0.9985 and px > lo * 1.001): return None
                    if s == "sell" and not (hi > px * 1.0015 and px < hi * 0.999): return None
                return s

            side = _gate(v.imbalance, v.surge_ratio, v.absorption, v.breakout_only)
            tag = "main"
            if side is None and v.explore and px <= v.explore_max_price:
                # 実機の探索モードは吸収・ブレイクを使わず、板不均衡と出来高だけ緩める
                side = _gate(v.explore_imbalance, v.explore_surge, False, False)
                tag = "explore"
            if side is None:
                st.streak_side, st.streak_n = None, 0
                continue
            if v.ml_veto > 0:  # ML: 30秒後の方向予測でふるいにかける
                prob = float(_get_scorer()(r))
                side_prob = prob if side == "buy" else 1.0 - prob
                if side_prob < v.ml_veto:
                    continue
            if v.confirm_ticks > 0:
                if side == st.streak_side:
                    st.streak_n += 1
                else:
                    st.streak_side, st.streak_n = side, 1
                if st.streak_n < v.confirm_ticks:
                    continue

            if v.maker_entry and v.fill_timeout_sec > 0:
                # 実機は best bid/ask に指値を置き、待っても刺さらなければ取り消す。
                # 待ち時間内に価格がその指値に届いたときだけ約定とみなす。
                half = px * _spread_bps(r) / 2 / 10000
                limit = px - half if side == "buy" else px + half
                filled = False
                for fr in rows[i + 1:]:
                    if _epoch(fr["ts"]) - t > v.fill_timeout_sec:
                        break
                    fpx = fr.get("last_price")
                    if not fpx:
                        continue
                    if (side == "buy" and fpx <= limit) or (side == "sell" and fpx >= limit):
                        filled = True
                        break
                if not filled:
                    continue  # 取り消し: 建玉は作らない
                epx = limit
            elif v.maker_entry:
                # ①指値: 実機は最良買い気配(売りなら最良売り気配)に置くので、
                # 約定すれば現値より半スプレッド有利な価格で入る。
                # (取り逃し=約21%は未モデル化。fill_timeout_sec の項を参照)
                half = px * _spread_bps(r) / 2 / 10000
                epx = px - half if side == "buy" else px + half
            else:
                cost = px * (_spread_bps(r) / 2 + SLIP_BP) / 10000
                epx = px + cost if side == "buy" else px - cost
            sign = 1 if side == "buy" else -1
            stop_pct, tgt_pct = v.stop_pct, v.target_pct
            if v.vol_stop:  # F: 荒れた銘柄ほど損切りを広く (ノイズで刈られない)
                rng5 = (max(win_prices) - min(win_prices)) / px * 100
                stop_pct = min(max(0.7 * rng5, 0.4), 1.5)
                tgt_pct = stop_pct * 2
            if v.live_sizing:
                # 探索モードは実機も100株固定。本命だけ資金とリスクから計算する
                qty = UNIT if tag == "explore" else _live_qty(v, side, px, imb, br)
                if qty <= 0:
                    continue  # 1単元に満たない = 実機も見送っている (高すぎる株など)
            else:
                qty = UNIT
            if v.max_total_exposure > 0:
                # 実機は既存の建玉総額を見て、上限を超える新規は見送る
                exposure = sum(s.pos[1] * s.pos_qty for s in states.values() if s.pos)
                if exposure + qty * px > v.max_total_exposure:
                    continue
            st.pos = (side, epx, t,
                   epx * (1 - sign * stop_pct / 100),
                   epx * (1 + sign * tgt_pct / 100),
                   epx)
            st.pos_tag = tag
            st.pos_qty = qty

    for code, st in states.items():  # 引けで強制決済
        if st.pos is not None:
            side, epx, et, _, _, _ = st.pos
            px = per_code[code][-1]["last_price"]
            if not px:
                continue
            pnl = (px - epx) * st.pos_qty if side == "buy" else (epx - px) * st.pos_qty
            trades.append(pnl)
            tags.append(st.pos_tag)

    wins = [x for x in trades if x > 0]
    hold = _HOLD_SECS
    n_exp = sum(1 for g in tags if g == "explore")
    return {
        "explore_trades": n_exp,
        "explore_pnl": sum(p for p, g in zip(trades, tags) if g == "explore"),
        "main_pnl": sum(p for p, g in zip(trades, tags) if g != "explore"),
        "hold_avg": sum(hold)/len(hold) if hold else 0.0,
        "pnl_list": list(trades),
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
            max_spread_bps=8.0, hours_filter=False, trailing_pct=0.3)  # 時間帯制限は撤廃済み

def _live(name, **over):
    return Variant(name, **{**LIVE, **over})

CUR = dict(min_range_pct=0.3, loss_cooldown_sec=1800)

VARIANTS = [
    _live("★現ライブ設定 (B+E込み)", **CUR),
    # --- 実機163取引の分析から立てた仮説 (2026-07-28) ---
    # J: 買いが全期間で大負け(-15,870円)、売りは黒字(+9,120円)
    _live("J1: 売りのみ", **CUR, side_filter="sell"),
    _live("J2: 買いのみ", **CUR, side_filter="buy"),
    # K: ストップ決済が16戦全敗 -20,420円。トレールに任せて損切りを浅くする
    _live("K1: ストップ0.25%", **CUR, stop_pct=0.25),
    _live("K2: ストップ0.2%", **CUR, stop_pct=0.2),
    _live("K3: ストップ0.3%+トレール0.25", **CUR, stop_pct=0.3, trailing_pct=0.25),
    _live("K4: 売りのみ+ストップ0.25", **CUR, side_filter="sell", stop_pct=0.25),
    _live("F: 変動連動ストップ", **CUR, vol_stop=True),
    _live("★+吸収復活", **CUR, absorption=True),
    _live("★+吸収+トレール.4", **CUR, absorption=True, trailing_pct=0.4),
    _live("★+ML0.50", **CUR, ml_veto=0.50),
    _live("★+ML0.55", **CUR, ml_veto=0.55),
    _live("★+ML0.60", **CUR, ml_veto=0.60),
    _live("探索級+ML0.55", **CUR, imbalance=0.35, surge_ratio=1.2, ml_veto=0.55),
    _live("探索級+ML0.60", **CUR, imbalance=0.35, surge_ratio=1.2, ml_veto=0.60),
    _live("G: spread12bp", **CUR, max_spread_bps=12.0),
    _live("G2: spread15bp", **CUR, max_spread_bps=15.0),
    _live("H: 量1.5倍+spread12", **CUR, max_spread_bps=12.0, surge_ratio=1.5),
    _live("I: 出来高条件なし", **CUR, volume_surge=False),
    _live("I2: 出来高1.3倍", **CUR, surge_ratio=1.3),
    _live("I3: 出来高1.2倍", **CUR, surge_ratio=1.2),
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
    days = sys.argv[1:] or sorted({p.name.split(".")[0] for p in snap_dir.glob("*.jsonl*")})
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
