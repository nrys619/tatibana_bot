"""残った建玉・現物の後始末 (寄り前に実行する想定).

- 信用建玉: すべて成行で返済 (デイトレボットは持ち越さない前提のため、
  残っている建玉は前日の決済失敗などの異常系)
- 現物: 売付可能株数があれば成行売り (上場廃止銘柄などの売却不能はスキップ)

usage: python scripts/repay_leftovers.py
"""

from __future__ import annotations

import logging

from tatibana_bot.api.factory import create_session
from tatibana_bot.api.orders import OrderGateway
from tatibana_bot.api.session import TachibanaApiError
from tatibana_bot.config import load_config
from tatibana_bot.models import Side

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("repay_leftovers")


def main() -> None:
    cfg = load_config()
    with create_session(cfg) as session:
        gw = OrderGateway(session)

        margins = [
            (r.get("sOrderIssueCode"), str(r.get("sOrderBaibaiKubun")),
             int(float(r.get("sOrderTategyokuSuryou", "0") or 0)))
            for r in gw.margin_positions()
            if int(float(r.get("sOrderTategyokuSuryou", "0") or 0)) > 0
        ]
        for code, baibai, qty in margins:
            side = Side.SELL if baibai == "3" else Side.BUY
            try:
                gw.repay_margin_order(code, side, qty)
                logger.info("repaid leftover margin: %s x%d", code, qty)
            except TachibanaApiError:
                logger.warning("failed to repay %s x%d", code, qty, exc_info=True)

        stocks = [
            (r.get("sUriOrderIssueCode"),
             int(float(r.get("sUriOrderUritukeKanouSuryou", "0") or 0)))
            for r in gw.stock_positions()
            if int(float(r.get("sUriOrderUritukeKanouSuryou", "0") or 0)) > 0
        ]
        for code, qty in stocks:
            try:
                gw.new_market_order(code, Side.SELL, qty)
                logger.info("sold leftover stock: %s x%d", code, qty)
            except TachibanaApiError:
                logger.warning("failed to sell %s x%d (上場廃止銘柄等はスキップ)", code, qty,
                               exc_info=True)

        if not margins and not stocks:
            logger.info("no leftovers — account is clean")


if __name__ == "__main__":
    main()
