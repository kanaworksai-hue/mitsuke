import os
import threading
import time

import pytest

from conftest import make_image
from mitsuke import watch


def test_collector_debounce(tmp_path):
    c = watch.ChangeCollector({1: str(tmp_path / "a"), 2: str(tmp_path / "a" / "b")}, debounce=0.2)
    c.note(str(tmp_path / "a" / "x.txt"))  # not an image
    c.note(str(tmp_path / "elsewhere" / "x.png"))  # outside every root
    assert not c.dirty
    c.note(str(tmp_path / "a" / "b" / "x.png"))
    assert c.dirty == {2}  # longest root wins
    assert c.take() == set()  # still inside the debounce window
    time.sleep(0.25)
    assert c.take() == {2} and c.take() == set()
    c.note(str(tmp_path / "a" / "newdir"), is_dir=True)
    assert c.dirty == {1}
    import unicodedata

    c = watch.ChangeCollector({1: unicodedata.normalize("NFC", str(tmp_path / "ガ"))}, debounce=0)
    c.note(unicodedata.normalize("NFD", str(tmp_path / "ガ" / "x.png")))  # FSEvents may report decomposed names
    assert c.dirty == {1}


def test_fsevents_end_to_end(tmp_path):
    pytest.importorskip("watchdog")
    root = tmp_path / "w"
    root.mkdir()
    seen, stop = [], threading.Event()

    def on_change(dirty):
        seen.append(dirty)
        stop.set()

    t = threading.Thread(target=watch.run, args=(None, {1: os.path.realpath(root)}, on_change), kwargs={"debounce": 0.3, "stop": stop})
    t.start()
    time.sleep(1.0)  # let the observer start
    make_image(root / "new.png")
    t.join(timeout=15)
    stop.set()
    assert seen and seen[0] == {1}


def _wait_for(cond, timeout=5.0):
    t = time.monotonic()
    while not cond() and time.monotonic() - t < timeout:
        time.sleep(0.1)
    return cond()


def test_root_that_comes_back_is_watched_again(tmp_path):
    """An unplugged drive (offline at start) or a root renamed away and back: rescanned and watched once it is back."""
    pytest.importorskip("watchdog")
    root, away = tmp_path / "drive", tmp_path / "drive_away"
    seen, stop = [], threading.Event()
    t = threading.Thread(target=watch.run, args=(None, {1: os.path.realpath(tmp_path) + "/drive"}, seen.append),
                         kwargs={"debounce": 0.2, "stop": stop, "recheck": 0.2})
    t.start()
    try:
        time.sleep(0.5)
        assert seen == []
        root.mkdir()  # the drive is plugged in
        assert _wait_for(lambda: seen == [{1}])
        root.rename(away)
        time.sleep(0.6)
        away.rename(root)
        time.sleep(1.0)
        seen.clear()
        make_image(root / "after.png")  # a change after it came back still gets noticed
        assert _wait_for(lambda: {1} in seen)
    finally:
        stop.set()
        t.join(timeout=10)


def test_folder_added_elsewhere_is_picked_up(tmp_path):
    """`mitsuke add` while `serve` / `index --watch` runs: the watcher re-reads the folder list and scans the new one."""
    pytest.importorskip("watchdog")
    from mitsuke.store import Store

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    make_image(b / "x.png")
    s = Store()
    ra, _ = s.add_root(os.path.realpath(a))
    seen, stop = [], threading.Event()
    t = threading.Thread(target=watch.run, args=(s, {ra: os.path.realpath(a)}, seen.append),
                         kwargs={"debounce": 0.2, "stop": stop, "recheck": 0.2})
    t.start()
    try:
        time.sleep(0.4)
        rb, _ = s.add_root(os.path.realpath(b))  # another process adds a folder
        assert _wait_for(lambda: {rb} in seen)
        seen.clear()
        make_image(b / "y.png")  # and it is watched from then on
        assert _wait_for(lambda: {rb} in seen)
    finally:
        stop.set()
        t.join(timeout=10)
