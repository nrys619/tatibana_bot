"""TDnet 適時開示の取得.

TDnetには公式の無償APIが無いため、コミュニティAPI
(webapi.yanoshin.jp/tdnet) を利用する。非公式サービスなので
本格運用ではTDnetの正規データ配信サービスや自前スクレイパへの
差し替えを検討すること。
"""

from __future__ import annotations

import logging
import re
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


def fetch_disclosures(day: date | None = None, limit: int = 300, timeout: int = 30) -> list[Disclosure]:
    """指定日 (デフォルト今日) の適時開示一覧を取得.

    まずコミュニティAPI (webapi.yanoshin.jp) を試し、落ちていたら
    東証の公式ページ (release.tdnet.info) を直接読む。
    """
    key = (day or date.today()).strftime("%Y%m%d")
    try:
        resp = requests.get(TDNET_API.format(key=key), params={"limit": limit}, timeout=timeout)
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception:
        logger.warning("community API unavailable — falling back to official TDnet pages")
        return _fetch_official(key)

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


TDNET_OFFICIAL = "https://www.release.tdnet.info/inbs/I_list_{page:03d}_{key}.html"

_OFFICIAL_ROW = re.compile(
    r'kjTime[^>]*>\s*([\d:]+)\s*<'
    r'.*?kjCode[^>]*>\s*(\d+)\s*<'
    r'.*?kjName[^>]*>(.*?)</td>'
    r'.*?kjTitle[^>]*>\s*<a href="([^"]+)"[^>]*>(.*?)</a>',
    re.S,
)


def _fetch_official(key: str, max_pages: int = 30) -> list[Disclosure]:
    """東証公式の日別一覧ページ (1ページ約100件) をページ送りしながら読む."""
    out: list[Disclosure] = []
    for page in range(1, max_pages + 1):
        url = TDNET_OFFICIAL.format(page=page, key=key)
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            break
        resp.encoding = "utf-8"
        rows = _OFFICIAL_ROW.findall(resp.text)
        if not rows:
            break
        for hhmm, code, name, pdf_href, title in rows:
            code = code.strip()
            if len(code) == 5 and code.endswith("0"):
                code = code[:4]
            out.append(
                Disclosure(
                    code=code,
                    company_name=re.sub(r"<[^>]+>", "", name).strip(),
                    title=re.sub(r"<[^>]+>", "", title).strip(),
                    pubdate=f"{key[:4]}-{key[4:6]}-{key[6:]} {hhmm}",
                    url_pdf=f"https://www.release.tdnet.info/inbs/{pdf_href}",
                )
            )
    logger.info("fetched %d disclosures for %s (official pages)", len(out), key)
    return out


def filter_by_codes(disclosures: list[Disclosure], codes: set[str]) -> list[Disclosure]:
    return [d for d in disclosures if d.code in codes]
