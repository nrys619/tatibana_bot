"""日足データの取得とローカルキャッシュ.

yfinance (Yahoo Finance) から日足を取得し、data_dir/daily/{code}.parquet に保存する。
列は open/high/low/close/volume (小文字)、index は日付 (tz なし)。
東証コード ("7203" 等) は Yahoo の ".T" 付きティッカーに変換し、
"^N225" のような指数コードはそのまま使う。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_COLUMNS = ["open", "high", "low", "close", "volume"]


def _to_ticker(code: str) -> str:
    return code if code.startswith("^") else f"{code}.T"


def _cache_path(data_dir: str | Path, code: str) -> Path:
    return Path(data_dir) / "daily" / f"{code}.parquet"


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    # yfinance は単一銘柄でも (Price, Ticker) の MultiIndex 列を返すことがある
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=lambda c: str(c).lower())[_COLUMNS]
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df.index.name = "date"
    return df.dropna(how="all").sort_index()


def _fetch(code: str, days: int) -> pd.DataFrame | None:
    import yfinance as yf

    # days は営業日ベースなのでカレンダー日数に余裕を持たせて取得する
    start = pd.Timestamp.today().normalize() - pd.Timedelta(days=int(days * 1.6))
    try:
        df = yf.download(
            _to_ticker(code),
            start=start,
            interval="1d",
            auto_adjust=False,
            progress=False,
        )
    except Exception:
        logger.warning("download failed: %s", code, exc_info=True)
        return None
    if df is None or df.empty:
        logger.warning("no data returned: %s", code)
        return None
    return _normalize(df)


def update_universe(
    codes: list[str], data_dir: str | Path, days: int = 750
) -> dict[str, pd.DataFrame]:
    """codes の日足を取得してキャッシュを更新し、code -> DataFrame で返す.

    取得に失敗した銘柄は、キャッシュがあればそれで代用し、なければ結果から外す。
    """
    bars: dict[str, pd.DataFrame] = {}
    for code in codes:
        df = _fetch(code, days)
        path = _cache_path(data_dir, code)
        if df is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(path)
        elif path.exists():
            logger.warning("using stale cache for %s", code)
            df = pd.read_parquet(path)
        else:
            continue
        bars[code] = df
    logger.info("daily bars updated: %d/%d codes", len(bars), len(codes))
    return bars


def load_universe(codes: list[str], data_dir: str | Path) -> dict[str, pd.DataFrame]:
    """キャッシュ済みの日足を読む (ネットワークアクセスなし)。無い銘柄は結果から外す."""
    bars: dict[str, pd.DataFrame] = {}
    for code in codes:
        path = _cache_path(data_dir, code)
        if path.exists():
            bars[code] = pd.read_parquet(path)
    return bars
