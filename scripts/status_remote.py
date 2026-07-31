"""VPS上のボットの状態をまとめて1枚のテキストにする.

このスクリプトは**VPS側で**実行される (Mac の状態.command が ssh 経由で呼ぶ)。
Mac にはもうエンジンもデータも無いので、ローカルを見ても意味がない。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import date, datetime
from pathlib import Path

DB = "data/trades.sqlite3"


def _names() -> dict:
    p = Path("data/names.json")
    return json.loads(p.read_text()) if p.exists() else {}


def _systemd(unit: str) -> str:
    r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
    return r.stdout.strip()


def _latest_prices(codes: set[str]) -> dict[str, float]:
    """今日の板記録の末尾から各銘柄の最新値を拾う (含み損益の計算用)."""
    path = Path(f"data/snapshots/{date.today():%Y%m%d}.jsonl")
    out: dict[str, float] = {}
    if not path.exists() or not codes:
        return out
    try:
        # 末尾だけ読めば足りる (ファイルは数百MBになりうる)
        tail = subprocess.run(["tail", "-n", "20000", str(path)],
                              capture_output=True, text=True).stdout
        for line in tail.splitlines():
            r = json.loads(line)
            if r.get("code") in codes and r.get("last_price"):
                out[r["code"]] = r["last_price"]
    except Exception:
        pass
    return out


def main() -> None:
    names = _names()
    now = datetime.now()
    print("=" * 42, flush=True)
    print("   🤖 tatibana_bot の状態 (VPS)", flush=True)
    print(f"   {now:%Y-%m-%d %H:%M} 現在", flush=True)
    print("=" * 42, flush=True)
    print(flush=True)

    # --- 見張り番が上げた警報 (最優先で見せる) ---
    alert = Path("logs/ALERT.txt")
    if alert.exists():
        print("🚨 " + "=" * 38)
        print(alert.read_text().strip()[:600])
        print("🚨 " + "=" * 38)
        print()

    # --- 稼働状況 ---
    state = _systemd("tatibana-engine.service")
    hhmm, dow = now.strftime("%H%M"), now.isoweekday()
    if state == "active":
        line = subprocess.run(
            ["bash", "-c", "grep 'engine start' logs/engine.log 2>/dev/null | tail -1"],
            capture_output=True, text=True).stdout
        print("✅ 稼働中")
        if "watchlist=" in line:
            codes = line.split("watchlist=[")[1][:90]
            print(f"   監視: {codes}...")
        buy = subprocess.run(
            ["bash", "-c", f"grep '^{now:%Y-%m-%d}' logs/engine.log | grep -c '買いエントリーを停止'"],
            capture_output=True, text=True).stdout.strip()
        if buy and buy != "0":
            print("   ⚠️ 下げ相場のため今日は売りのみ")
    elif dow >= 6:
        print("💤 停止中 (週末・市場休み)")
    elif hhmm < "0853" or hhmm > "1530":
        print("💤 停止中 (取引時間外。平日8:53に自動起動します)")
    else:
        print("❌ 停止中 — 取引時間内なのに動いていません!")
        print("   Claudeに「ボット起動して」と伝えてください")

    # --- 次の予定 ---
    t = subprocess.run(
        ["bash", "-c", "systemctl list-timers 'tatibana-*' --no-pager 2>/dev/null "
                       "| awk 'NR==2{print $1,$2,$3\" \"$4}'"],
        capture_output=True, text=True).stdout.strip()
    if t:
        print(f"   次の自動処理: {t}")
    tries = Path("logs/.watchdog_tries")
    if tries.exists() and tries.read_text().strip() not in ("", "0"):
        print(f"   ⚠️ 見張り番が今日 {tries.read_text().strip()}回 起動し直しました")

    # --- 今日の成績 ---
    conn = sqlite3.connect(DB)
    q = conn.execute
    today = date.today().isoformat()
    n, net, gross, wins = q("""SELECT COUNT(*), COALESCE(SUM(COALESCE(pnl_net,pnl)),0),
        COALESCE(SUM(pnl),0), SUM(CASE WHEN COALESCE(pnl_net,pnl)>0 THEN 1 ELSE 0 END)
        FROM trades WHERE entry_ts LIKE ?""", (today + "%",)).fetchone()
    print()
    print("-" * 42)
    if n:
        print(f"📊 今日: {n}取引  実質 {net:+,.0f}円  (勝ち{wins})")
        print(f"   ※手数料込みの実額。手数料抜きなら {gross:+,.0f}円")
        for r in q("""SELECT entry_ts, code, side, exit_ts, COALESCE(pnl_net,pnl), exit_reason
                      FROM trades WHERE entry_ts LIKE ? ORDER BY entry_ts""", (today + "%",)):
            ets, code, side, xts, pnl, why = r
            mark = "買" if side == "buy" else "売"
            st = f"{pnl:+,.0f}円 ({why})" if xts else "★保有中"
            print(f"   {ets[11:16]} {mark} {code} {names.get(code,'')} {st}")
    else:
        print("📊 今日: まだ取引なし")

    # --- 保有中 ---
    held = list(q("""SELECT code, side, quantity, entry_price FROM trades
                     WHERE exit_ts IS NULL"""))
    if held:
        px = _latest_prices({h[0] for h in held})
        print()
        print("💼 保有中:")
        for code, side, qty, ep in held:
            cur = px.get(code)
            if cur:
                pnl = (cur - ep) * qty * (1 if side == "buy" else -1)
                print(f"   {code} {names.get(code,'')} {qty}株 @{ep:,.0f} "
                      f"-> {cur:,.0f} 含み{pnl:+,.0f}円")
            else:
                print(f"   {code} {names.get(code,'')} {qty}株 @{ep:,.0f}")

    # --- 通算 ---
    tn, tnet = q("""SELECT COUNT(*), COALESCE(SUM(COALESCE(pnl_net,pnl)),0)
                    FROM trades WHERE exit_ts IS NOT NULL""").fetchone()
    print()
    print("-" * 42)
    print(f"📈 通算: {tn}取引  実質 {tnet:+,.0f}円")
    print("   (デモ口座なので本物のお金は動いていません)")

    # --- 見送った買いの追跡 ---
    try:
        sn, snet = q("""SELECT COUNT(*), COALESCE(SUM(COALESCE(pnl_net,pnl)),0)
                        FROM shadow_trades WHERE exit_ts IS NOT NULL""").fetchone()
        if sn:
            verdict = "見送って正解" if snet < 0 else "見送りは損だった"
            print(f"🔍 見送った買い {sn}件: もし買っていたら {snet:+,.0f}円 → {verdict}")
    except sqlite3.OperationalError:
        pass

    # --- ディスク ---
    dfree = subprocess.run(["bash", "-c", "df -h / | awk 'NR==2{print $4\" 空き (\"$5\" 使用)\"}'"],
                           capture_output=True, text=True).stdout.strip()
    snap = subprocess.run(["bash", "-c", "du -sh data/snapshots 2>/dev/null | cut -f1"],
                          capture_output=True, text=True).stdout.strip()
    print(f"💾 ディスク: {dfree} / 板録画 {snap}")
    conn.close()


if __name__ == "__main__":
    main()
