"""場中エンジンの起動.

usage:
  python scripts/run_live.py               # config の mode (paper/live) で起動
  python scripts/run_live.py --mode paper  # モードを上書き
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from tatibana_bot.api import MarketDataClient, OrderGateway, TachibanaSession
from tatibana_bot.config import env, load_config
from tatibana_bot.data.daily import load_universe
from tatibana_bot.data.store import SnapshotRecorder, TradeLog
from tatibana_bot.engine.engine import LiveEngine
from tatibana_bot.engine.executor import Executor
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
    if not watchlist:
        raise SystemExit("watchlist is empty — run scripts/nightly.py first")

    # 地合い判定 (取得済みの指数日足を使用)
    regime_mult = 1.0
    index_bars = load_universe([cfg.risk.regime_index], cfg.paths.data_dir)
    index_df = index_bars.get(cfg.risk.regime_index)
    if index_df is not None:
        state = classify_regime(index_df)
        regime_mult = state.risk_multiplier
        logger.info("regime=%s vol=%.2f trend=%.2f%% -> risk x%.2f",
                    state.regime.value, state.realized_vol,
                    state.trend_strength, regime_mult)
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
        ),
        ml_scorer=scorer,
    )

    base_url = cfg.api.base_urls.get(cfg.api.env)
    session = TachibanaSession(
        base_url=base_url,
        version=cfg.api.version,
        user_id=env("TACHIBANA_USER_ID"),
        password=env("TACHIBANA_PASSWORD"),
        timeout_sec=cfg.api.timeout_sec,
    )

    with session:
        executor = Executor(mode, OrderGateway(session) if mode == "live" else None)
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
            trade_log=TradeLog(Path(cfg.paths.data_dir) / "trades.sqlite3"),
            watchlist=watchlist,
            regime_multiplier=regime_mult,
            max_hold_sec=cfg.signals.max_hold_sec,
            poll_interval_sec=cfg.engine.poll_interval_sec,
            recorder=SnapshotRecorder(cfg.paths.data_dir) if cfg.engine.record_snapshots else None,
        )
        engine.run()


if __name__ == "__main__":
    main()
