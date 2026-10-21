"""Keep the index in step with the folders on disk.

Two stages, both safe to interrupt at any point:
1. `sync`  — walk roots, compare (size, mtime_ns) with the index. Changed files get re-hashed (`content_hashes`):
   same hash → only the timestamp is updated; different → re-embed. Files that vanished are matched against new
   files by size + hash, so renames and moves keep their vectors. Offline roots (unplugged drives) are skipped,
   never emptied.
2. `embed_pending` — decode in a thread pool while the model embeds; results are committed every few files,
   so a Ctrl-C loses at most one small batch and the next run continues from the remaining `pending` rows.
   Files in offline roots wait (they stay `pending`); a file that has vanished is left to the next `sync`, which
   deletes it or pairs it with its new name — this stage never deletes rows.
   Videos are cut into segments (`video.py`); each segment gets a vector, the file gets their mean.
"""

from __future__ import annotations

import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from . import thumbs
from .decode import VIDEO_EXTS, load_preview
from .scan import content_hash, content_hashes, kind_of, walk

PREVIEW_SIDE = 1024
MIN_SIDE = 64  # longest side; smaller images are icons the model cannot read
COMMIT_EVERY = 16


@dataclass
class SyncStats:
    new: int = 0
    changed: int = 0
    touched: int = 0
    moved: int = 0
    deleted: int = 0
    unchanged: int = 0
    offline: list = field(default_factory=list)

    def any(self) -> bool:
        return bool(self.new or self.changed or self.touched or self.moved or self.deleted)


def _drop_thumb_if_unused(db, sha1):
    if sha1 and not db.execute("SELECT 1 FROM files WHERE sha1=? LIMIT 1", (sha1,)).fetchone():
        thumbs.drop_thumb(sha1)
        thumbs.drop_shot_thumbs(sha1)


def sync(store, root_ids=None) -> SyncStats:
    st = SyncStats()
    roots = [r for r in store.roots() if root_ids is None or r["id"] in root_ids]
    disk: dict[tuple[int, str], tuple[int, int]] = {}
    live_roots = []
    for r in roots:
        if not os.path.isdir(r["path"]):
            st.offline.append(r["path"])
            continue
        live_roots.append(r)
        for rel, size, mtime in walk(r["path"]):
            disk[(r["id"], rel)] = (size, mtime)
    if not live_roots:
        return st
    root_path = {r["id"]: r["path"] for r in live_roots}
    qmarks = ",".join("?" * len(live_roots))
    rows = store.db.execute(
        f"SELECT id, root_id, relpath, size, mtime_ns, sha1, status FROM files WHERE root_id IN ({qmarks})",
        [r["id"] for r in live_roots],
    ).fetchall()

    known = set()
    vanished = []
    updates_touch, updates_changed = [], []
    for row in rows:
        key = (row["root_id"], row["relpath"])
        cur = disk.get(key)
        if cur is None:
            vanished.append(row)
            continue
        known.add(key)
        size, mtime = cur
        if size == row["size"] and mtime == row["mtime_ns"]:
            st.unchanged += 1
            continue
        path = os.path.join(root_path[row["root_id"]], row["relpath"])
        try:
            sha = content_hash(path, size)
        except OSError:
            continue
        if row["sha1"] and sha == row["sha1"] and row["status"] != "error":
            updates_touch.append((size, mtime, row["id"]))
            st.touched += 1
        else:
            updates_changed.append((size, mtime, row["id"], row["sha1"]))
            st.changed += 1

    new = [(k, v) for k, v in disk.items() if k not in known]
    by_size: dict[int, list] = {}
    for row in vanished:
        if row["sha1"]:
            by_size.setdefault(row["size"], []).append(row)

    moves, inserts = [], []
    for (rid, rel), (size, mtime) in new:
        sha = None
        cands = by_size.get(size)
        if cands:
            try:
                sha, legacy = content_hashes(os.path.join(root_path[rid], rel), size)
            except OSError:
                continue
            match = next((c for c in cands if c["sha1"] in (sha, legacy)), None)
            if match is not None:
                cands.remove(match)
                vanished.remove(match)
                moves.append((rid, rel, mtime, match["id"]))
                continue
        inserts.append((rid, rel, size, mtime, sha, kind_of(rel)))
    st.moved, st.new, st.deleted = len(moves), len(inserts), len(vanished)

    if not st.any():
        return st
    db = store.db
    with store.tx():
        db.executemany("UPDATE files SET size=?, mtime_ns=? WHERE id=?", updates_touch)
        for size, mtime, fid, _ in updates_changed:
            db.execute(
                "UPDATE files SET size=?, mtime_ns=?, sha1=NULL, status='pending', error=NULL WHERE id=?",
                (size, mtime, fid),
            )
            db.execute("DELETE FROM vectors WHERE file_id=?", (fid,))
            db.execute("DELETE FROM shots WHERE file_id=?", (fid,))
        # Two-step move so swapping two names inside one sync cannot collide on UNIQUE(root_id, relpath).
        for i, (_, _, _, fid) in enumerate(moves):
            db.execute("UPDATE files SET relpath=? WHERE id=?", (f"\0moving{i}", fid))
        for rid, rel, mtime, fid in moves:
            db.execute("UPDATE files SET root_id=?, relpath=?, mtime_ns=? WHERE id=?", (rid, rel, mtime, fid))
        db.executemany(
            "INSERT INTO files(root_id, relpath, size, mtime_ns, sha1, status, kind) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            inserts,
        )
        db.executemany("DELETE FROM files WHERE id=?", [(r["id"],) for r in vanished])
        store.bump_generation()
    for _, _, _, old_sha in updates_changed:
        _drop_thumb_if_unused(db, old_sha)
    for r in vanished:
        _drop_thumb_if_unused(db, r["sha1"])
    return st


def requeue_for(store, model: str, tokens: int) -> int:
    """Mark indexed files whose vector came from another model / quality as pending (old vector stays searchable)."""
    with store.tx():
        cur = store.db.execute(
            "UPDATE files SET status='pending' WHERE status='ok' AND id NOT IN "
            "(SELECT file_id FROM vectors WHERE model=? AND tokens=?)",
            (model, tokens),
        )
    return cur.rowcount


def retry_errors(store) -> int:
    with store.tx():
        return store.db.execute("UPDATE files SET status='pending', error=NULL WHERE status='error'").rowcount


def pending_count(store) -> int:
    return store.db.execute("SELECT COUNT(*) FROM files WHERE status='pending'").fetchone()[0]


@dataclass
class _Job:
    id: int
    path: str
    size: int
    mtime_ns: int
    sha1: str | None = None
    preview: object = None
    width: int = 0
    height: int = 0
    method: str = ""
    error: str | None = None
    gone: bool = False
    segments: list | None = None  # videos only
    duration: float | None = None


def _prepare(job: _Job) -> _Job:
    try:
        s = os.stat(job.path)
    except OSError:
        job.gone = True
        return job
    job.size, job.mtime_ns = s.st_size, s.st_mtime_ns
    try:
        job.sha1 = content_hash(job.path, job.size)
        if os.path.splitext(job.path)[1].lower() in VIDEO_EXTS:
            from .video import load_segments

            info, segs = load_segments(job.path)
            job.width, job.height, job.duration, job.method = info.width, info.height, info.duration, "video"
            for i, sg in enumerate(segs):
                thumbs.save_thumb(f"{job.sha1}-{i}", sg.image)
            thumbs.save_thumb(job.sha1, segs[len(segs) // 3].image)  # skips most title cards / fade-ins
            job.segments = segs
            return job
        p = load_preview(job.path, PREVIEW_SIDE)
        job.width, job.height, job.method = p.width, p.height, p.method
        if min(p.image.size) < 1 or (p.width and max(p.width, p.height) < MIN_SIDE):  # icons; thin sprites are fine
            job.error = f"too small ({p.width}x{p.height})"
            return job
        thumbs.save_thumb(job.sha1, p.image)
        job.preview = p.image
    except Exception as e:  # noqa: BLE001 — any decoder failure becomes a per-file error, never a crash
        job.error = f"{type(e).__name__}: {e}"[:500]
    return job


def embed_pending(store, embedder, tokens: int, limit: int | None = None, workers: int | None = None, on_progress=None):
    """Embed every pending file. Returns counts. Raises KeyboardInterrupt untouched (committed work is kept)."""
    model = embedder.name
    online = [r["id"] for r in store.roots() if os.path.isdir(r["path"])]
    rows = store.db.execute(
        "SELECT f.id, r.path AS root, f.relpath, f.size, f.mtime_ns FROM files f JOIN roots r ON r.id=f.root_id "
        f"WHERE f.status='pending' AND f.root_id IN ({','.join('?' * len(online))}) ORDER BY f.id"
        + (f" LIMIT {int(limit)}" if limit else ""), online
    ).fetchall()
    total = len(rows)
    counts = {"ok": 0, "error": 0, "gone": 0, "reused": 0, "total": total}
    if not total:
        return counts
    jobs = (_Job(r["id"], os.path.join(r["root"], r["relpath"]), r["size"], r["mtime_ns"]) for r in rows)
    workers = workers or min(4, os.cpu_count() or 2)
    t0 = time.perf_counter()
    done = 0
    ready: list[_Job] = []
    finished: list[tuple[_Job, object]] = []

    def flush():
        nonlocal finished
        if not finished:
            return
        db = store.db
        with store.tx():
            vec_rows = []
            for job, vec in finished:
                counts["gone" if job.gone else "error" if job.error else "ok"] += 1
                if job.gone:
                    pass  # stays pending: the next sync deletes it, or keeps its row if it was moved / renamed
                elif job.error:
                    db.execute(
                        "UPDATE files SET status='error', error=?, size=?, mtime_ns=?, sha1=?, indexed_at=? WHERE id=?",
                        (job.error, job.size, job.mtime_ns, job.sha1, time.time(), job.id),
                    )
                else:
                    cur = db.execute(
                        "UPDATE files SET status='ok', error=NULL, size=?, mtime_ns=?, sha1=?, width=?, height=?, "
                        "method=?, indexed_at=?, duration=? WHERE id=?",
                        (job.size, job.mtime_ns, job.sha1, job.width, job.height, job.method, time.time(),
                         job.duration, job.id),
                    )
                    if cur.rowcount:  # the row may be gone if its folder was removed meanwhile
                        if job.segments is not None:
                            vec, seg_vecs = vec
                            store.put_shots(job.id, model, tokens, job.segments, seg_vecs)
                        vec_rows.append((job.id, model, tokens, vec))
            store.put_vectors(vec_rows)
            store.bump_generation()
        finished = []

    def embed_ready():
        nonlocal ready, done
        if not ready:
            return
        batch, ready = ready, []
        # Identical content elsewhere in the library (copies, exports): reuse its vector.
        todo = []
        for job in batch:
            if job.segments is not None:
                try:
                    sv = embedder.embed_images([sg.image for sg in job.segments], tokens=tokens)
                    mean = sv.mean(axis=0)
                    finished.append((job, (mean / max(float(np.linalg.norm(mean)), 1e-12), sv)))
                except Exception as e:  # noqa: BLE001
                    job.error = f"embed: {type(e).__name__}: {e}"[:500]
                    finished.append((job, None))
                for sg in job.segments:
                    sg.image = None
                continue
            row = store.db.execute(
                "SELECT v.vec FROM files f JOIN vectors v ON v.file_id=f.id WHERE f.sha1=? AND v.model=? AND v.tokens=? "
                "AND f.id<>? LIMIT 1",
                (job.sha1, model, tokens, job.id),
            ).fetchone()
            if row:
                finished.append((job, np.frombuffer(row[0], dtype=np.float16).astype(np.float32)))
                counts["reused"] += 1
            else:
                todo.append(job)
        if todo:
            try:
                vecs = embedder.embed_images([j.preview for j in todo], tokens=tokens)
                finished.extend(zip(todo, vecs))
            except Exception:  # one bad image must not sink the batch: retry one by one
                for j in todo:
                    try:
                        finished.append((j, embedder.embed_images([j.preview], tokens=tokens)[0]))
                    except Exception as e:  # noqa: BLE001
                        j.error = f"embed: {type(e).__name__}: {e}"[:500]
                        finished.append((j, None))
        for j in batch:
            j.preview = None
        done += len(batch)

    def record(job: _Job):
        nonlocal done
        if job.gone or job.error:
            finished.append((job, None))
            done += 1
        else:
            ready.append(job)
            if len(ready) >= embedder.batch_size:
                embed_ready()
        if len(finished) >= COMMIT_EVERY:
            flush()
            report()

    def report():
        if on_progress:
            dt = time.perf_counter() - t0
            rate = done / dt if dt else 0.0
            on_progress({"event": "progress", "done": done, "total": total, "per_s": round(rate, 2),
                         "eta_s": round((total - done) / rate) if rate else None})

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mitsuke-decode")
    try:
        window: deque = deque()
        for job in jobs:
            window.append(pool.submit(_prepare, job))
            if len(window) >= workers * 4:
                record(window.popleft().result())
        while window:
            record(window.popleft().result())
        embed_ready()
        flush()
        report()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    counts["seconds"] = round(time.perf_counter() - t0, 1)
    return counts
