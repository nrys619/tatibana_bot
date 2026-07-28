"""シミュレータが実機をどれだけ再現できているかを採点する.

バックテストが黒字なのに実機が赤字、という乖離の原因を潰すための物差し。
同じ日について「シミュの日次損益」と「実機の日次損益」を並べ、相関・方向一致率・
取引数の比を出す。**この点数が上がるまで、どのバックテスト結果も信用しない。**

usage:
  python scripts/validate_sim.py              # 実機ログがある日すべて
  python scripts/validate_sim.py 20260722 ...  # 日付指定
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from backtest import Variant, _load_day, run_variant  # noqa: E402
from watchlog import load_watch_timeline  # noqa: E402

DB = "data/trades.sqlite3"
SNAP_DIR = Path("data/snapshots")

# 実機の設定をできるだけ忠実に写したバリアント (2026-07-28時点)
LIVE_FAITHFUL = dict(
    trend_filter=True, volume_surge=True, imbalance=0.4, maker_entry=True,
    max_spread_bps=8.0, hours_filter=False, trailing_pct=0.3,
    min_range_pct=0.3, loss_cooldown_sec=0,
    explore=True, explore_imbalance=0.35, explore_surge=1.2, explore_max_price=3000.0,
    respect_watchlist=True,
    live_sizing=True, regime_mult=0.30,  # 実機ログ上7/9〜7/28は全日x0.30
    anomaly=True, price_shock_bps=300.0,  # 実機configと同じ
    max_total_exposure=6_000_000,         # 実機の建玉総額上限
    gross_pnl=True,   # 実機DBは (決済価格-建値)x株数 しか記録しておらずコストを引いていない。
                      # 採点を公平にするため合わせる (実機の真の損益はこれより悪い)
    # fill_timeout_sec=15.0 は不採用。「待ち時間内に価格が指値に届いたら約定」という
    # モデルは、買い指値が下落時にしか刺さらない = 不利な入り方だけを選ぶ偏りを生み、
    # 相関が +0.11 -> -0.22 に悪化した。実際の指値は板の順番待ちで、価格が下がらなくても
    # 売り手が来れば約定する。約定モデルを入れるならその点を直してから。
)

# 採点の履歴 (実機との相関) — 何が効いて何が効かなかったかの記録
SCORE_LOG = """
2026-07-28  -0.12  出発点 (探索モードなし・記録専用銘柄まで売買・監視外も売買)
2026-07-28  +0.11  探索モード追加 + その時刻の監視リストに限定
2026-07-28  -0.22  上記 + 指値約定モデル → 悪化したので不採用
2026-07-28  +0.27  探索+監視リスト+実機と同じ建玉サイズ計算 (高すぎる株の見送りを再現)
2026-07-28  +0.29  上記 + 監視ログが無い日を採点から除外 (13日で採点)
2026-07-28  +0.51  上記 + 実機の異常検知(板の急減/急変動)を移植、スプレッドinfバグ修正
                   方向一致 10/13日(77%)、取引数の比 1.01倍まで一致
"""


def live_daily() -> dict[str, tuple[int, float, int, float]]:
    """day -> (取引数, 損益, 探索の取引数, 探索の損益)."""
    conn = sqlite3.connect(DB)
    out = {}
    for day, n, pnl, en, epnl in conn.execute("""
        SELECT substr(entry_ts, 1, 10), COUNT(*), COALESCE(SUM(pnl), 0),
               SUM(CASE WHEN entry_reason LIKE 'explore%' THEN 1 ELSE 0 END),
               COALESCE(SUM(CASE WHEN entry_reason LIKE 'explore%' THEN pnl ELSE 0 END), 0)
        FROM trades GROUP BY 1"""):
        out[day.replace("-", "")] = (n, pnl, en, epnl)
    conn.close()
    return out


def _corr(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    vx = sum((a - mx) ** 2 for a in xs) ** 0.5
    vy = sum((b - my) ** 2 for b in ys) ** 0.5
    return cov / (vx * vy) if vx and vy else 0.0


def main() -> None:
    live = live_daily()
    days = sys.argv[1:] or sorted(d for d in (p.stem for p in SNAP_DIR.glob("*.jsonl"))
                                  if d in live)
    if not days:
        print("比較できる日がありません (実機の取引記録とスナップショットの両方が要る)")
        return

    timeline = load_watch_timeline()
    # 監視リストの履歴が無い日 (engine.log 以前) はシミュが構造的に何も売買できない。
    # 採点対象から外さないと、実力とは無関係に相関が下がる。
    skipped = [d for d in days if not timeline.all_codes(f"{d[:4]}-{d[4:6]}-{d[6:]}")]
    days = [d for d in days if d not in skipped]
    if skipped:
        print(f"(監視ログが無いため採点から除外: {', '.join(skipped)})")
    v = Variant("実機忠実", **LIVE_FAITHFUL)

    print(f"{'日付':<10}{'シミュ':>10}{'実機':>10}{'方向':>6}"
          f"{'シミュ件数':>10}{'実機件数':>9}{'シミュ探索率':>12}{'実機探索率':>11}")
    print("-" * 80)
    sim_pnl, live_pnl = [], []
    tot_sim_n = tot_live_n = 0
    for d in days:
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        res = run_variant(v, _load_day(SNAP_DIR / f"{d}.jsonl"), day=iso, timeline=timeline)
        ln, lp, len_, _ = live[d]
        sim_pnl.append(res["total"])
        live_pnl.append(lp)
        tot_sim_n += res["trades"]
        tot_live_n += ln
        agree = "○" if (res["total"] > 0) == (lp > 0) else "×"
        sr = res["explore_trades"] / res["trades"] if res["trades"] else 0.0
        lr = len_ / ln if ln else 0.0
        print(f"{iso:<10}{res['total']:>+10.0f}{lp:>+10.0f}{agree:>6}"
              f"{res['trades']:>10}{ln:>9}{sr:>11.0%}{lr:>11.0%}")

    n = len(days)
    agree_n = sum(1 for a, b in zip(sim_pnl, live_pnl) if (a > 0) == (b > 0))
    corr = _corr(sim_pnl, live_pnl)
    print("-" * 80)
    print(f"合計       {sum(sim_pnl):>+10.0f}{sum(live_pnl):>+10.0f}"
          f"{'':>6}{tot_sim_n:>10}{tot_live_n:>9}")
    print()
    print(f"【採点】 相関係数 {corr:+.2f} / 方向一致 {agree_n}/{n}日 "
          f"({agree_n / n:.0%}) / 取引数の比 {tot_sim_n / max(tot_live_n, 1):.2f}倍")
    if corr >= 0.7:
        print("合格: シミュは実機をおおむね再現できている")
    else:
        print("不合格: 相関0.7未満。この状態のバックテスト結果は信用できない")


if __name__ == "__main__":
    main()
