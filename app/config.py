"""Загрузка config.yaml. Все параметры (модель, пороги, пути, роли) берутся отсюда."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
_ENV_RE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"'))


@lru_cache
def get_config() -> dict:
    _load_dotenv(ROOT / ".env")
    path = Path(os.environ.get("APP_CONFIG", ROOT / "config.yaml"))
    with open(path, encoding="utf-8") as f:
        cfg = _expand(yaml.safe_load(f))
    return cfg


def path(key: str) -> Path:
    """Путь из секции paths, относительный к корню репозитория."""
    p = Path(get_config()["paths"][key])
    return p if p.is_absolute() else ROOT / p
