import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own index + cache and the fake embedder (no model, no GPU)."""
    monkeypatch.setenv("MITSUKE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MITSUKE_CACHE", raising=False)
    monkeypatch.setenv("MITSUKE_EMBEDDER", "fake")
    return tmp_path / "home"


def make_image(path, seed=0, size=(160, 120), mode="RGB", fmt=None):
    """A distinct, deterministic picture per seed (random 4x4 colour blocks upscaled)."""
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 256, (4, 4, 3), dtype=np.uint8)
    im = Image.fromarray(a).resize(size, Image.Resampling.NEAREST)
    if mode != "RGB":
        im = im.convert(mode)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format=fmt)
    return path


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "lib"
    for i in range(6):
        make_image(root / f"set{i % 2}" / f"img{i}.png", seed=i)
    make_image(root / "photo.jpg", seed=10)
    make_image(root / ".hidden" / "skip.png", seed=11)
    make_image(root / "node_modules" / "skip.png", seed=12)
    (root / "notes.txt").write_text("not an image " * 50)
    return root
