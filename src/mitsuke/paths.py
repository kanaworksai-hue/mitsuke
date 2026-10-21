"""Where Mitsuke keeps its data.

- Index (SQLite):  ~/Library/Application Support/Mitsuke/index.sqlite   (override: MITSUKE_HOME)
- Cache (thumbnails, vector matrix): ~/Library/Caches/Mitsuke              (override: MITSUKE_CACHE,
  or <MITSUKE_HOME>/cache when only MITSUKE_HOME is set, so one variable isolates everything)
"""

from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path


def nfc(path: str) -> str:
    """macOS file names can be stored decomposed (NFD: カ + ゛, a + ¨) or composed (NFC) and a typed or pasted path
    may use the other form; the file system treats both as the same name, so compare paths in one form."""
    return unicodedata.normalize("NFC", path)


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
