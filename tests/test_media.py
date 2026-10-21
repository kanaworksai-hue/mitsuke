"""EXR / Radiance HDR previews and video segmentation (need OpenCV; skipped without it)."""

import numpy as np
import pytest

import mitsuke  # noqa: F401 — sets OPENCV_IO_ENABLE_OPENEXR before cv2 is imported
from mitsuke.decode import load_preview, tonemap

cv2 = pytest.importorskip("cv2")


def _hdr_image(h=96, w=128):
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.zeros((h, w, 3), np.float32)
    img[..., 0] = x / w * 0.5  # B
    img[..., 2] = y / h * 0.5  # R
    img[h // 3 : h // 2, w // 3 : w // 2] = 40.0  # a bright "sun" far above 1.0
    return img


@pytest.mark.parametrize("ext", [".exr", ".hdr"])
def test_hdr_formats_tonemap(tmp_path, ext):
    p = tmp_path / f"scene{ext}"
    assert cv2.imwrite(str(p), _hdr_image())
    pv = load_preview(p, 64)
    assert pv.method == "hdr-tonemap" and (pv.width, pv.height) == (128, 96)
    a = np.asarray(pv.image)
    assert a.shape == (48, 64, 3) and a.dtype == np.uint8
    assert a[20, 26].min() > 240  # the sun is white, not clipped garbage
    assert 20 < a.mean() < 235  # auto exposure: neither black nor blown out


def test_tonemap_handles_nan_and_black():
    a = np.full((4, 4, 3), np.nan, np.float32)
    assert tonemap(a).max() == 0
    assert tonemap(np.zeros((2, 2, 3), np.float32)).max() == 0


def _write_video(path, colours, seconds_each=3, fps=10, size=(160, 96)):
    path.parent.mkdir(parents=True, exist_ok=True)
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    assert vw.isOpened()
    rng = np.random.default_rng(0)
    for c in colours:
        for _ in range(seconds_each * fps):
            f = np.empty((size[1], size[0], 3), np.uint8)
            f[:] = c
            f += rng.integers(0, 6, f.shape, dtype=np.uint8)
            vw.write(f)
    vw.release()
    return path


def test_video_segments(tmp_path):
    from mitsuke.video import load_segments

    p = _write_video(tmp_path / "clip.mp4", [(200, 30, 30), (30, 200, 30), (30, 30, 200)])
    info, segs = load_segments(str(p))
    assert (info.width, info.height) == (160, 96) and abs(info.duration - 9) < 0.5
    assert len(segs) == 3
    assert [round(s.t0) for s in segs] == [0, 3, 6] and abs(segs[-1].t1 - 9) < 0.6
    assert all(s.t0 <= s.tm <= s.t1 for s in segs)


def test_long_shot_is_split(tmp_path):
    from mitsuke.video import MAX_SEGMENT, load_segments

    p = _write_video(tmp_path / "long.mp4", [(90, 90, 90)], seconds_each=20)
    _, segs = load_segments(str(p))
    assert len(segs) >= 3 and all(s.t1 - s.t0 <= MAX_SEGMENT + 1 for s in segs)


def test_video_index_and_search(tmp_path, capsys):
    import json

    from mitsuke.cli import main
    from conftest import make_image

    lib = tmp_path / "lib"
    _write_video(lib / "shots.mp4", [(200, 30, 30), (30, 200, 30)])
    make_image(lib / "still.png", seed=3)
    assert main(["--json", "add", str(lib)]) == 0
    assert main(["--json", "index"]) == 0
    capsys.readouterr()
    main(["--json", "search", "anything", "-k", "10"])
    res = json.loads(capsys.readouterr().out)["results"]
    vids = [r for r in res if r["kind"] == "video"]
    assert len(res) == 2 and len(vids) == 1  # one hit per file, not one per shot
    v = vids[0]
    assert v["duration"] > 5 and len(v["shots"]) == 2 and {"time", "t0", "t1", "shot", "poster"} <= set(v)
    main(["--json", "search", "x", "--kind", "image"])
    assert {r["kind"] for r in json.loads(capsys.readouterr().out)["results"]} == {"png"}
    main(["--json", "search", "x", "--kind", "video"])
    assert {r["kind"] for r in json.loads(capsys.readouterr().out)["results"]} == {"video"}
    # `similar` takes a video shot like every other image reference
    shot = v["shots"][1]["shot"]
    main(["--json", "similar", f"s{shot}"])
    out = json.loads(capsys.readouterr().out)
    assert out["shot"] == shot and out["image"] == v["path"] and out["in_library"]
    vid = [r for r in out["results"] if r["kind"] == "video"]
    assert len(out["results"]) == 2 and vid[0]["shot"] != shot  # the query shot itself is left out


def test_black_frames_are_skipped(tmp_path):
    from mitsuke.video import _dark, load_segments

    # black leader, a red shot that fades to black halfway, then a green shot
    p = _write_video(tmp_path / "fade.mp4", [(0, 0, 0), (30, 30, 200), (0, 0, 0), (30, 200, 30)])
    _, segs = load_segments(str(p))
    assert segs and not any(_dark(s.image) for s in segs)
    assert segs[0].t0 >= 2.5  # the black leader is gone

    p = _write_video(tmp_path / "black.mp4", [(0, 0, 0)], seconds_each=4)
    _, segs = load_segments(str(p))
    assert len(segs) >= 1  # an all-black video still gets a shot
