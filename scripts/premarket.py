"""寄り前バッチ: TDnet開示をLLM解析して監視リストに反映する.

- 監視リスト銘柄の開示 -> センチメント/インパクトを付与
- 監視リスト外でも impact >= min_impact の開示銘柄は監視リストに追加

usage: python scripts/premarket.py [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from tatibana_bot.config import load_config
from tatibana_bot.disclosure.analyzer import DisclosureAnalyzer
from tatibana_bot.disclosure.tdnet import fetch_disclosures
from tatibana_bot.models import WatchItem
from tatibana_bot.screening.watchlist import load_watchlist, save_watchlist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("premarket")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="開示を取得する日 (デフォルト: 前営業日=昨日)")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    # 引け後〜夜に出た開示が翌日の材料になるため、デフォルトは前日分
    target_day = date.fromisoformat(args.date) if args.date else date.today() - timedelta(days=1)

    watchlist = load_watchlist(cfg.paths.watchlist)
    watch_codes = {w.code for w in watchlist}
    universe = set(cfg.universe.codes)

    disclosures = fetch_disclosures(target_day, limit=cfg.disclosure.max_docs_per_run)
    # ユニバース内の開示だけをLLMにかける (コスト管理)
    relevant = [d for d in disclosures if d.code in universe | watch_codes]
    if not relevant:
        logger.info("no relevant disclosures for %s", target_day)
        return

    analyzer = DisclosureAnalyzer(model=cfg.disclosure.model)
    results = analyzer.analyze_all(relevant)

    by_code = {w.code: w for w in watchlist}
    for code, analysis in results.items():
        if code in by_code:
            w = by_code[code]
        elif analysis.impact >= cfg.disclosure.min_impact_for_watchlist:
            w = WatchItem(code=code, notes=["added by disclosure"])
            watchlist.append(w)
            by_code[code] = w
        else:
            continue
        w.disclosure_sentiment = analysis.sentiment.value
        w.disclosure_impact = analysis.impact
        w.disclosure_summary = f"{analysis.summary} / {analysis.day_trade_note}"

    save_watchlist(watchlist, cfg.paths.watchlist)
    logger.info("watchlist updated with %d disclosure analyses", len(results))
    for w in watchlist:
        logger.info("  %s score=%.3f sentiment=%s impact=%s %s",
                    w.code, w.ml_score, w.disclosure_sentiment,
                    w.disclosure_impact, (w.disclosure_summary or "")[:60])


if __name__ == "__main__":
    main()
