"""JPXの上場銘柄一覧から「偏りのない銘柄リスト」を作り、日足を集める.

これまでの検証は、松井のランキングに載った銘柄 (= 後から動いた銘柄) だけを
対象にしていた。当たりくじだけ入った箱から引いていたのと同じで、
スイングの優位もスクリーニングモデルのAUC 0.95も、この偏りで膨らんでいる疑いがある。

JPXが公開している上場銘柄一覧を使えば「後から選んだのではない」母集団になる。

**残る偏り(正直に書いておく)**: 上場廃止になった銘柄は今の一覧に載らないため、
「生き残った銘柄だけ」という偏りは消えない。ただし「動いた銘柄だけ」という
今の偏りに比べれば影響は格段に小さい。

usage:
  python scripts/build_universe.py --fetch-list      # 一覧を取り直す
  python scripts/build_universe.py --download        # 日足を集める (時間がかかる)
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("universe")

JPX_URL = ("https://www.jpx.co.jp/markets/statistics-equities/misc/"
           "tvdivq0000001vg2-att/data_j.xls")
OUT_JSON = Path("data/universe_jpx.json")
DAILY_DIR = Path("data/daily_all")


def fetch_list() -> pd.DataFrame:
    import requests
    r = requests.get(JPX_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    tmp = Path("/tmp/jpx.xls")
    tmp.write_bytes(r.content)
    d = pd.read_excel(tmp)
    # 内国株のみ (ETF/REIT/PRO Market/外国株は対象外)
    d = d[d["市場・商品区分"].isin([
        "プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）"])]
    d = d[["コード", "銘柄名", "市場・商品区分", "33業種区分", "規模区分"]].copy()
    d["コード"] = d["コード"].astype(str).str.strip()
    return d.reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-list", action="store_true")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--days", type=int, default=750)
    ap.add_argument("--limit", type=int, default=0, help="先頭N銘柄だけ (試験用)")
    args = ap.parse_args()

    if args.fetch_list or not OUT_JSON.exists():
        d = fetch_list()
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(
            {r["コード"]: {"name": r["銘柄名"], "market": r["市場・商品区分"],
                           "sector": r["33業種区分"], "size": r["規模区分"]}
             for _, r in d.iterrows()}, ensure_ascii=False, indent=1))
        logger.info("銘柄一覧を保存: %d銘柄 -> %s", len(d), OUT_JSON)
        logger.info("  市場別: %s", d["市場・商品区分"].value_counts().to_dict())

    if not args.download:
        return

    from tatibana_bot.data.daily import _fetch, _cache_path
    codes = list(json.loads(OUT_JSON.read_text()))
    if args.limit:
        codes = codes[:args.limit]
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("日足の取得を開始: %d銘柄 (%d日分)", len(codes), args.days)

    ok = skip = fail = 0
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        path = DAILY_DIR / f"{code}.parquet"
        if path.exists():
            skip += 1
            continue
        try:
            df = _fetch(code, args.days)
            if df is not None and len(df) > 60:
                df.to_parquet(path)
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1
        if i % 200 == 0:
            el = time.time() - t0
            logger.info("  %d/%d 完了 (取得%d 既存%d 失敗%d) 経過%.0f分 残り約%.0f分",
                        i, len(codes), ok, skip, fail, el / 60,
                        el / i * (len(codes) - i) / 60)
    logger.info("完了: 取得%d / 既存%d / 失敗%d / %.0f分", ok, skip, fail,
                (time.time() - t0) / 60)
    size = sum(f.stat().st_size for f in DAILY_DIR.glob("*.parquet")) / 1e6
    logger.info("ディスク使用: %.0fMB (%d銘柄)", size, len(list(DAILY_DIR.glob("*.parquet"))))


if __name__ == "__main__":
    main()
