"""TDnet 適時開示の取得.

TDnetには公式の無償APIが無いため、コミュニティAPI
(webapi.yanoshin.jp/tdnet) を利用する。非公式サービスなので
本格運用ではTDnetの正規データ配信サービスや自前スクレイパへの
差し替えを検討すること。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import requests

logger = logging.getLogger(__name__)

TDNET_API = "https://webapi.yanoshin.jp/webapi/tdnet/list/{key}.json"


@dataclass
class Disclosure:
    code: str          # 銘柄コード (4桁)
    company_name: str
    title: str
    pubdate: str
    url_pdf: str
    url_xbrl: str = ""


def fetch_disclosures(day: date | None = None, limit: int = 300) -> list[Disclosure]:
    """指定日 (デフォルト今日) の適時開示一覧を取得."""
    key = (day or date.today()).strftime("%Y%m%d")
    resp = requests.get(TDNET_API.format(key=key), params={"limit": limit}, timeout=30)
    resp.raise_for_status()
    items = resp.json().get("items", [])

    out: list[Disclosure] = []
    for item in items:
        td = item.get("Tdnet", {})
        code = str(td.get("company_code", "")).strip()
        # TDnetの銘柄コードは5桁 (末尾0) のことが多いので4桁に正規化
        if len(code) == 5 and code.endswith("0"):
            code = code[:4]
        out.append(
            Disclosure(
                code=code,
                company_name=td.get("company_name", ""),
                title=td.get("title", ""),
                pubdate=td.get("pubdate", ""),
                url_pdf=td.get("document_url", ""),
                url_xbrl=td.get("url_xbrl", ""),
            )
        )
    logger.info("fetched %d disclosures for %s", len(out), key)
    return out


def filter_by_codes(disclosures: list[Disclosure], codes: set[str]) -> list[Disclosure]:
    return [d for d in disclosures if d.code in codes]
