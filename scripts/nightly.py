"""夜間バッチ: 日足更新 -> スクリーニングML学習(任意) -> 翌日の監視リスト生成.

usage:
  python scripts/nightly.py            # データ更新 + ランキングのみ
  python scripts/nightly.py --train    # モデルも再学習する
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from tatibana_bot.config import load_config
from tatibana_bot.data.daily import update_universe
from tatibana_bot.screening.rank import rank_universe
from tatibana_bot.screening.train import build_dataset, train_model
from tatibana_bot.screening.watchlist import save_watchlist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("nightly")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", action="store_true", help="スクリーニングモデルを再学習する")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    model_path = Path(cfg.paths.models_dir) / "screener.txt"

    # 1. 日足更新 (地合い判定用の指数も含める)
    codes = list(cfg.universe.codes)
    bars = update_universe(codes + [cfg.risk.regime_index], cfg.paths.data_dir,
                           days=cfg.screening.history_days)
    index_df = bars.pop(cfg.risk.regime_index, None)
    if index_df is None:
        logger.warning("failed to fetch regime index %s", cfg.risk.regime_index)

    # 2. 学習 (--train 時、またはモデル未作成時)
    if args.train or not model_path.exists():
        table = build_dataset(bars, cfg.screening.label_range_pct,
                              cfg.screening.min_turnover_jpy)
        metrics = train_model(table, model_path)
        logger.info("training metrics: %s", metrics)

    # 3. ランキング -> 監視リスト保存
    items = rank_universe(bars, model_path, top_n=cfg.screening.top_n,
                          min_turnover_jpy=cfg.screening.min_turnover_jpy)
    save_watchlist(items, cfg.paths.watchlist)
    logger.info("watchlist saved to %s: %s", cfg.paths.watchlist, [w.code for w in items])


if __name__ == "__main__":
    main()
