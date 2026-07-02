"""監視リストの永続化 (①MLの出力に②LLM解析の結果をマージして保存)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from tatibana_bot.models import WatchItem


def save_watchlist(items: list[WatchItem], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump([asdict(w) for w in items], f, ensure_ascii=False, indent=2)


def load_watchlist(path: str | Path) -> list[WatchItem]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return [WatchItem(**row) for row in data]
