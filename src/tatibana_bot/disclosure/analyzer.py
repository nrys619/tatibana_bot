"""適時開示のLLM解析 (Claude API).

- 一次スクリーニング: タイトルのみで高速に好悪判定 (安価)
- 深掘り: インパクトが大きそうな開示はPDF本文ごと解析

structured outputs (messages.parse + Pydantic) を使うので
戻り値は常に検証済みのオブジェクトになる。
"""

from __future__ import annotations

import base64
import logging
from enum import Enum

import requests
from pydantic import BaseModel, Field

from tatibana_bot.disclosure.tdnet import Disclosure

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """あなたは日本株のデイトレーダーを支援するアナリストです。
与えられた適時開示 (TDnet) を、翌営業日の株価インパクトの観点で評価してください。

評価の観点:
- 業績修正は「なぜ修正されたか」が重要。一過性要因 (特損戻し、補助金、資産売却益) による
  上方修正は見た目より弱い。本業の需要増による修正は強い。
- 自社株買い・増配・株式分割は規模次第。発行済株式数に対する比率が大きいほど強い。
- 増資・MSワラント・株式売出しは希薄化でネガティブ。
- 決算は市場コンセンサス比が重要だが、コンセンサス不明なら会社計画比と進捗率で判断。
- デイトレ観点では「サプライズの大きさ」と「翌日に出来高を伴って動くか」を重視する。"""


class Sentiment(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class DisclosureAnalysis(BaseModel):
    """1件の開示に対する構造化された評価."""

    sentiment: Sentiment = Field(description="翌営業日の株価への方向性")
    impact: int = Field(ge=1, le=5, description="インパクトの大きさ 1(無視してよい)-5(ストップ高/安級)")
    category: str = Field(description="開示の種類 (決算/業績修正/自社株買い/増資/提携 など)")
    summary: str = Field(description="デイトレーダー向けの要点 (日本語で2文以内)")
    is_one_off: bool = Field(description="一過性要因が主因の場合 true")
    day_trade_note: str = Field(description="翌日のトレード戦略メモ (寄り付きの扱い方など、1文)")


class DisclosureAnalyzer:
    def __init__(self, model: str = "claude-opus-4-8"):
        import anthropic

        self._client = anthropic.Anthropic()
        self._model = model

    def analyze_title(self, disclosure: Disclosure) -> DisclosureAnalysis | None:
        """タイトルのみの軽量解析 (一次スクリーニング用)."""
        prompt = (
            f"銘柄: {disclosure.company_name} ({disclosure.code})\n"
            f"開示タイトル: {disclosure.title}\n"
            f"開示日時: {disclosure.pubdate}\n\n"
            "タイトルから判断できる範囲で評価してください。"
            "タイトルだけでは判断できない要素は保守的に (impactを低めに) 評価すること。"
        )
        return self._parse(prompt)

    def analyze_pdf(self, disclosure: Disclosure) -> DisclosureAnalysis | None:
        """開示PDF本文を含めた詳細解析 (impactが高い開示の深掘り用)."""
        try:
            pdf = requests.get(disclosure.url_pdf, timeout=30)
            pdf.raise_for_status()
            pdf_b64 = base64.standard_b64encode(pdf.content).decode()
        except Exception:
            logger.warning("failed to download PDF, falling back to title: %s",
                           disclosure.url_pdf, exc_info=True)
            return self.analyze_title(disclosure)

        content = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": pdf_b64,
                },
            },
            {
                "type": "text",
                "text": (
                    f"銘柄: {disclosure.company_name} ({disclosure.code})\n"
                    f"開示タイトル: {disclosure.title}\n\n"
                    "添付の開示資料を精読して評価してください。"
                ),
            },
        ]
        return self._parse(content)

    def _parse(self, content) -> DisclosureAnalysis | None:
        import anthropic

        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                output_format=DisclosureAnalysis,
            )
        except anthropic.APIStatusError:
            logger.error("Claude API error during disclosure analysis", exc_info=True)
            return None
        if response.stop_reason == "refusal":
            logger.warning("analysis refused: stop_details=%s", response.stop_details)
            return None
        return response.parsed_output

    def analyze_all(
        self, disclosures: list[Disclosure], deep_dive_impact: int = 4
    ) -> dict[str, DisclosureAnalysis]:
        """開示リストを一括解析。銘柄コード -> 最重要の解析結果 を返す.

        タイトル解析で impact が deep_dive_impact 以上ならPDF本文で再解析する。
        同一銘柄に複数開示がある場合は impact 最大のものを採用。
        """
        results: dict[str, DisclosureAnalysis] = {}
        for d in disclosures:
            analysis = self.analyze_title(d)
            if analysis is None:
                continue
            if analysis.impact >= deep_dive_impact and d.url_pdf:
                deep = self.analyze_pdf(d)
                if deep is not None:
                    analysis = deep
            prev = results.get(d.code)
            if prev is None or analysis.impact > prev.impact:
                results[d.code] = analysis
            logger.info("[%s] %s -> %s impact=%d (%s)",
                        d.code, d.title[:40], analysis.sentiment.value,
                        analysis.impact, analysis.category)
        return results
