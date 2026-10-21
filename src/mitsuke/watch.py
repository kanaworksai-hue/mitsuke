"""`mitsuke index --watch`: FSEvents (via watchdog) → debounce → sync + embed.

Changes are collected per root and handled once the folder has been quiet for `debounce` seconds, so a file
that is still being written (a big PSD save, a camera import) is picked up once, after it is complete.
Without watchdog it falls back to polling (a stat walk of every root).
"""

from __future__ import annotations

import threading
import time

from .scan import is_candidate


class ChangeCollector:
    """Thread-safe set of dirty roots with a debounce clock. Fed by watchdog events (or by tests directly)."""

    def __init__(self, roots: dict[int, str], debounce: float = 2.0):
        self.roots = {rid: p.rstrip("/") + "/" for rid, p in roots.items()}
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


def run(store, roots: dict[int, str], on_change, debounce: float = 2.0, poll: float = 30.0, stop: threading.Event | None = None):
    """Block until `stop` is set (or Ctrl-C). `on_change(set_of_root_ids)` runs on the calling thread."""
    stop = stop or threading.Event()
    collector = ChangeCollector(roots, debounce)
    observer = None
    try:
        from watchdog.observers import Observer

        observer = Observer()
        h = _handler(collector)
        for p in roots.values():
            observer.schedule(h, p, recursive=True)
        observer.start()
        mode = "fsevents"
    except ImportError:
        mode = "poll"
    last_poll = time.monotonic()
    try:
        while not stop.is_set():
            stop.wait(0.25)
            dirty = collector.take()
            if mode == "poll" and time.monotonic() - last_poll >= poll:
                dirty = set(roots)
                last_poll = time.monotonic()
            if dirty:
                on_change(dirty)
    finally:
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
    return mode
