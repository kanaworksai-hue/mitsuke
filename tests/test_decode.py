import numpy as np
import pytest
from PIL import Image

from conftest import make_image
from mitsuke.decode import load_preview


def test_jpeg_draft_and_size(tmp_path):
    p = make_image(tmp_path / "a.jpg", size=(3000, 2000))
    pv = load_preview(p, 512)
    assert pv.method == "pil-draft"
    assert (pv.width, pv.height) == (3000, 2000)
    assert max(pv.image.size) <= 512 and pv.image.mode == "RGB"


def test_exif_orientation(tmp_path):
    im = Image.new("RGB", (200, 100), (200, 10, 10))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 CW on display
    im.save(tmp_path / "o.jpg", exif=exif)
    assert load_preview(tmp_path / "o.jpg").image.size == (100, 200)


def test_transparent_png_on_grey(tmp_path):
    im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    im.paste((255, 0, 0, 255), (40, 40, 60, 60))
    im.save(tmp_path / "s.png")
    a = np.asarray(load_preview(tmp_path / "s.png").image)
    assert tuple(a[0, 0]) == (128, 128, 128) and tuple(a[50, 50]) == (255, 0, 0)


def test_16bit_tiff(tmp_path):
    a = (np.linspace(0, 65535, 128 * 64).reshape(64, 128)).astype(np.uint16)
    Image.fromarray(a).save(tmp_path / "t.tiff")
    out = np.asarray(load_preview(tmp_path / "t.tiff").image)
    assert out.dtype == np.uint8 and out[0, 0, 0] < 5 and out[-1, -1, 0] > 250


def test_animated_gif_first_frame(tmp_path):
    frames = [Image.new("RGB", (80, 80), c) for c in ((255, 0, 0), (0, 0, 255))]
    frames[0].save(tmp_path / "g.gif", save_all=True, append_images=frames[1:])
    pv = load_preview(tmp_path / "g.gif")
    assert pv.method.endswith("frame0")
    r, g, b = np.asarray(pv.image)[40, 40]
    assert r > 200 and b < 50


def test_cmyk_jpeg(tmp_path):
    Image.new("CMYK", (80, 80), (0, 255, 255, 0)).save(tmp_path / "c.jpg")
    r, g, b = np.asarray(load_preview(tmp_path / "c.jpg").image)[40, 40]
    assert r > 200 and g < 60 and b < 60


@pytest.mark.parametrize("ext", ["bmp", "webp", "tiff"])
def test_basic_formats(tmp_path, ext):
    p = make_image(tmp_path / f"x.{ext}", seed=3)
    assert load_preview(p).image.size == (160, 120)


def test_psd(tmp_path):
    psd_tools = pytest.importorskip("psd_tools")
    base = make_image(tmp_path / "b.png", seed=4)
    psd_tools.PSDImage.frompil(Image.open(base)).save(tmp_path / "x.psd")
    a, b = np.asarray(load_preview(tmp_path / "x.psd").image, np.float32), np.asarray(Image.open(base), np.float32)
    assert np.abs(a - b).mean() < 3


def test_heic(tmp_path):
    pytest.importorskip("pillow_heif")
    make_image(tmp_path / "x.heic", seed=5, fmt="HEIF")
    assert load_preview(tmp_path / "x.heic").image.size == (160, 120)


def test_corrupt_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("mitsuke.decode.sys.platform", "linux")  # no sips fallback
    (tmp_path / "bad.jpg").write_bytes(b"\xff\xd8\xff" + b"garbage" * 100)
    with pytest.raises(Exception):
        load_preview(tmp_path / "bad.jpg")
