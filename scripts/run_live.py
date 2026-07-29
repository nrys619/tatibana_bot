"""場中エンジンの起動.

usage:
  python scripts/run_live.py               # config の mode (paper/live) で起動
  python scripts/run_live.py --mode paper  # モードを上書き
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from tatibana_bot.api import MarketDataClient, OrderGateway
from tatibana_bot.api.factory import create_session
from tatibana_bot.config import load_config
from tatibana_bot.data.daily import load_universe
from tatibana_bot.data.store import SnapshotRecorder, TradeLog
from tatibana_bot.engine.engine import LiveEngine
from tatibana_bot.engine.executor import Executor
from tatibana_bot.engine.reconcile import reconcile_startup
from tatibana_bot.risk.limits import LimitParams, RiskLimits
from tatibana_bot.risk.regime import classify_regime
from tatibana_bot.risk.sizing import SizingParams
from tatibana_bot.screening.watchlist import load_watchlist
from tatibana_bot.signals.model import load_scorer
from tatibana_bot.signals.strategy import MicroStrategy, StrategyParams

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_live")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["paper", "live"], default=None)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    mode = args.mode or cfg.engine.mode

    watchlist = load_watchlist(cfg.paths.watchlist)
    record_codes = []
    _rl = Path(cfg.paths.data_dir) / "recordlist.json"
    if _rl.exists():
        import json as _json
        record_codes = _json.loads(_rl.read_text())
        logger.info("record-only codes: %d銘柄", len(record_codes))
    if not watchlist:
        raise SystemExit("watchlist is empty — run scripts/nightly.py first")

    # 地合い判定 (取得済みの指数日足を使用)
    regime_mult = 1.0
    allow_buy = True
    index_bars = load_universe([cfg.risk.regime_index], cfg.paths.data_dir)
    index_df = index_bars.get(cfg.risk.regime_index)
    if index_df is not None:
        state = classify_regime(index_df)
        regime_mult = state.risk_multiplier
        logger.info("regime=%s vol=%.2f trend=%.2f%% -> risk x%.2f",
                    state.regime.value, state.realized_vol,
                    state.trend_strength, regime_mult)
        # J: 実機163取引で買いは全期間-15,870円/売りは+9,120円。
        # 地合いが下向き(MA5<MA20)の日は買いエントリーを止める。
        # 上げ相場に転じれば自動で買いが復活する。
        if cfg.signals.get("skip_buy_when_market_falling", False):
            allow_buy = state.trend_strength >= 0
            if not allow_buy:
                logger.info("下げ相場のため今日は買いエントリーを停止 (売りのみ)")
    else:
        logger.warning("no regime index data — using multiplier 1.0")

    # 短期MLモデルがあれば有効化 (無ければルールベースのみ)
    scorer = load_scorer(Path(cfg.paths.models_dir) / "intraday.txt")
    logger.info("intraday ML scorer: %s", "enabled" if scorer else "disabled (rule-based only)")

    strategy = MicroStrategy(
        StrategyParams(
            imbalance_entry=cfg.signals.imbalance_entry,
            tape_ratio_entry=cfg.signals.tape_ratio_entry,
            max_spread_bps=cfg.signals.max_spread_bps,
            target_pct=cfg.signals.target_pct,
            stop_pct=cfg.signals.stop_pct,
            trend_filter=cfg.signals.get("trend_filter", False),
            volume_surge=cfg.signals.get("volume_surge", False),
            absorption=cfg.signals.get("absorption", False),
            breakout_only=cfg.signals.get("breakout_only", False),
            min_range_pct=float(cfg.signals.get("min_range_pct", 0.0)),
        ),
        ml_scorer=scorer,
    )

    # 場中スキャン: 松井デイトレ適性ランキングで監視リストを定期入れ替え
    watch_updater = None
    scan_interval = 300.0
    scan_cfg = cfg.engine.get("intraday_scan")
    if scan_cfg is not None and scan_cfg.get("enabled", False):
        from datetime import datetime as _dt, time as _time

        from tatibana_bot.data.matsui import scan_daytrade_watchlist
        from tatibana_bot.risk.sizing import UNIT_SHARES

        max_price = cfg.risk.max_position_value / UNIT_SHARES
        scan_interval = float(scan_cfg.get("interval_sec", 300))

        def watch_updater():
            return scan_daytrade_watchlist(
                top_n=cfg.screening.top_n,
                max_price_jpy=max_price,
                min_turnover_jpy=cfg.screening.min_turnover_jpy,
                afternoon=_dt.now().time() >= _time(12, 0),
            )

        logger.info("intraday scan: enabled (every %.0fs)", scan_interval)

    # ③参加時間帯 (config: signals.entry_windows)
    entry_windows = None
    raw_windows = cfg.signals.get("entry_windows")
    if raw_windows:
        from datetime import time as _t
        entry_windows = [
            (_t(*map(int, s.split(":"))), _t(*map(int, e.split(":"))))
            for s, e in raw_windows
        ]
        logger.info("entry windows: %s", raw_windows)

    # 探索モード (緩い条件の試し玉)
    explore_strategy = None
    exp_cfg = cfg.signals.get("explore")
    if exp_cfg is not None and exp_cfg.get("enabled", False):
        explore_strategy = MicroStrategy(
            StrategyParams(
                imbalance_entry=float(exp_cfg.get("imbalance_entry", 0.35)),
                tape_ratio_entry=cfg.signals.tape_ratio_entry,
                max_spread_bps=cfg.signals.max_spread_bps,
                target_pct=cfg.signals.target_pct,
                stop_pct=cfg.signals.stop_pct,
                trend_filter=True,
                volume_surge=True,
                surge_ratio=float(exp_cfg.get("surge_ratio", 1.2)),
                min_range_pct=float(cfg.signals.get("min_range_pct", 0.0)),
            ),
            ml_scorer=scorer,
        )
        logger.info("explore mode: enabled (imb>=%.2f surge>=%.1fx, min size)",
                    float(exp_cfg.get("imbalance_entry", 0.35)),
                    float(exp_cfg.get("surge_ratio", 1.2)))

    session = create_session(cfg)

    with session:
        # 起動時照合: 前回落ちた等で宙に浮いた建玉があれば引き取り、
        # 実口座に無い未決済は帳簿を正す (2026-07-17 と 07-28 に実際に事故った)
        trade_log = TradeLog(Path(cfg.paths.data_dir) / "trades.sqlite3")
        adopted = []
        if mode == "live":
            adopted = reconcile_startup(
                OrderGateway(session), trade_log,
                stop_pct=cfg.signals.stop_pct, target_pct=cfg.signals.target_pct,
                max_hold_sec=cfg.signals.max_hold_sec,
                trailing_pct=float(cfg.signals.get("trailing_pct", 0.0)))
            if adopted:
                logger.warning("宙に浮いた建玉を %d件 引き取った (エンジンが決済する)",
                               len(adopted))

        executor = Executor(
            mode,
            OrderGateway(session) if mode == "live" else None,
            maker_entry=cfg.engine.get("maker_entry", False),
            fill_timeout_sec=float(cfg.engine.get("fill_timeout_sec", 8)),
            trailing_pct=float(cfg.signals.get("trailing_pct", 0.0)),
        )
        engine = LiveEngine(
            market_data=MarketDataClient(session),
            executor=executor,
            strategy=strategy,
            sizing=SizingParams(
                equity_jpy=cfg.risk.equity_jpy,
                risk_per_trade=cfg.risk.risk_per_trade,
                max_position_value=cfg.risk.max_position_value,
            ),
            limits=RiskLimits(LimitParams(
                equity_jpy=cfg.risk.equity_jpy,
                daily_loss_limit=cfg.risk.daily_loss_limit,
                max_consecutive_losses=cfg.risk.max_consecutive_losses,
            )),
            trade_log=trade_log,
            watchlist=watchlist,
            regime_multiplier=regime_mult,
            max_hold_sec=cfg.signals.max_hold_sec,
            poll_interval_sec=cfg.engine.poll_interval_sec,
            recorder=SnapshotRecorder(cfg.paths.data_dir) if cfg.engine.record_snapshots else None,
            watch_updater=watch_updater,
            scan_interval_sec=scan_interval,
            max_watch=cfg.screening.top_n,
            max_total_exposure=cfg.risk.get("max_total_exposure"),
            entry_windows=entry_windows,
            loss_cooldown_sec=float(cfg.signals.get("loss_cooldown_sec", 0)),
            explore_strategy=explore_strategy,
            price_shock_bps=float(cfg.risk.get("price_shock_bps", 300)),
            record_codes=record_codes,
            adopted=adopted,
            allow_buy=allow_buy,
            slippage_bps=float(cfg.get("costs", {}).get("slippage_bps", 2.0)),
            commission_jpy=float(cfg.get("costs", {}).get("commission_jpy", 0.0)),
            explore_daily_loss_cap=float((exp_cfg.get("daily_loss_cap", 5000)
                                          if exp_cfg is not None else 5000)),
        )
        engine.run()


if __name__ == "__main__":
    main()
