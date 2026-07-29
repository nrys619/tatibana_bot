"""「下げ相場では買わない」判断が正しかったのかを採点する.

見送った買いは shadow_trades に「もし入っていたら」として記録されている。
実際に取った売りの成績と並べて、見送りが得だったのか損だったのかを出す。

usage:
  python scripts/shadow_report.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB = "data/trades.sqlite3"


def _names() -> dict:
    p = Path("data/names.json")
    return json.loads(p.read_text()) if p.exists() else {}


def main() -> None:
    names = _names()
    conn = sqlite3.connect(DB)
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                        " AND name='shadow_trades'").fetchone():
        print('shadow_trades テーブルがまだありません '
              '(エンジンを一度起動すれば作られます)')
        return

    n_open = conn.execute(
        "SELECT COUNT(*) FROM shadow_trades WHERE exit_ts IS NULL").fetchone()[0]
    rows = list(conn.execute("""
        SELECT substr(entry_ts,1,10) AS d, COUNT(*),
               COALESCE(SUM(COALESCE(pnl_net, pnl)),0)
        FROM shadow_trades WHERE exit_ts IS NOT NULL GROUP BY d ORDER BY d"""))
    if not rows:
        print("見送った買いの記録はまだありません "
              "(下げ相場で買い合図が出た日が来ると溜まります)")
        if n_open:
            print(f"  ※未決済の影の建玉が {n_open}件 (場中なら正常)")
        return

    print(f"{'日付':<12}{'見送った買い':>12}{'もし入っていたら':>18}{'実際の売り':>14}")
    print("-" * 58)
    tot_shadow = tot_real = 0.0
    for day, cnt, pnl in rows:
        real = conn.execute("""
            SELECT COALESCE(SUM(COALESCE(pnl_net, pnl)),0) FROM trades
            WHERE entry_ts LIKE ? AND side='sell'""", (day + "%",)).fetchone()[0]
        tot_shadow += pnl
        tot_real += real
        print(f"{day:<12}{cnt:>9}件{pnl:>+16.0f}円{real:>+12.0f}円")
    print("-" * 58)
    print(f"{'合計':<12}{'':>10}{tot_shadow:>+16.0f}円{tot_real:>+12.0f}円")
    print()

    if tot_shadow < 0:
        print(f"判定: 見送って正解。買っていたら {tot_shadow:+,.0f}円 だった "
              f"(= {-tot_shadow:,.0f}円 の損失を回避)")
    else:
        print(f"判定: **見送りは損だった**。買っていたら {tot_shadow:+,.0f}円 取れていた。"
              f"買い停止の撤回を検討すべき")

    print("\n=== 見送った買いの銘柄別 ===")
    for code, cnt, pnl in conn.execute("""
            SELECT code, COUNT(*), COALESCE(SUM(COALESCE(pnl_net, pnl)),0)
            FROM shadow_trades WHERE exit_ts IS NOT NULL
            GROUP BY code ORDER BY 3"""):
        print(f"  {code} {names.get(code, '?')}: {cnt}件 {pnl:+.0f}円")
    if n_open:
        print(f"\n未決済の影の建玉: {n_open}件 (場中なら正常)")
    conn.close()


if __name__ == "__main__":
    main()
