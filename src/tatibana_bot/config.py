"""設定ファイル (config/config.yaml) の読み込み."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATHS = [
    Path("config/config.yaml"),
    Path("config/config.example.yaml"),
]


class Config:
    """dot アクセスできる薄い設定ラッパー."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getattr__(self, key: str) -> Any:
        try:
            value = self._data[key]
        except KeyError as e:
            raise AttributeError(f"config key not found: {key}") from e
        if isinstance(value, dict):
            return Config(value)
        return value

    def get(self, key: str, default: Any = None) -> Any:
        value = self._data.get(key, default)
        if isinstance(value, dict):
            return Config(value)
        return value

    def to_dict(self) -> dict[str, Any]:
        return self._data


def load_config(path: str | Path | None = None) -> Config:
    # .env ファイルがあれば環境変数として読み込む (認証情報用)
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    if path is not None:
        candidates = [Path(path)]
    else:
        candidates = DEFAULT_CONFIG_PATHS
    for p in candidates:
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return Config(yaml.safe_load(f))
    raise FileNotFoundError(
        f"config file not found (tried: {[str(p) for p in candidates]}). "
        "Copy config/config.example.yaml to config/config.yaml."
    )


def env(key: str) -> str:
    """必須の環境変数を取得。無ければ明示的に落とす."""
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f"environment variable {key} is not set (see .env.example)")
    return value
