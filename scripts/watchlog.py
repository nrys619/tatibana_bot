"""engine.log から「その時刻に実際に監視していた銘柄」の履歴を復元する.

バックテストが実機と乖離していた最大の原因のひとつが、記録専用銘柄まで
売買してしまっていたこと。実機が実際に売買できた銘柄だけを再生するために、
エンジンのログから監視リストの時系列を組み立てる。

engine.log の該当行:
  "engine start: mode=live watchlist=['6976', ...] regime_mult=0.30"
  "watchlist updated: +[...] -[...] -> ['6976', ...]"
"""

from __future__ import annotations

import ast
import re
from bisect import bisect_right
from pathlib import Path

_START = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d),\d+ .*engine start: .*watchlist=(\[[^\]]*\])")
_UPDATE = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d),\d+ .*watchlist updated: .*-> (\[[^\]]*\])")


class WatchTimeline:
    """日付 -> [(秒, その時点の監視銘柄集合)] を保持し、時刻から集合を引く."""

    def __init__(self, by_day: dict[str, list[tuple[int, frozenset[str]]]]):
        self._by_day = by_day

    @property
    def days(self) -> list[str]:
        return sorted(self._by_day)

    def codes_at(self, day: str, hhmmss: str) -> frozenset[str]:
        """day は 'YYYY-MM-DD'、hhmmss は 'HH:MM:SS'。履歴がなければ空集合."""
        entries = self._by_day.get(day)
        if not entries:
            return frozenset()
        sec = _to_sec(hhmmss)
        i = bisect_right([e[0] for e in entries], sec) - 1
        if i < 0:
            return frozenset()
        return entries[i][1]

    def all_codes(self, day: str) -> frozenset[str]:
        """その日に一度でも監視対象になった銘柄."""
        out: set[str] = set()
        for _, codes in self._by_day.get(day, []):
            out |= codes
        return frozenset(out)


def _to_sec(hhmmss: str) -> int:
    h, m, s = hhmmss.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def load_watch_timeline(log_path: str | Path = "logs/engine.log") -> WatchTimeline:
    by_day: dict[str, list[tuple[int, frozenset[str]]]] = {}
    path = Path(log_path)
    if not path.exists():
        return WatchTimeline({})
    with path.open(errors="replace") as f:
        for line in f:
            m = _START.match(line) or _UPDATE.match(line)
            if not m:
                continue
            day, hhmmss, raw = m.group(1), m.group(2), m.group(3)
            try:
                codes = frozenset(str(c) for c in ast.literal_eval(raw))
            except (ValueError, SyntaxError):
                continue  # ログが途中で切れている行は捨てる
            if not codes:
                continue
            by_day.setdefault(day, []).append((_to_sec(hhmmss), codes))
    for entries in by_day.values():
        entries.sort(key=lambda e: e[0])
    return WatchTimeline(by_day)


if __name__ == "__main__":
    tl = load_watch_timeline()
    for day in tl.days:
        at9 = tl.codes_at(day, "09:05:00")
        at14 = tl.codes_at(day, "14:00:00")
        print(f"{day}: 9:05に{len(at9)}銘柄 / 14:00に{len(at14)}銘柄 / "
              f"その日に一度でも監視 {len(tl.all_codes(day))}銘柄")
