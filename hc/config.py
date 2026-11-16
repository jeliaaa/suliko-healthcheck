"""Configuration: config.toml (non-secret) + .env (secrets) + HC_* env overrides."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path) -> None:
    """Minimal .env parser. Real environment variables win over the file."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _coerce(raw: str, like: Any) -> Any:
    if isinstance(like, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int):
        return int(raw)
    if isinstance(like, float):
        return float(raw)
    if isinstance(like, list):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


class Config:
    def __init__(self, data: dict[str, Any]):
        self._data = data

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Config":
        load_env_file(ROOT / ".env")
        path = config_path or ROOT / "config.toml"
        with open(path, "rb") as fh:
            return cls(tomllib.load(fh))

    def get(self, section: str, key: str, default: Any = None) -> Any:
        base = self._data.get(section, {}).get(key, default)
        override = os.environ.get(f"HC_{section}_{key}".upper())
        if override not in (None, ""):
            try:
                return _coerce(override, base)
            except ValueError:
                return base
        return base

    def path(self, section: str, key: str) -> Path:
        p = Path(self.get(section, key))
        return p if p.is_absolute() else ROOT / p


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()
