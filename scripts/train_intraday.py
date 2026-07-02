"""③短期MLモデルの学習: 収集済みの板スナップショットから学習する.

usage: python scripts/train_intraday.py 20260701 20260702 ...
       (引数なしなら data/snapshots/ にある全日付を使う)
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from tatibana_bot.config import load_config
from tatibana_bot.data.store import SnapshotRecorder
from tatibana_bot.signals.model import build_intraday_dataset, train_intraday_model

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("train_intraday")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("days", nargs="*", help="YYYYMMDD 形式の日付リスト")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    recorder = SnapshotRecorder(cfg.paths.data_dir)

    days = args.days
    if not days:
        snap_dir = Path(cfg.paths.data_dir) / "snapshots"
        days = sorted(p.stem for p in snap_dir.glob("*.jsonl"))
    if not days:
        raise SystemExit("no snapshot data — run the engine with record_snapshots: true first")

    frames = [recorder.load_day(d) for d in days]
    snapshots = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    logger.info("loaded %d snapshots from %d days", len(snapshots), len(days))

    table = build_intraday_dataset(snapshots)
    if len(table) < 1000:
        logger.warning("only %d labeled samples — model may be unreliable", len(table))
    metrics = train_intraday_model(table, Path(cfg.paths.models_dir) / "intraday.txt")
    logger.info("done: %s", metrics)


if __name__ == "__main__":
    main()
