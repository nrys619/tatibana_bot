"""Googleスプレッドシートへ日次成績を書き込む.

usage:
  python scripts/sheet_report.py --date 2026-07-09 --analysis analysis.json
  (analysis.json は 分析結果/良かった点/... のキーを持つJSON)

書き込み先はユーザーのシートに設置した Apps Script ウェブアプリ
(.env の SHEET_WEBHOOK_URL)。トレード明細と日次サマリーの2タブに追記する。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

import requests

TOKEN = "tatibana-0709"

ANALYSIS_KEYS = ["分析結果", "良かった点", "悪かった点", "気づき・課題",
                 "改善点", "次に実装できそうな点", "実装した点", "バックテスト通算"]


def load_names() -> dict:
    p = Path("data/names.json")
    return json.loads(p.read_text()) if p.exists() else {}


def build_payload(day: str, analysis: dict) -> dict:
    names = load_names()
    conn = sqlite3.connect("data/trades.sqlite3")

    trades = []
    win = lose = 0
    total = honmei = explore = 0.0
    for r in conn.execute(
        "SELECT entry_ts, code, side, quantity, entry_price, exit_ts, exit_price,"
        " pnl, exit_reason, entry_reason FROM trades"
        " WHERE entry_ts LIKE ? AND exit_ts IS NOT NULL ORDER BY id", (day + "%",)):
        is_explore = (r[9] or "").startswith("explore")
        pnl = r[7] or 0.0
        total += pnl
        win += pnl > 0
        lose += pnl < 0
        if is_explore:
            explore += pnl
        else:
            honmei += pnl
        hold_min = round((datetime.fromisoformat(r[5]) -
                          datetime.fromisoformat(r[0])).total_seconds() / 60, 1)
        trades.append([
            day, r[0][11:19], r[1], names.get(r[1], ""),
            "買い" if r[2] == "buy" else "空売り",
            "探索" if is_explore else "本命",
            r[3], r[4], r[6], round(pnl), r[8], hold_min,
        ])

    cumulative = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE pnl IS NOT NULL"
        " AND substr(exit_ts,1,10) <= ?", (day,)).fetchone()[0]
    conn.close()

    snap = Path(f"data/snapshots/{day.replace('-', '')}.jsonl")
    snap_total = sum(
        sum(1 for _ in open(p)) for p in Path("data/snapshots").glob("*.jsonl")
        if p.stem <= day.replace("-", "")
    ) if snap.parent.exists() else 0

    summary = [day, len(trades), win, lose, round(total), round(cumulative),
               round(honmei), round(explore)]
    summary += [analysis.get(k, "") for k in ANALYSIS_KEYS[:-1]]
    summary += [analysis.get("バックテスト通算", ""), snap_total]
    return {"token": TOKEN, "trades": trades, "summary": summary}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=f"{date.today():%Y-%m-%d}")
    ap.add_argument("--analysis", default=None, help="分析JSONのファイルパス")
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    url = os.environ.get("SHEET_WEBHOOK_URL")
    if not url:
        raise SystemExit("SHEET_WEBHOOK_URL が .env にありません")

    analysis = {}
    if args.analysis:
        analysis = json.loads(Path(args.analysis).read_text())

    payload = build_payload(args.date, analysis)
    res = requests.post(url, json=payload, timeout=30)
    print(f"{args.date}: trades={len(payload['trades'])}件 -> {res.text.strip()}")


if __name__ == "__main__":
    main()
