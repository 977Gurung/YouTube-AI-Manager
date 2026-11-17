"""Configuration: config/settings.yaml, config/channels.yaml and .env."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("YTM_CONFIG_DIR", ROOT / "config"))
load_dotenv(ROOT / ".env")


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Settings:
    """Dotted-path access to settings.yaml, e.g. settings.get('llm.model')."""

    def __init__(self) -> None:
        self._data: dict = {}
        self.reload()

    def reload(self) -> None:
        self._data = _read_yaml(CONFIG_DIR / "settings.yaml")

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def raw(self) -> dict:
        return self._data


settings = Settings()


def env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def resolve_path(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (ROOT / p).resolve()


def data_dir() -> Path:
    d = resolve_path(settings.get("app.data_dir", "./data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    d = ROOT / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_channels() -> list[dict]:
    chans = _read_yaml(CONFIG_DIR / "channels.yaml").get("channels", []) or []
    for ch in chans:
        if "slug" not in ch or "name" not in ch:
            raise ValueError(f"Every channel in channels.yaml needs 'slug' and 'name': {ch}")
    return chans
