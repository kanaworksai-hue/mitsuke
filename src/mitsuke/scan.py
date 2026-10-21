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
SAMPLE_BYTES = 1 << 16


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


def content_hashes(path: str | Path, size: int | None = None) -> tuple[str, str]:
    """(hash, legacy hash). The hash covers the first and last 1 MB, 8 blocks of 64 KB spread in between, and
    the size: cheap, stable across renames / moves, and it changes when an uncompressed file (BMP, TIFF, EXR) is
    edited in place at the same size — those keep their first rows. The legacy hash (first 1 MB + size, used
    before) only matters for pairing moves of files indexed by an older version. Up to 1 MB both are the same."""
    if size is None:
        size = os.path.getsize(path)
    h = hashlib.sha1()
    with open(path, "rb") as f:
        head = f.read(HEAD_BYTES)
        h.update(head)
        legacy = h.copy()
        if size > HEAD_BYTES:
            for i in range(1, 9):
                f.seek(HEAD_BYTES + (size - 2 * HEAD_BYTES) * i // 9)
                h.update(f.read(SAMPLE_BYTES))
            f.seek(max(HEAD_BYTES, size - HEAD_BYTES))
            h.update(f.read(HEAD_BYTES))
    h.update(str(size).encode())
    legacy.update(str(size).encode())
    return h.hexdigest(), legacy.hexdigest()


def content_hash(path: str | Path, size: int | None = None) -> str:
    return content_hashes(path, size)[0]
