import os

from conftest import make_image
from mitsuke.scan import content_hash, is_candidate, kind_of, walk


def test_walk_filters(library):
    found = sorted(r for r, _, _ in walk(library))
    assert found == ["photo.jpg", "set0/img0.png", "set0/img2.png", "set0/img4.png", "set1/img1.png", "set1/img3.png", "set1/img5.png"]


def test_walk_symlink_loop(tmp_path):
    make_image(tmp_path / "r" / "a.png")
    os.symlink(tmp_path / "r", tmp_path / "r" / "loop")
    assert [r for r, _, _ in walk(tmp_path / "r")] == ["a.png"]


def test_hash_survives_rename(tmp_path):
    p = make_image(tmp_path / "a.png", seed=1)
    h = content_hash(p)
    p.rename(tmp_path / "b.png")
    assert content_hash(tmp_path / "b.png") == h
    make_image(tmp_path / "c.png", seed=2)
    assert content_hash(tmp_path / "c.png") != h


def test_kinds_and_candidates():
    assert kind_of("x/IMG_1.CR3") == "raw" and kind_of("a.PSB") == "psd" and kind_of("a.JPEG") == "jpeg" and kind_of("a.HIF") == "heic"
    assert is_candidate("a/b.png") and not is_candidate(".git/b.png") and not is_candidate("a/b.txt") and not is_candidate("x.app/c.png")
