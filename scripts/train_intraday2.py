"""場中MLモデル 第2ラウンドの学習.

第1ラウンドは検証の切り方とラベル設計に問題があり、AUC 0.593 で不採用だった。
ここでは実際の決済ルール(三重障壁)をラベルにし、**日で分割して**検証する。

usage:
  python scripts/train_intraday2.py                # 全日付
  python scripts/train_intraday2.py --stride 20    # 間引きを粗く (速いが精度は落ちる)
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
from pathlib import Path

import pandas as pd

from tatibana_bot.signals.model2 import FEATURES, build_day, train_with_day_split

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("train2")

SNAP_DIR = Path("data/snapshots")


def _snap_path(day: str) -> Path:
    """圧縮済みならそちらを返す (ディスク節約で古い日は gzip されている)."""
    p = SNAP_DIR / f"{day}.jsonl"
    return p if p.exists() else SNAP_DIR / f"{day}.jsonl.gz"
OUT_DIR = Path("models")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-pct", type=float, default=0.3, help="利確幅 (トレーリング相当)")
    ap.add_argument("--stop-pct", type=float, default=0.25, help="損切り幅 (実機と同じ)")
    ap.add_argument("--window", type=int, default=300, help="先を見る本数 (秒)")
    ap.add_argument("--stride", type=int, default=10, help="行の間引き (重なり対策)")
    ap.add_argument("--valid-days", type=int, default=6, help="検証に回す末尾の日数")
    args = ap.parse_args()

    days = sorted({p.name.split(".")[0] for p in SNAP_DIR.glob("*.jsonl*")})
    if len(days) <= args.valid_days:
        raise SystemExit(f"日数が足りない ({len(days)}日)")
    logger.info("対象 %d日: %s 〜 %s", len(days), days[0], days[-1])

    tables = []
    for d in days:
        df = pd.read_json(_snap_path(d), lines=True)
        t = build_day(df, args.target_pct, args.stop_pct, args.window, args.stride)
        if not t.empty:
            t["day"] = d
            keep = [c for c in FEATURES if c in t.columns] + \
                   ["day", "code", "label_buy", "label_sell"]
            tables.append(t[keep].astype({c: "float32" for c in FEATURES if c in t.columns}))
        logger.info("  %s: %d行 -> 学習用 %d行", d, len(df), 0 if t.empty else len(t))
        del df, t
        gc.collect()

    table = pd.concat(tables, ignore_index=True)
    del tables
    gc.collect()
    for col in ("label_buy", "label_sell"):
        n = table[col].notna().sum()
        logger.info("%s: 判定できた行 %d (勝ち率 %.1f%%)", col, n, table[col].mean() * 100)

    OUT_DIR.mkdir(exist_ok=True)
    report = {"設定": vars(args), "総行数": len(table)}
    for col, name in (("label_buy", "buy"), ("label_sell", "sell")):
        logger.info("=== %s を学習 ===", name)
        m = train_with_day_split(table, OUT_DIR / f"intraday2_{name}_candidate.txt",
                                 col, valid_days=args.valid_days)
        report[name] = m
        logger.info("%s: AUC=%.4f (学習%d行 / 検証%d行)", name, m["auc"],
                    m["n_train"], m["n_valid"])
        logger.info("  絞らない場合の勝率: %.1f%%", m["baseline_win_rate"] * 100)
        for th, g in m["gates"].items():
            logger.info("  閾値%.2f: 通過%.0f%% -> 勝率%.1f%%",
                        th, g["通過率"] * 100, g["勝率"] * 100)
        logger.info("  効いた特徴量: %s", list(m["importance"])[:5])

    path = OUT_DIR / "round2_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    logger.info("レポート: %s", path)
    logger.info("※モデルは *_candidate.txt。engine が読むのは models/intraday.txt なので、"
                "検証で採用と判断するまで本番には効かない")


if __name__ == "__main__":
    main()
