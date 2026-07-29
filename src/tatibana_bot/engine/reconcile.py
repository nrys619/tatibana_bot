"""起動時に「帳簿の未決済」と「実口座の建玉」を突き合わせる.

エンジンが異常終了したり引けの一括決済を取りこぼすと、建玉が誰にも管理されない
まま残る。2026-07-17 (Macのスリープでエンジンが日中2回死亡) と 2026-07-28
(引けの一括決済で取りこぼし) の2回、実際にこれが起きた。

起動のたびにここを通し、
  - 帳簿に未決済があり、実口座にも建玉がある -> エンジンの管理下に引き取る
  - 帳簿に未決済があるが、実口座に建玉が無い -> 帳簿を正す (幽霊行)
  - 実口座に100株単位の建玉があるのに帳簿に無い -> 警告 (デモのサンプルと紛らわしいので自動処理はしない)
"""

from __future__ import annotations

import logging
from datetime import datetime

from tatibana_bot.models import Position, Side

logger = logging.getLogger(__name__)

# 信用建玉の売買区分 (立花API): 3=買建 / 1=売建
_KUBUN_TO_SIDE = {"3": "buy", "1": "sell"}


def _account_margin(gateway) -> list[dict] | None:
    """実口座の建玉。照会に失敗したら None (「建玉なし」と区別する)."""
    try:
        return gateway.margin_positions()
    except Exception:
        logger.error("建玉照会に失敗した — 照合を中止する "
                     "(失敗を『建玉なし』と誤認すると実在する建玉の帳簿を消してしまう)",
                     exc_info=True)
        return None


def reconcile_startup(
    gateway,
    trade_log,
    stop_pct: float,
    target_pct: float,
    max_hold_sec: int,
    trailing_pct: float,
    now: datetime | None = None,
) -> list[tuple[Position, int]]:
    """帳簿と実口座を突き合わせ、引き取るべき建玉を (Position, trade_id) で返す."""
    now = now or datetime.now()
    pending = trade_log.open_trades()
    account = _account_margin(gateway)
    if account is None:
        # 実口座の状態が分からないまま帳簿を書き換えるのが一番危ない。何もしない。
        if pending:
            logger.error("起動時照合を中止した。未決済が %d件 残ったままなので手動確認が必要",
                         len(pending))
        return []

    # 実口座側を (銘柄, 売買) -> 株数 に畳む
    held: dict[tuple[str, str], int] = {}
    for p in account:
        code = str(p.get("sOrderIssueCode", "")).strip()
        side = _KUBUN_TO_SIDE.get(str(p.get("sOrderBaibaiKubun", "")).strip())
        try:
            qty = int(float(p.get("sTategyokuSuryou") or 0))
        except (TypeError, ValueError):
            qty = 0
        if code and side and qty > 0:
            held[(code, side)] = held.get((code, side), 0) + qty

    if not pending:
        logger.info("起動時照合: 帳簿に未決済なし (実口座の建玉 %d件)", len(account))
    adopted: list[tuple[Position, int]] = []

    for row in pending:
        key = (row["code"], row["side"])
        available = held.get(key, 0)
        if available >= row["quantity"]:
            side = Side.BUY if row["side"] == "buy" else Side.SELL
            sign = 1 if side == Side.BUY else -1
            entry = float(row["entry_price"])
            pos = Position(
                code=row["code"], side=side, quantity=row["quantity"],
                entry_price=entry, entry_ts=datetime.fromisoformat(row["entry_ts"]),
                stop_price=entry * (1 - sign * stop_pct / 100),
                target_price=entry * (1 + sign * target_pct / 100),
                max_hold_sec=max_hold_sec, trailing_pct=trailing_pct, peak=entry,
                tag="adopted",
            )
            adopted.append((pos, row["id"]))
            held[key] = available - row["quantity"]
            logger.warning("起動時照合: 宙に浮いた建玉を引き取った %s %s %d株 (建 %s)",
                           row["code"], row["side"], row["quantity"], row["entry_ts"])
        else:
            # 実口座に無い = すでに決済済みか、そもそも約定していなかった。
            # 損益は不明なので0で閉じ、理由を残して集計から除外できるようにする。
            trade_log.close_trade(row["id"], now, float(row["entry_price"]), 0.0,
                                  "reconciled(no_position)")
            logger.warning("起動時照合: 実口座に無い未決済を帳簿上で解消 %s %s %d株 (建 %s)",
                           row["code"], row["side"], row["quantity"], row["entry_ts"])

    # 帳簿に無い100株単位の建玉 = ボットが作った可能性がある。自動処理はせず警告のみ。
    for (code, side), qty in held.items():
        if qty > 0 and qty % 100 == 0 and qty <= 1000:
            logger.warning("起動時照合: 帳簿に無い建玉あり %s %s %d株 "
                           "(デモのサンプルかもしれないので自動決済はしない)", code, side, qty)
    return adopted
