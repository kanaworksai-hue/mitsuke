"""Turn any supported image file into a small RGB preview for embedding and thumbnails.

Strategy per format (fastest path first, falling back to a full decode):
- JPEG: Pillow draft mode (DCT scaling) so a 24 MP photo decodes at ~1/4 size.
- HEIC/HEIF/AVIF: pillow-heif / Pillow.
- GIF / animated WebP: first frame.
- PSD: Pillow reads the merged composite; PSB and high bit depth go through psd-tools.
- Camera RAW: the embedded JPEG preview via rawpy; if missing or tiny, a half-size demosaic.
- OpenEXR / Radiance HDR: OpenCV float decode, auto exposure + filmic tone map to sRGB.
- Anything a decoder above cannot open: macOS `sips` (ImageIO) as a last resort.
EXIF orientation is applied everywhere; CMYK with an embedded ICC profile is colour-managed to sRGB.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # optional
    pillow_heif = None

Image.MAX_IMAGE_PIXELS = 400_000_000  # large PSB / panoramas are normal for artists

RAW_EXTS = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw", ".3fr", ".iiq", ".rwl", ".x3f"}
PSD_EXTS = {".psd", ".psb"}
PIL_EXTS = {".jpg", ".jpeg", ".jpe", ".png", ".webp", ".heic", ".heif", ".hif", ".avif", ".tif", ".tiff", ".gif", ".bmp", ".jp2", ".tga"}
HDR_EXTS = {".exr", ".hdr"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
IMAGE_EXTS = RAW_EXTS | PSD_EXTS | PIL_EXTS | HDR_EXTS
SUPPORTED_EXTS = IMAGE_EXTS | VIDEO_EXTS


@dataclass
class Preview:
    image: Image.Image  # RGB, longest side <= max_side
    width: int  # original pixel size (0 if unknown)
    height: int
    method: str  # how it was obtained, for doctor/debugging


def _fit(im: Image.Image, max_side: int) -> Image.Image:
    if max(im.size) > max_side:
        im = im.copy()
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return im


def _cmyk_to_rgb(im: Image.Image, icc: bytes | None) -> Image.Image:
    if icc:
        try:
            from PIL import ImageCms

            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            return ImageCms.profileToProfile(im, src, ImageCms.createProfile("sRGB"), outputMode="RGB")
        except Exception:  # noqa: BLE001 — broken / unsupported profile: plain conversion below
            pass
    return im.convert("RGB")


def _to_rgb(im: Image.Image, icc: bytes | None = None) -> Image.Image:
    if im.mode == "RGB":
        return im
    if im.mode == "CMYK":
        return _cmyk_to_rgb(im, icc)
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        a = np.asarray(im, dtype=np.float32)
        a = a / (65535.0 if a.max() > 255 else 255.0)
        return Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8)).convert("RGB")
    if im.mode == "F":
        a = np.asarray(im, dtype=np.float32)
        return Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8)).convert("RGB")
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        # Composite transparent art onto neutral grey so sprites keep their silhouette.
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (128, 128, 128, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return im.convert("RGB")


def _open_pil(path: Path, max_side: int) -> Preview:
    im = Image.open(path)
    w, h = im.size
    icc = im.info.get("icc_profile")
    method = "pil"
    if im.format == "JPEG":
        im.draft("RGB", (max_side, max_side))
        method = "pil-draft"
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
        method += "-frame0"
    im = ImageOps.exif_transpose(im)
    return Preview(_fit(_to_rgb(im, icc), max_side), w, h, method)


def _open_raw(path: Path, max_side: int) -> Preview:
    import rawpy

    with rawpy.imread(str(path)) as raw:
        w, h = raw.sizes.width, raw.sizes.height
        flip = raw.sizes.flip
        try:
            thumb = raw.extract_thumb()
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            thumb = None
        im = None
        if thumb is not None:
            if thumb.format == rawpy.ThumbFormat.JPEG:
                im = Image.open(io.BytesIO(thumb.data))
                im.draft("RGB", (max_side, max_side))
                transposed = ImageOps.exif_transpose(im)
                if transposed.size == im.size and flip:
                    transposed = _apply_libraw_flip(transposed, flip)
                im = transposed
            else:
                im = _apply_libraw_flip(Image.fromarray(thumb.data), flip)
        if im is not None and max(im.size) >= min(512, max_side):
            return Preview(_fit(_to_rgb(im), max_side), w, h, "raw-embedded")
        rgb = raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8, no_auto_bright=False)
        return Preview(_fit(Image.fromarray(rgb), max_side), w, h, "raw-demosaic")


def _apply_libraw_flip(im: Image.Image, flip: int) -> Image.Image:
    # LibRaw flip codes: 3 = 180, 5 = 90 CCW, 6 = 90 CW
    if flip == 3:
        return im.transpose(Image.Transpose.ROTATE_180)
    if flip == 5:
        return im.transpose(Image.Transpose.ROTATE_90)
    if flip == 6:
        return im.transpose(Image.Transpose.ROTATE_270)
    return im


def _psd_header(path: Path) -> tuple[int, int]:
    """(channels, colour mode) from the 26-byte PSD header."""
    with open(path, "rb") as f:
        h = f.read(26)
    return int.from_bytes(h[12:14], "big"), int.from_bytes(h[24:26], "big")


def _open_psd(path: Path, max_side: int) -> Preview:
    channels, mode = _psd_header(path)
    # Pillow misreads CMYK files with spot channels (extra channels end up as cyan): use psd-tools for those.
    if path.suffix.lower() == ".psd" and not (mode == 4 and channels > 5):
        try:
            im = Image.open(path)
            if im.mode in ("RGB", "RGBA", "L", "CMYK", "LA", "P"):
                w, h = im.size
                im.load()
                return Preview(_fit(_to_rgb(im), max_side), w, h, "psd-composite")
        except Exception:
            pass
    from psd_tools import PSDImage

    psd = PSDImage.open(path)
    im = psd.topil()  # merged image data stored in the file; no layer re-render
    method = "psd-tools-merged"
    if im is None:
        im = psd.composite()
        method = "psd-tools-composite"
    return Preview(_fit(_to_rgb(im), max_side), psd.width, psd.height, method)


def _open_sips(path: Path, max_side: int) -> Preview:
    with tempfile.TemporaryDirectory(prefix="mitsuke-sips") as td:
        out = Path(td) / "preview.png"
        subprocess.run(
            ["sips", "-s", "format", "png", "-Z", str(max_side), str(path), "--out", str(out)],
            capture_output=True, timeout=120, check=True,
        )
        im = Image.open(out)
        im.load()
    return Preview(_to_rgb(im), 0, 0, "sips")


def tonemap(rgb: np.ndarray) -> np.ndarray:
    """Scene-linear float RGB → 8-bit sRGB: exposure from the log-average luminance (key 0.18), ACES-fitted
    curve, sRGB transfer. Good enough to *recognise* the picture; this is a search preview, not a grade.
    Brightening is capped at one stop so a night shot or a candle-lit room stays dark (a search for "night" must
    find it); only files that are clearly unscaled (99th percentile below 0.05) get the full lift."""
    rgb = np.nan_to_num(np.asarray(rgb, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0).clip(0, None)
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    valid = lum[lum > 1e-6]
    avg = float(np.exp(np.mean(np.log(valid)))) if valid.size else 1.0
    gain = 0.18 / max(avg, 1e-6)
    if lum.size and float(np.percentile(lum, 99)) >= 0.05:
        gain = min(gain, 2.0)
    x = rgb * gain
    x = (x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14)
    x = np.clip(x, 0, 1)
    x = np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)
    return (np.clip(x, 0, 1) * 255 + 0.5).astype(np.uint8)


def _open_hdr(path: Path, max_side: int) -> Preview:
    import cv2

    a = cv2.imread(str(path), cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH | cv2.IMREAD_ANYCOLOR)
    if a is None:
        raise ValueError("OpenCV could not read this file")
    h, w = a.shape[:2]
    if max(h, w) > max_side:
        s = max_side / max(h, w)
        a = cv2.resize(a, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    a = a.astype(np.float32)
    if a.ndim == 2:
        a = np.repeat(a[:, :, None], 3, axis=2)
    alpha = a[:, :, 3:4].clip(0, 1) if a.shape[2] == 4 else None
    rgb = a[:, :, 2::-1] if a.shape[2] >= 3 else np.repeat(a[:, :, :1], 3, axis=2)  # BGR(A) → RGB
    out = tonemap(rgb)
    if alpha is not None:  # premultiplied in EXR: put it over the same neutral grey as other transparent art
        out = (out * alpha + 128 * (1 - alpha)).astype(np.uint8)
    return Preview(Image.fromarray(np.ascontiguousarray(out)), w, h, "hdr-tonemap")


def load_preview(path: str | Path, max_side: int = 1024) -> Preview:
    path = Path(path)
    ext = path.suffix.lower()
    try:
        if ext in HDR_EXTS:
            return _open_hdr(path, max_side)
        if ext in RAW_EXTS:
            return _open_raw(path, max_side)
        if ext in PSD_EXTS:
            return _open_psd(path, max_side)
        return _open_pil(path, max_side)
    except Exception:
        if sys.platform == "darwin" and shutil.which("sips"):
            try:
                return _open_sips(path, max_side)
            except Exception:  # noqa: BLE001 — report the original decoder error, it is more useful
                pass
        raise
