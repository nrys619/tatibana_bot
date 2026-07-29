"""その日の成績を集計し、分析を自動生成してスプレッドシートに記入する.

Claude のリマインダーは7日で失効するため、日次記入をそれに任せると必ず穴があく。
このスクリプトを launchd (平日15:43) から回して、**人が居なくても毎日必ず記入される**
状態にする。Claude はこの上に「気づき」を足す役目に回る。

usage:
  python scripts/daily_report.py                    # 今日
  python scripts/daily_report.py --date 2026-07-28  # 指定日
  python scripts/daily_report.py --force            # 記入済みでも再送する
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

ANALYSES_DIR = Path("data/analyses")   # /tmp は再起動で消えるためリポジトリ配下に置く
POSTED_DIR = ANALYSES_DIR / ".posted"  # 二重記入を防ぐ印
DB = "data/trades.sqlite3"


def _names() -> dict:
    p = Path("data/names.json")
    return json.loads(p.read_text()) if p.exists() else {}


def _jp(code: str, names: dict) -> str:
    return names.get(code, code)


def build_analysis(day: str) -> dict:
    """その日の数字から分析文を組み立てる (人手が入らなくても最低限は埋まる)."""
    names = _names()
    conn = sqlite3.connect(DB)
    q = conn.execute

    n, total, wins, gross, cost = q("""SELECT COUNT(*),
        COALESCE(SUM(COALESCE(pnl_net, pnl)),0),
        SUM(CASE WHEN COALESCE(pnl_net, pnl)>0 THEN 1 ELSE 0 END),
        COALESCE(SUM(pnl),0), COALESCE(SUM(cost),0) FROM trades
        WHERE entry_ts LIKE ?""", (day + "%",)).fetchone()
    if not n:
        conn.close()
        return {"分析結果": "取引なし。合図が出なかったか、地合いフィルタで見送った。"}

    def agg(cond: str) -> tuple[int, float]:
        r = q(f"SELECT COUNT(*), COALESCE(SUM(COALESCE(pnl_net, pnl)),0) FROM trades "
              f"WHERE entry_ts LIKE ? AND {cond}", (day + "%",)).fetchone()
        return r[0], r[1]

    bn, bp = agg("side='buy'")
    sn, sp = agg("side='sell'")
    en, ep = agg("entry_reason LIKE 'explore%'")
    mn, mp = agg("entry_reason NOT LIKE 'explore%'")

    exits = list(q("""SELECT exit_reason, COUNT(*), COALESCE(SUM(COALESCE(pnl_net, pnl)),0) FROM trades
        WHERE entry_ts LIKE ? GROUP BY 1 ORDER BY 3""", (day + "%",)))
    best = list(q("""SELECT code, SUM(COALESCE(pnl_net, pnl)) FROM trades WHERE entry_ts LIKE ?
        GROUP BY code ORDER BY 2 DESC LIMIT 2""", (day + "%",)))
    worst = list(q("""SELECT code, SUM(COALESCE(pnl_net, pnl)) FROM trades WHERE entry_ts LIKE ?
        GROUP BY code ORDER BY 2 LIMIT 2""", (day + "%",)))
    cum = q("""SELECT COALESCE(SUM(COALESCE(pnl_net, pnl)),0) FROM trades
        WHERE substr(entry_ts,1,10) <= ?""", (day,)).fetchone()[0]
    open_n = q("SELECT COUNT(*) FROM trades WHERE entry_ts LIKE ? AND exit_ts IS NULL",
               (day + "%",)).fetchone()[0]
    conn.close()

    wr = wins / n * 100
    ex_txt = " / ".join(f"{r[0]}{r[1]}件{r[2]:+.0f}円" for r in exits)
    best_txt = "、".join(f"{_jp(c, names)}{p:+.0f}円" for c, p in best)
    worst_txt = "、".join(f"{_jp(c, names)}{p:+.0f}円" for c, p in worst)

    good, bad, note = [], [], []
    if sp > 0:
        good.append(f"売り{sn}件で{sp:+.0f}円")
    if bp > 0:
        good.append(f"買い{bn}件で{bp:+.0f}円")
    if best and best[0][1] > 0:
        good.append(f"最も稼いだのは{best_txt}")
    if bp < 0:
        bad.append(f"買い{bn}件が{bp:+.0f}円")
    if sp < 0:
        bad.append(f"売り{sn}件が{sp:+.0f}円")
    if worst and worst[0][1] < 0:
        bad.append(f"最も損したのは{worst_txt}")
    stop = next((r for r in exits if r[0] == "stop"), None)
    if stop:
        note.append(f"ストップ決済が{stop[1]}件で{stop[2]:+.0f}円。"
                    f"実機ではストップは負け続けているため要監視")
    if open_n:
        note.append(f"★未決済が{open_n}件残っている。持ち越し事故の可能性があるので要確認")
    if ep and mn and (ep > 0) != (mp > 0):
        note.append(f"本命{mn}件{mp:+.0f}円 と 探索{en}件{ep:+.0f}円 で符号が逆")

    return {
        "分析結果": (f"{n}取引で{total:+.0f}円 (勝率{wr:.0f}%)。"
                     f"買い{bn}件{bp:+.0f}円 / 売り{sn}件{sp:+.0f}円、"
                     f"本命{mn}件{mp:+.0f}円 / 探索{en}件{ep:+.0f}円。"
                     f"決済内訳: {ex_txt}。通算{cum:+.0f}円。"
                     f"(売買コスト{cost:,.0f}円を差し引いた実質値。コスト抜きなら{gross:+.0f}円)"),
        "良かった点": "、".join(good) or "特になし",
        "悪かった点": "、".join(bad) or "大きな損失なし",
        "気づき・課題": "。".join(note) or "特記事項なし",
        "改善点": "(自動生成。Claudeが後から追記)",
        "次に実装できそうな点": "(自動生成。Claudeが後から追記)",
        "実装した点": "(自動生成。Claudeが後から追記)",
        "バックテスト通算": "(自動生成。Claudeが後から追記)",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=f"{date.today():%Y-%m-%d}")
    ap.add_argument("--force", action="store_true", help="記入済みでも再送する")
    ap.add_argument("--catchup", type=int, metavar="N",
                    help="直近N日のうち未記入の日をまとめて記入する")
    args = ap.parse_args()

    if args.catchup:
        # launchdの時報はMacがスリープしていると鳴らないことがある。
        # 取りこぼした日を後から拾うための追いつき処理。
        POSTED_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB)
        recent = [r[0] for r in conn.execute(
            "SELECT DISTINCT substr(entry_ts,1,10) FROM trades ORDER BY 1 DESC LIMIT ?",
            (args.catchup,))]
        conn.close()
        missing = [d for d in sorted(recent) if not (POSTED_DIR / d).exists()]
        if not missing:
            print(f"追いつき: 直近{args.catchup}日はすべて記入済み")
            return
        print(f"追いつき: 未記入 {missing}")
        for d in missing:
            subprocess.run([sys.executable, __file__, "--date", d], check=False)
        return

    day = args.date

    ANALYSES_DIR.mkdir(parents=True, exist_ok=True)
    POSTED_DIR.mkdir(parents=True, exist_ok=True)
    marker = POSTED_DIR / day

    if marker.exists() and not args.force:
        print(f"{day}: 記入済み ({marker})。再送するなら --force")
        return

    # 数字から作る欄は毎回作り直し、Claudeが手で書いた欄だけ引き継ぐ。
    # (両方を無条件に上書きすると、古い自動生成文が新しい計算を潰してしまう)
    MANUAL_KEYS = ("改善点", "次に実装できそうな点", "実装した点", "バックテスト通算")
    path = ANALYSES_DIR / f"{day}.json"
    analysis = build_analysis(day)
    if path.exists():
        prev = json.loads(path.read_text())
        for k in MANUAL_KEYS:
            v = prev.get(k, "")
            if v and not v.startswith("(自動生成"):
                analysis[k] = v
    path.write_text(json.dumps(analysis, ensure_ascii=False, indent=1))

    res = subprocess.run(
        [sys.executable, "scripts/sheet_report.py", "--date", day, "--analysis", str(path)],
        capture_output=True, text=True)
    print(res.stdout.strip() or res.stderr.strip())
    if res.returncode == 0 and "OK" in res.stdout:
        marker.write_text(f"{day}\n")
    else:
        raise SystemExit(f"{day}: 記入に失敗した (再実行が必要)")


if __name__ == "__main__":
    main()
