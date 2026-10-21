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
