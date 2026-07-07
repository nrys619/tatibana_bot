"""松井証券の公開マーケット情報 (finance.matsui.co.jp) からランキングを取得する.

ログイン不要の公開ページをスクレイピングする (robots.txt は制限なしを確認済み)。
夜間バッチで1日数回読む程度の負荷に留めること。

取得できるランキング (RANKINGS のキー):
  day_trading_morning / day_trading_afternoon : デイトレ適性 (株価変動率×売買代金)
  tick                : 約定回数 (Tick) 上位
  volume / volume_surge : 出来高上位 / 出来高急増
  trading_value / trading_value_surge : 売買代金上位 / 売買代金急増
  rise / fall         : 値上がり率 / 値下がり率
  interval_rise / interval_fall : 値上がり幅 / 値下がり幅

※ 信用残 (週次) と業種別ランキングもサイトにはあるが、銘柄選定には
   直接使いにくいためここでは扱わない。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://finance.matsui.co.jp"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) tatibana_bot/0.1"

RANKINGS = {
    "day_trading_morning": "ranking-day-trading-morning",
    "day_trading_afternoon": "ranking-day-trading-afternoon",
    "tick": "ranking-tick",
    "volume": "ranking-volume",
    "volume_surge": "ranking-volume-surge",
    "trading_value": "ranking-trading-top",
    "trading_value_surge": "ranking-trading-increase",
    "rise": "ranking-rise",
    "fall": "ranking-fall",
    "interval_rise": "ranking-interval-rise",
    "interval_fall": "ranking-interval-fall",
}

# market クエリ: 0=全体 1=東証プライム 2=スタンダード 3=グロース 4=日経225
MARKETS = {0: "全体", 1: "東証プライム", 2: "東証スタンダード", 3: "東証グロース", 4: "日経225"}

# 表ヘッダー -> 正規化キー
_HEADER_KEYS = {
    "現在値": "price",
    "出来高": "volume",
    "概算売買代金": "turnover_jpy",
    "株価変動率": "range_pct",
    "約定回数": "ticks",
}

# 個別株の市場タグ (ETF・REIT 等を除くのに使う)
_STOCK_MARKETS = ("東P", "東S", "東G")


def _to_number(text: str) -> Any:
    s = text.strip().replace(",", "").replace("%", "").replace("円", "")
    try:
        return float(s)
    except ValueError:
        return text.strip()


def _parse_cell(td) -> Any:
    # 先頭の <span>ラベル：</span> (モバイル用) を除いた本文を数値化する
    for span in td.find_all("span"):
        if span.get_text().endswith("："):
            span.decompose()
    return _to_number(td.get_text(" ", strip=True))


def parse_ranking_html(html: str) -> list[dict]:
    """ランキングページのHTMLから銘柄リストを取り出す."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="m-table")
    if table is None:
        return []

    headers = [th.get_text(strip=True) for th in table.find_all("th")]
    rows = []
    for tr in table.find_all("tr"):
        tds = [
            td for td in tr.find_all("td")
            if "m-sp-only" not in (td.get("class") or [])  # モバイル専用の重複セルは除く
        ]
        if len(tds) < 3:
            continue

        link = tds[1].find("a")
        tag = tds[1].find("span")
        if link is None or tag is None:
            continue
        code_market = tag.get_text(strip=True).split()
        row: dict[str, Any] = {
            "rank": int(tds[0].get_text(strip=True)),
            "code": code_market[0],
            "market": code_market[1] if len(code_market) > 1 else "",
            "name": link.get_text(strip=True),
        }

        # 3列目以降を表ヘッダーに対応づける (最後の「注文」列は捨てる)
        for header, td in zip(headers[2:], tds[2:]):
            if header == "注文":
                continue
            if header == "前日比":
                text = td.get_text(" ", strip=True)
                row["change_text"] = text
                if "(" in text:
                    change, _, pct = text.partition("(")
                    row["change"] = _to_number(change)
                    row["change_pct"] = _to_number(pct.rstrip(")"))
            else:
                row[_HEADER_KEYS.get(header, header)] = _parse_cell(td)
        rows.append(row)
    return rows


def fetch_ranking(
    kind: str,
    market: int = 0,
    pages: int = 1,
    timeout: int = 10,
) -> list[dict]:
    """指定種類のランキングを取得する (1ページ=50銘柄)."""
    if kind not in RANKINGS:
        raise ValueError(f"unknown ranking kind: {kind} (choose from {list(RANKINGS)})")
    result: list[dict] = []
    for page in range(1, pages + 1):
        url = f"{BASE_URL}/{RANKINGS[kind]}/index"
        params = {"market": market, "page": page}
        try:
            res = requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=timeout)
            res.raise_for_status()
        except Exception:
            logger.warning("matsui ranking fetch failed: %s page=%d", kind, page, exc_info=True)
            break
        rows = parse_ranking_html(res.text)
        if not rows:
            break
        result.extend(rows)
        if page < pages:
            time.sleep(0.5)  # 連続アクセスの間隔を空ける (礼儀)
    logger.info("matsui ranking %s: %d rows", kind, len(result))
    return result


def candidate_codes(
    kinds: list[str],
    market: int = 0,
    top_k: int = 20,
    stocks_only: bool = True,
) -> dict[str, int]:
    """複数ランキングの上位 top_k から銘柄候補を集める.

    戻り値は code -> 登場したランキング数 (多いほど注目度が高い)。
    stocks_only=True なら ETF/REIT 等 (東P/東S/東G 以外) を除く。
    """
    counts: dict[str, int] = {}
    for i, kind in enumerate(kinds):
        if i:
            time.sleep(0.5)
        for row in fetch_ranking(kind, market=market)[:top_k]:
            if stocks_only and row["market"] not in _STOCK_MARKETS:
                continue
            counts[row["code"]] = counts.get(row["code"], 0) + 1
    return counts


def rows_to_watch_items(
    rows: list[dict],
    top_n: int = 5,
    max_price_jpy: float | None = None,
    min_turnover_jpy: float = 0.0,
):
    """ランキング行を予算・流動性でふるいにかけ、監視銘柄リストに変換する."""
    from tatibana_bot.models import WatchItem

    items = []
    for row in rows:
        if row.get("market") not in _STOCK_MARKETS:
            continue
        price = row.get("price")
        if not isinstance(price, (int, float)):
            continue
        if max_price_jpy is not None and price > max_price_jpy:
            continue
        turnover = row.get("turnover_jpy")
        if isinstance(turnover, (int, float)) and turnover < min_turnover_jpy:
            continue
        items.append(WatchItem(code=row["code"], name=row.get("name", ""),
                               notes=[f"intraday_scan#{row.get('rank', '?')}"]))
        if len(items) >= top_n:
            break
    return items


def scan_daytrade_watchlist(
    top_n: int = 5,
    max_price_jpy: float | None = None,
    min_turnover_jpy: float = 0.0,
    market: int = 0,
    afternoon: bool = False,
):
    """場中用: デイトレ適性ランキング(リアルタイム更新)から監視候補を選ぶ."""
    kind = "day_trading_afternoon" if afternoon else "day_trading_morning"
    rows = fetch_ranking(kind, market=market)
    return rows_to_watch_items(rows, top_n=top_n, max_price_jpy=max_price_jpy,
                               min_turnover_jpy=min_turnover_jpy)
