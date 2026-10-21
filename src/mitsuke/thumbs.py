"""512 px WebP thumbnails keyed by content hash: <cache>/thumbs/<sha[:2]>/<sha>.webp (video segments: <sha>-<n>.webp)."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image

from . import paths

SIZE = 512


def thumb_path(sha1: str) -> Path:
    return paths.thumbs_dir() / sha1[:2] / f"{sha1}.webp"


def save_thumb(sha1: str, image: Image.Image) -> Path:
    p = thumb_path(sha1)
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    im = image
    if max(im.size) > SIZE:
        im = im.copy()
        im.thumbnail((SIZE, SIZE), Image.Resampling.LANCZOS)
    tmp = p.with_suffix(f".tmp{os.getpid()}")
    im.save(tmp, "WEBP", quality=80, method=4)
    os.replace(tmp, p)
    return p


def drop_thumb(sha1: str | None):
    if sha1:
        thumb_path(sha1).unlink(missing_ok=True)


def drop_shot_thumbs(sha1: str | None):
    """Video segment thumbnails are <sha>-<segment>.webp."""
    if sha1:
        for p in (paths.thumbs_dir() / sha1[:2]).glob(f"{sha1}-*.webp"):
            p.unlink(missing_ok=True)
