"""Walk a root folder and list supported image files with their (size, mtime_ns)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .decode import PSD_EXTS, RAW_EXTS, SUPPORTED_EXTS, VIDEO_EXTS

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".godot", ".import", "__pycache__", ".venv", "venv",
    "Caches", "DerivedData",  # dot-folders (.cache, .Trash, .Spotlight-V100, ...) are skipped by name
}
SKIP_DIR_SUFFIXES = (".app", ".photoslibrary", ".lrdata", ".fcpbundle", ".bundle", ".framework", ".xcassets")
MIN_BYTES = 256  # anything smaller is not a real image (64 px minimum side is checked after decode)
HEAD_BYTES = 1 << 20


def kind_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in RAW_EXTS:
        return "raw"
    if ext in PSD_EXTS:
        return "psd"
    if ext in VIDEO_EXTS:
        return "video"
    return {
        ".jpg": "jpeg", ".jpeg": "jpeg", ".jpe": "jpeg", ".tif": "tiff", ".tiff": "tiff",
        ".heic": "heic", ".heif": "heic", ".hif": "heic",
    }.get(ext, ext.lstrip("."))


def skip_dir(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIRS or name.endswith(SKIP_DIR_SUFFIXES)


def is_candidate(relpath: str) -> bool:
    parts = relpath.split("/")
    if any(skip_dir(p) for p in parts[:-1]):
        return False
    name = parts[-1]
    return not name.startswith(".") and os.path.splitext(name)[1].lower() in SUPPORTED_EXTS


def walk(root: str | Path):
    """Yield (relpath, size, mtime_ns) for supported files under root. Symlinked dirs are followed once."""
    root = str(root)
    seen = set()
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            st = os.stat(d)
        except OSError:
            continue
        key = (st.st_dev, st.st_ino)
        if key in seen:  # symlink loop / duplicate mount
            continue
        seen.add(key)
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir():
                    if not skip_dir(e.name):
                        stack.append(e.path)
                    continue
                if e.name.startswith(".") or os.path.splitext(e.name)[1].lower() not in SUPPORTED_EXTS:
                    continue
                s = e.stat()
            except OSError:
                continue
            if s.st_size < MIN_BYTES:
                continue
            yield os.path.relpath(e.path, root).replace(os.sep, "/"), s.st_size, s.st_mtime_ns


def content_hash(path: str | Path, size: int | None = None) -> str:
    """sha1 of the first 1 MB plus the file size: cheap, and stable across renames / moves."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(HEAD_BYTES))
    if size is None:
        size = os.path.getsize(path)
    h.update(str(size).encode())
    return h.hexdigest()
