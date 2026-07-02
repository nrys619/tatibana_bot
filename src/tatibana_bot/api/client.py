"""時価・板情報の取得 (CLMMfdsGetMarketPrice).

sTargetColumn のカラム名は e支店API仕様書の「時価情報項目」に対応する。
代表的なもの:
  pDPP  現在値          pDV   出来高
  pQBP  最良買気配      pQAP  最良売気配
  pGBP1..pGBP10 買気配値 pGBV1..pGBV10 買気配数量
  pGAP1..pGAP10 売気配値 pGAV1..pGAV10 売気配数量
仕様書の版によって名称が異なる場合があるため、実際のレスポンスを見て
BOARD_COLUMNS を調整すること。
"""

from __future__ import annotations

import logging
from datetime import datetime

from tatibana_bot.api.session import TachibanaSession
from tatibana_bot.models import Board, BoardLevel

logger = logging.getLogger(__name__)

DEPTH = 10

BOARD_COLUMNS = (
    ["pDPP", "pDV"]
    + [f"pGBP{i}" for i in range(1, DEPTH + 1)]
    + [f"pGBV{i}" for i in range(1, DEPTH + 1)]
    + [f"pGAP{i}" for i in range(1, DEPTH + 1)]
    + [f"pGAV{i}" for i in range(1, DEPTH + 1)]
)


def _to_float(value: object) -> float | None:
    try:
        s = str(value).strip()
        if not s or s in ("-", "--"):
            return None
        return float(s.replace(",", ""))
    except (TypeError, ValueError):
        return None


class MarketDataClient:
    def __init__(self, session: TachibanaSession):
        self._session = session

    def get_boards(self, codes: list[str]) -> dict[str, Board]:
        """複数銘柄の板スナップショットを取得."""
        data = self._session.price(
            "CLMMfdsGetMarketPrice",
            sTargetIssueCode=",".join(codes),
            sTargetColumn=",".join(BOARD_COLUMNS),
        )
        now = datetime.now()
        boards: dict[str, Board] = {}
        rows = data.get("aCLMMfdsMarketPrice", [])
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            code = str(row.get("sIssueCode", "")).strip()
            if not code:
                continue
            boards[code] = self._parse_board(code, row, now)
        return boards

    def _parse_board(self, code: str, row: dict, ts: datetime) -> Board:
        bids: list[BoardLevel] = []
        asks: list[BoardLevel] = []
        for i in range(1, DEPTH + 1):
            bp, bv = _to_float(row.get(f"pGBP{i}")), _to_float(row.get(f"pGBV{i}"))
            if bp is not None and bv is not None and bv > 0:
                bids.append(BoardLevel(price=bp, quantity=bv))
            ap, av = _to_float(row.get(f"pGAP{i}")), _to_float(row.get(f"pGAV{i}"))
            if ap is not None and av is not None and av > 0:
                asks.append(BoardLevel(price=ap, quantity=av))
        return Board(
            code=code,
            ts=ts,
            bids=bids,
            asks=asks,
            last_price=_to_float(row.get("pDPP")),
        )
