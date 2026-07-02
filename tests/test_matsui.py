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
