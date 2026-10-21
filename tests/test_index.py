import os
import shutil

import numpy as np
import pytest

from conftest import make_image
from mitsuke import indexer, thumbs
from mitsuke.model import FakeEmbedder
from mitsuke.search import image_vector, rank, search_text
from mitsuke.store import Store


class CountingEmbedder(FakeEmbedder):
    def __init__(self, fail_after=None):
        super().__init__()
        self.calls = 0
        self.images = 0
        self.fail_after = fail_after

    def embed_images(self, images, tokens=140):
        if self.fail_after is not None and self.images >= self.fail_after:
            raise KeyboardInterrupt
        self.calls += 1
        self.images += len(images)
        return super().embed_images(images, tokens)


def build(root, emb=None):
    s = Store()
    s.add_root(os.path.realpath(root))
    emb = emb or CountingEmbedder()
    st = indexer.sync(s)
    counts = indexer.embed_pending(s, emb, 140)
    return s, emb, st, counts


def status_counts(s):
    return dict(s.db.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())


def test_index_and_search(library):
    s, emb, st, counts = build(library)
    assert st.new == 7 and counts["ok"] == 7 and emb.images == 7
    assert status_counts(s) == {"ok": 7}
    # thumbnails exist for every file
    for (sha,) in s.db.execute("SELECT sha1 FROM files"):
        assert thumbs.thumb_path(sha).exists()
    hits = search_text(s, emb, "anything", k=3)
    assert len(hits) == 3 and hits[0]["score"] >= hits[1]["score"]
    # second run: nothing to do
    st2 = indexer.sync(s)
    assert not st2.any() and st2.unchanged == 7 and indexer.pending_count(s) == 0


def test_similar_inside_and_outside(library, tmp_path):
    s, emb, _, _ = build(library)
    target = os.path.realpath(library / "set0" / "img2.png")
    fid, path, vec = image_vector(s, "fake", target, lambda: pytest.fail("model must not load for library files"), 140)
    assert fid is not None
    hits = rank(s, "fake", vec, k=3, exclude={fid})
    assert all(h["id"] != fid for h in hits)
    # an outside copy of the same picture finds the original first
    outside = tmp_path / "outside.png"
    shutil.copy(target, outside)
    fid2, _, vec2 = image_vector(s, "fake", str(outside), lambda: emb, 140)
    assert fid2 is None
    assert rank(s, "fake", vec2, k=1)[0]["path"] == target


def test_filters(library):
    s, emb, _, _ = build(library)
    q = emb.embed_texts(["x"])[0]
    assert {h["kind"] for h in rank(s, "fake", q, k=20, kinds=["jpeg"])} == {"jpeg"}
    sub = os.path.realpath(library / "set1")
    hits = rank(s, "fake", q, k=20, root=sub)
    assert len(hits) == 3 and all(h["path"].startswith(sub + "/") for h in hits)


def test_rename_move_keep_vectors(library):
    s, emb, _, _ = build(library)
    before = {r["sha1"]: r["id"] for r in s.db.execute("SELECT id, sha1 FROM files")}
    (library / "set0" / "img0.png").rename(library / "set0" / "renamed.png")
    (library / "moved").mkdir()
    (library / "set1" / "img1.png").rename(library / "moved" / "img1.png")
    st = indexer.sync(s)
    assert (st.moved, st.new, st.deleted) == (2, 0, 0)
    assert indexer.pending_count(s) == 0
    after = {r["sha1"]: r["id"] for r in s.db.execute("SELECT id, sha1 FROM files")}
    assert after == before  # same rows, same vectors
    assert s.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 7


def test_move_between_roots(library, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    s, emb, _, _ = build(library)
    s.add_root(os.path.realpath(other))
    (library / "photo.jpg").rename(other / "photo.jpg")
    st = indexer.sync(s)
    assert st.moved == 1 and indexer.pending_count(s) == 0


def test_modify_and_delete(library):
    s, emb, _, _ = build(library)
    p = library / "set0" / "img4.png"
    old_sha = s.db.execute("SELECT sha1 FROM files WHERE relpath='set0/img4.png'").fetchone()[0]
    make_image(p, seed=99)
    os.utime(p, ns=(1, 1))  # make sure mtime differs even on coarse clocks
    (library / "photo.jpg").unlink()
    st = indexer.sync(s)
    assert (st.changed, st.deleted) == (1, 1)
    assert not thumbs.thumb_path(old_sha).exists()
    n = emb.images
    indexer.embed_pending(s, emb, 140)
    assert emb.images == n + 1
    assert status_counts(s) == {"ok": 6}
    new_sha = s.db.execute("SELECT sha1 FROM files WHERE relpath='set0/img4.png'").fetchone()[0]
    assert new_sha != old_sha and thumbs.thumb_path(new_sha).exists()


def test_touch_without_content_change(library):
    s, emb, _, _ = build(library)
    os.utime(library / "photo.jpg", ns=(5, 5))
    st = indexer.sync(s)
    assert st.touched == 1 and indexer.pending_count(s) == 0


def test_resume_after_interrupt(tmp_path):
    root = tmp_path / "big"
    for i in range(40):
        make_image(root / f"{i:02d}.png", seed=100 + i)
    s = Store()
    s.add_root(os.path.realpath(root))
    indexer.sync(s)
    with pytest.raises(KeyboardInterrupt):
        indexer.embed_pending(s, CountingEmbedder(fail_after=24), 140)
    done = status_counts(s).get("ok", 0)
    assert 0 < done <= 24 and indexer.pending_count(s) == 40 - done
    # a fresh process continues where it stopped, embedding only the rest
    s2 = Store()
    emb2 = CountingEmbedder()
    indexer.sync(s2)
    indexer.embed_pending(s2, emb2, 140)
    assert emb2.images == 40 - done
    assert status_counts(s2) == {"ok": 40}
    assert s2.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 40


def test_errors_and_retry(library, monkeypatch):
    monkeypatch.setattr("mitsuke.decode.sys.platform", "linux")  # no sips fallback
    bad = library / "broken.jpg"
    bad.write_bytes(b"\xff\xd8\xff" + b"x" * 1000)
    from PIL import Image

    noise = np.random.default_rng(0).integers(0, 256, (20, 30, 3), dtype=np.uint8)
    Image.fromarray(noise).save(library / "tiny.png")  # noisy so the file is above the scanner's byte minimum
    s, emb, _, counts = build(library)
    assert counts["error"] == 2
    errs = dict(s.db.execute("SELECT relpath, error FROM files WHERE status='error'").fetchall())
    assert "too small" in errs["tiny.png"]
    # fixing the file makes the next sync retry it
    make_image(bad, seed=7, fmt="JPEG")
    st = indexer.sync(s)
    assert st.changed == 1
    indexer.embed_pending(s, emb, 140)
    assert status_counts(s) == {"ok": 8, "error": 1}


def test_offline_root_is_kept(library, tmp_path):
    s, emb, _, _ = build(library)
    moved = tmp_path / "unplugged"
    library.rename(moved)
    st = indexer.sync(s)
    assert st.offline and st.deleted == 0
    assert status_counts(s) == {"ok": 7}


def test_duplicates_reuse_vectors(library):
    s, emb, _, _ = build(library)
    shutil.copy(library / "photo.jpg", library / "photo copy.jpg")
    indexer.sync(s)
    n = emb.images
    counts = indexer.embed_pending(s, emb, 140)
    assert counts["reused"] == 1 and emb.images == n


def test_quality_change_requeues(library):
    s, emb, _, _ = build(library)
    assert indexer.requeue_for(s, "fake", 140) == 0
    assert indexer.requeue_for(s, "fake", 280) == 7
    # old vectors stay searchable while re-indexing
    assert len(rank(s, "fake", emb.embed_texts(["x"])[0], k=20)) == 7
    indexer.embed_pending(s, emb, 280)
    assert {r[0] for r in s.db.execute("SELECT tokens FROM vectors")} == {280}


def test_matrix_cache_invalidates(library):
    s, emb, _, _ = build(library)
    ids, mat = s.matrix("fake")
    assert len(ids) == 7 and mat.dtype == np.float16
    (library / "photo.jpg").unlink()
    indexer.sync(s)
    ids2, _ = s.matrix("fake")
    assert len(ids2) == 6


def test_remove_root(library):
    s, emb, _, _ = build(library)
    rid = s.roots()[0]["id"]
    assert s.remove_root(rid) == 7
    assert s.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0
    assert (library / "photo.jpg").exists()
