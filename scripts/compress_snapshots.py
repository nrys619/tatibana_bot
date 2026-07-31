"""古い板スナップショットをgzip圧縮する (ディスクを埋めないため).

板の録画は1日約180MB。VPSのSSDは50GBなので、無圧縮だと約9ヶ月で満杯になる。
JSONL は gzip で約1/10 になるので、当日分以外を圧縮して回す。

SnapshotRecorder.load_day / pandas はどちらも .gz をそのまま読めるので、
学習やバックテストの側は変更不要。

usage:
  python scripts/compress_snapshots.py            # 昨日以前を圧縮
  python scripts/compress_snapshots.py --dry-run
  python scripts/compress_snapshots.py --purge-days 180   # 半年より古いものは削除
"""

from __future__ import annotations

import argparse
import gzip
import logging
import shutil
from datetime import date, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("compress")

SNAP_DIR = Path("data/snapshots")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--purge-days", type=int, default=0,
                    help="この日数より古い圧縮済みファイルを削除する (0=削除しない)")
    args = ap.parse_args()

    today = date.today().strftime("%Y%m%d")
    saved = 0
    for p in sorted(SNAP_DIR.glob("*.jsonl")):
        if p.stem >= today:
            continue                      # 当日分はエンジンが書き込み中
        gz = p.with_suffix(".jsonl.gz")
        if gz.exists():
            continue
        before = p.stat().st_size
        if args.dry_run:
            logger.info("圧縮対象: %s (%.0fMB)", p.name, before / 1e6)
            continue
        with p.open("rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo)
        after = gz.stat().st_size
        p.unlink()
        saved += before - after
        logger.info("%s: %.0fMB -> %.0fMB (%.0f%%削減)",
                    p.name, before / 1e6, after / 1e6, (1 - after / before) * 100)

    if args.purge_days > 0:
        cutoff = (date.today() - timedelta(days=args.purge_days)).strftime("%Y%m%d")
        for p in sorted(SNAP_DIR.glob("*.jsonl.gz")):
            if p.name[:8] < cutoff:
                logger.info("古いので削除: %s", p.name)
                if not args.dry_run:
                    p.unlink()

    if saved:
        logger.info("合計 %.1fGB を節約", saved / 1e9)
    used = sum(f.stat().st_size for f in SNAP_DIR.glob("*")) / 1e9
    logger.info("スナップショット合計: %.2fGB", used)


if __name__ == "__main__":
    main()
