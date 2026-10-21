"""`mitsuke index --watch`: FSEvents (via watchdog) → debounce → sync + embed.

Changes are collected per root and handled once the folder has been quiet for `debounce` seconds, so a file
that is still being written (a big PSD save, a camera import) is picked up once, after it is complete.
Without watchdog it falls back to polling (a stat walk of every root).
Roots that are offline (an unplugged drive) or that vanish and come back are noticed by a cheap check every
`recheck` seconds: they are watched again and rescanned once they are back.
"""

from __future__ import annotations

import os
import threading
import time

from .paths import nfc
from .scan import is_candidate


class ChangeCollector:
    """Thread-safe set of dirty roots with a debounce clock. Fed by watchdog events (or by tests directly)."""

    def __init__(self, roots: dict[int, str], debounce: float = 2.0):
        self.roots = {rid: nfc(p).rstrip("/") + "/" for rid, p in roots.items()}  # FSEvents may report NFD names
        self.debounce = debounce
        self.lock = threading.Lock()
        self.dirty: set[int] = set()
        self.last_event = 0.0

    def root_of(self, path: str) -> int | None:
        best = None
        for rid, root in self.roots.items():
            if path.startswith(root) and (best is None or len(root) > len(self.roots[best])):
                best = rid
        return best

    def note(self, *paths: str, is_dir: bool = False):
        for p in paths:
            if not p:
                continue
            p = nfc(p)
            rid = self.root_of(p)
            if rid is None:
                continue
            rel = p[len(self.roots[rid]):]
            if not is_dir and not is_candidate(rel):
                continue
            with self.lock:
                self.dirty.add(rid)
                self.last_event = time.monotonic()

    def take(self) -> set[int]:
        """Dirty roots whose last event is older than the debounce window (empty set otherwise)."""
        with self.lock:
            if not self.dirty or time.monotonic() - self.last_event < self.debounce:
                return set()
            out, self.dirty = self.dirty, set()
            return out


def _handler(collector: ChangeCollector):
    from watchdog.events import FileSystemEventHandler

    class H(FileSystemEventHandler):
        def on_any_event(self, event):
            if event.event_type in ("opened", "closed_no_write"):
                return
            collector.note(event.src_path, getattr(event, "dest_path", ""), is_dir=event.is_directory)

    return H()


def _identity(path: str):
    """(device, inode) of a folder, or None when it is not there (offline drive, renamed away)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if os.path.isdir(path) else None


def run(store, roots: dict[int, str], on_change, debounce: float = 2.0, poll: float = 30.0, stop: threading.Event | None = None,
        recheck: float = 5.0):
    """Block until `stop` is set (or Ctrl-C). `on_change(set_of_root_ids)` runs on the calling thread.
    `roots` may include offline folders: they are picked up when they come (back) online."""
    stop = stop or threading.Event()
    collector = ChangeCollector(roots, debounce)
    observer = None
    seen = {rid: _identity(p) for rid, p in roots.items()}
    try:
        from watchdog.observers import Observer

        observer = Observer()
        h = _handler(collector)

        def schedule():
            observer.unschedule_all()
            for rid, p in roots.items():
                if seen[rid] is not None:
                    try:
                        observer.schedule(h, p, recursive=True)
                    except OSError:  # gone again in the meantime; the next recheck sees it
                        seen[rid] = None

        schedule()
        observer.start()
        mode = "fsevents"
    except ImportError:
        mode = "poll"
    last_poll = last_check = time.monotonic()
    try:
        while not stop.is_set():
            stop.wait(0.25)
            dirty = collector.take()
            now = time.monotonic()
            if mode == "poll" and now - last_poll >= poll:
                dirty = {rid for rid, p in roots.items() if os.path.isdir(p)}
                last_poll = now
            if now - last_check >= recheck:
                last_check = now
                ids = {rid: _identity(p) for rid, p in roots.items()}
                changed = {rid for rid in roots if ids[rid] != seen[rid]}
                if changed:
                    # a folder that went away and came back is a new FSEvents stream: watch it again, rescan it
                    seen.update(ids)
                    if observer is not None:
                        schedule()
                    dirty |= {rid for rid in changed if ids[rid] is not None}
            if dirty:
                on_change(dirty)
    finally:
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
    return mode
