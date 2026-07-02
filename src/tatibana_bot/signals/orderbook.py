"""板 (気配値) から短期シグナル用の特徴量を計算する."""

from __future__ import annotations

from tatibana_bot.models import Board


def board_features(board: Board, depth: int = 5) -> dict[str, float]:
    """板スナップショット1枚から特徴量ベクトルを作る.

    返す特徴量:
      imbalance      : (買い数量-売り数量)/(合計) -1..1 で正なら買い優勢
      imbalance_top1 : 最良気配だけの不均衡
      spread_bps     : スプレッド (bp)
      microprice_dev : マイクロプライスの中値からの乖離 (正なら上方向の圧力)
      bid_depth, ask_depth : 上位depth段の数量合計
    """
    bids = board.bids[:depth]
    asks = board.asks[:depth]
    bid_qty = sum(l.quantity for l in bids)
    ask_qty = sum(l.quantity for l in asks)
    total = bid_qty + ask_qty

    feats: dict[str, float] = {
        "bid_depth": bid_qty,
        "ask_depth": ask_qty,
        "imbalance": (bid_qty - ask_qty) / total if total > 0 else 0.0,
    }

    if bids and asks:
        b1, a1 = bids[0], asks[0]
        top_total = b1.quantity + a1.quantity
        feats["imbalance_top1"] = (
            (b1.quantity - a1.quantity) / top_total if top_total > 0 else 0.0
        )
        mid = (b1.price + a1.price) / 2
        feats["spread_bps"] = (a1.price - b1.price) / mid * 10000 if mid > 0 else 0.0
        # マイクロプライス: 反対側の数量で加重した「実勢価格」
        if top_total > 0 and mid > 0:
            microprice = (b1.price * a1.quantity + a1.price * b1.quantity) / top_total
            feats["microprice_dev"] = (microprice - mid) / mid * 10000
        else:
            feats["microprice_dev"] = 0.0
    else:
        # 片側の板が消えている (ストップ張り付き等) — 異常検知側で扱う
        feats["imbalance_top1"] = 1.0 if bids else -1.0
        feats["spread_bps"] = float("inf")
        feats["microprice_dev"] = 0.0

    return feats
