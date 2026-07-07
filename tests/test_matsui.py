from pathlib import Path

import pytest

from tatibana_bot.data.matsui import RANKINGS, fetch_ranking, parse_ranking_html

FIXTURE = Path(__file__).parent / "fixtures" / "matsui_daytrading_sample.html"


def test_parse_ranking_html():
    rows = parse_ranking_html(FIXTURE.read_text())
    assert len(rows) == 3

    top = rows[0]
    assert top["rank"] == 1
    assert top["code"] == "285A"
    assert top["market"] == "東P"
    assert top["name"] == "キオクシアホールディングス"
    assert top["price"] == 76260.0
    assert top["volume"] == 19983500.0
    assert top["turnover_jpy"] == 1538487576000.0
    assert top["range_pct"] == 4.57


def test_parse_ranking_html_empty():
    assert parse_ranking_html("<html><body>メンテナンス中</body></html>") == []


def test_fetch_ranking_rejects_unknown_kind():
    with pytest.raises(ValueError):
        fetch_ranking("no_such_ranking")


def test_ranking_kinds_defined():
    assert "day_trading_afternoon" in RANKINGS
    assert "tick" in RANKINGS


def test_rows_to_watch_items_filters():
    from tatibana_bot.data.matsui import rows_to_watch_items

    rows = [
        {"rank": 1, "code": "285A", "market": "東P", "name": "キオクシア",
         "price": 76260.0, "turnover_jpy": 1.5e12},   # 値がさ → 除外
        {"rank": 2, "code": "1570", "market": "東E", "name": "日経レバ",
         "price": 250.0, "turnover_jpy": 1e11},        # ETF → 除外
        {"rank": 3, "code": "5802", "market": "東P", "name": "住友電工",
         "price": 2546.0, "turnover_jpy": 5e10},       # 採用
        {"rank": 4, "code": "9999", "market": "東G", "name": "薄商い",
         "price": 500.0, "turnover_jpy": 1e8},         # 流動性不足 → 除外
        {"rank": 5, "code": "6526", "market": "東P", "name": "ソシオネクスト",
         "price": 2685.5, "turnover_jpy": 8e10},       # 採用
    ]
    items = rows_to_watch_items(rows, top_n=5, max_price_jpy=3000, min_turnover_jpy=1e9)
    assert [w.code for w in items] == ["5802", "6526"]
    assert items[0].notes == ["intraday_scan#3"]
