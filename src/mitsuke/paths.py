"""Where Mitsuke keeps its data.

- Index (SQLite):  ~/Library/Application Support/Mitsuke/index.sqlite   (override: MITSUKE_HOME)
- Cache (thumbnails, vector matrix): ~/Library/Caches/Mitsuke              (override: MITSUKE_CACHE,
  or <MITSUKE_HOME>/cache when only MITSUKE_HOME is set, so one variable isolates everything)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def home_dir() -> Path:
    env = os.environ.get("MITSUKE_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Mitsuke"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "mitsuke"


def cache_dir() -> Path:
    env = os.environ.get("MITSUKE_CACHE")
    if env:
        return Path(env).expanduser()
    if os.environ.get("MITSUKE_HOME"):
        return home_dir() / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/Mitsuke"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mitsuke"


def db_path() -> Path:
    return home_dir() / "index.sqlite"


def thumbs_dir() -> Path:
    return cache_dir() / "thumbs"
