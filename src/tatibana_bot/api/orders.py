"""発注ゲートウェイ (現物・信用の新規/取消).

CLMKabuNewOrder のパラメータは e支店API仕様書「株式新規注文」に対応。
実弾投入前に必ずデモ環境 (config: api.env=demo) で電文を検証すること。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from tatibana_bot.api.session import TachibanaSession
from tatibana_bot.models import Side

logger = logging.getLogger(__name__)

# 売買区分 (仕様書: sBaibaiKubun) 3=買 1=売
BAIBAI = {Side.BUY: "3", Side.SELL: "1"}


class OrderGateway:
    def __init__(self, session: TachibanaSession):
        self._session = session
        # 注文に必須の第2暗証番号 (.env の TACHIBANA_SECOND_PASSWORD)
        self._second_pw = os.environ.get("TACHIBANA_SECOND_PASSWORD", "")

    def new_market_order(self, code: str, side: Side, quantity: int) -> dict[str, Any]:
        """成行の現物注文."""
        return self._session.request(
            "CLMKabuNewOrder",
            sZyoutoekiKazeiC="1",       # 譲渡益課税: 特定
            sIssueCode=code,
            sSizyouC="00",              # 市場: 東証
            sBaibaiKubun=BAIBAI[side],
            sCondition="0",             # 執行条件: 指定なし
            sOrderPrice="0",            # 0=成行
            sOrderSuryou=str(quantity),
            sGenkinShinyouKubun="0",    # 0=現物
            sOrderExpireDay="0",        # 当日限り
            sGyakusasiOrderType="0",
            sGyakusasiZyouken="0",
            sGyakusasiPrice="*",
            sTatebiType="*",
            sTategyokuZyoutoekiKazeiC="*",
            sSecondPassword=self._second_pw,
        )

    def new_limit_order(
        self, code: str, side: Side, quantity: int, price: float
    ) -> dict[str, Any]:
        """指値の現物注文."""
        return self._session.request(
            "CLMKabuNewOrder",
            sZyoutoekiKazeiC="1",
            sIssueCode=code,
            sSizyouC="00",
            sBaibaiKubun=BAIBAI[side],
            sCondition="0",
            sOrderPrice=str(price),
            sOrderSuryou=str(quantity),
            sGenkinShinyouKubun="0",
            sOrderExpireDay="0",
            sGyakusasiOrderType="0",
            sGyakusasiZyouken="0",
            sGyakusasiPrice="*",
            sTatebiType="*",
            sTategyokuZyoutoekiKazeiC="*",
            sSecondPassword=self._second_pw,
        )

    def new_margin_order(self, code: str, side: Side, quantity: int) -> dict[str, Any]:
        """成行の信用新規注文 (制度信用)。sideがSELLなら空売り."""
        return self._session.request(
            "CLMKabuNewOrder",
            sZyoutoekiKazeiC="1",
            sIssueCode=code,
            sSizyouC="00",
            sBaibaiKubun=BAIBAI[side],
            sCondition="0",
            sOrderPrice="0",            # 成行
            sOrderSuryou=str(quantity),
            sGenkinShinyouKubun="2",    # 2=信用新規
            sOrderExpireDay="0",
            sGyakusasiOrderType="0",
            sGyakusasiZyouken="0",
            sGyakusasiPrice="*",
            sTatebiType="*",            # 新規は指定なし
            sTategyokuZyoutoekiKazeiC="*",
            sSecondPassword=self._second_pw,
        )

    def repay_margin_order(self, code: str, side: Side, quantity: int) -> dict[str, Any]:
        """成行の信用返済注文。買建の返済は side=SELL、売建の返済は side=BUY."""
        return self._session.request(
            "CLMKabuNewOrder",
            sZyoutoekiKazeiC="1",
            sIssueCode=code,
            sSizyouC="00",
            sBaibaiKubun=BAIBAI[side],
            sCondition="0",
            sOrderPrice="0",            # 成行
            sOrderSuryou=str(quantity),
            sGenkinShinyouKubun="4",    # 4=信用返済
            sOrderExpireDay="0",
            sGyakusasiOrderType="0",
            sGyakusasiZyouken="0",
            sGyakusasiPrice="*",
            sTatebiType="2",            # 建日順に返済
            sTategyokuZyoutoekiKazeiC="*",
            sSecondPassword=self._second_pw,
        )

    def stock_positions(self) -> list[dict[str, Any]]:
        """現物残高一覧 (銘柄ごとの保有株数・売付可能株数)."""
        data = self._session.request("CLMGenbutuKabuList", sIssueCode="")
        return data.get("aGenbutuKabuList", []) or []

    def margin_positions(self) -> list[dict[str, Any]]:
        """信用建玉一覧."""
        data = self._session.request("CLMShinyouTategyokuList", sIssueCode="")
        return data.get("aShinyouTategyokuList", []) or []

    def cancel_order(self, order_number: str, eigyou_day: str) -> dict[str, Any]:
        return self._session.request(
            "CLMKabuCancelOrder",
            sOrderNumber=order_number,
            sEigyouDay=eigyou_day,
            sSecondPassword=self._second_pw,
        )

    def list_orders(self) -> dict[str, Any]:
        """当日の注文一覧."""
        return self._session.request("CLMOrderList")

    def cash_balance(self) -> dict[str, Any]:
        """買付余力の照会."""
        return self._session.request("CLMZanKaiSummary")
