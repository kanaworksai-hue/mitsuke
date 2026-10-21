"""The index: one SQLite file (WAL) plus a cached vector matrix for fast brute-force search.

Tables
- roots(id, path, added_at)
- files(id, root_id, relpath, size, mtime_ns, sha1, status, error, width, height, kind, method, indexed_at, duration)
  status: pending (needs embedding) / ok / error
- vectors(file_id, model, tokens, vec)  — 768-d float16, one row per file (for a video: the mean of its shots)
- shots(id, file_id, idx, t0, t1, tm, model, tokens, vec) — video segments; these, not the video's file vector,
  are what text / image search ranks
- boards(id, name, created_at, updated_at), board_items(board_id, file_id, shot_id, added_at) — moodboards
- meta(key, value)                       — schema version, generation counter, quality

`generation` is bumped whenever vectors change; the search matrix cache is keyed by it. Matrix row ids are file ids
for images and negative shot ids for video segments; `owners` maps every row to its file.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

import numpy as np

from . import paths

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    relpath TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha1 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    width INTEGER,
    height INTEGER,
    kind TEXT,
    method TEXT,
    indexed_at REAL,
    UNIQUE(root_id, relpath)
);
CREATE INDEX IF NOT EXISTS files_status ON files(status);
CREATE INDEX IF NOT EXISTS files_sha1 ON files(sha1);
CREATE TABLE IF NOT EXISTS vectors (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS shots (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    t0 REAL NOT NULL,
    t1 REAL NOT NULL,
    tm REAL NOT NULL,
    model TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    vec BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS shots_file ON shots(file_id);
CREATE TABLE IF NOT EXISTS boards (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS board_items (
    board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    shot_id INTEGER NOT NULL DEFAULT 0,
    added_at REAL NOT NULL,
    PRIMARY KEY (board_id, file_id, shot_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else paths.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(files)")}
        if "duration" not in cols:  # schema 1 → 2
            try:
                self.db.execute("ALTER TABLE files ADD COLUMN duration REAL")
            except sqlite3.OperationalError as e:  # another connection migrated first
                if "duplicate column" not in str(e):
                    raise
        if int(self.get_meta("schema_version", 0)) < SCHEMA_VERSION:
            self.set_meta("schema_version", SCHEMA_VERSION)

    def close(self):
        self.db.close()

    # --- meta -------------------------------------------------------------------------------
    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))

    def generation(self) -> int:
        return int(self.get_meta("generation", 0))

    def bump_generation(self):
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES ('generation', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
        )

    # --- roots ------------------------------------------------------------------------------
    def roots(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM roots ORDER BY path").fetchall()

    def add_root(self, path: str) -> tuple[int, bool]:
        row = self.db.execute("SELECT id FROM roots WHERE path=?", (path,)).fetchone()
        if row:
            return row[0], False
        cur = self.db.execute("INSERT INTO roots(path, added_at) VALUES (?, ?)", (path, time.time()))
        return cur.lastrowid, True

    def remove_root(self, root_id: int) -> int:
        n = self.db.execute("SELECT COUNT(*) FROM files WHERE root_id=?", (root_id,)).fetchone()[0]
        with self.tx():
            self.db.execute("DELETE FROM roots WHERE id=?", (root_id,))
            self.bump_generation()
        return n

    # --- transactions -----------------------------------------------------------------------
    def tx(self):
        return _Tx(self.db)

    # --- vectors ----------------------------------------------------------------------------
    def put_vectors(self, rows):
        """rows: iterable of (file_id, model, tokens, vec float32[768])."""
        self.db.executemany(
            "INSERT OR REPLACE INTO vectors(file_id, model, tokens, vec) VALUES (?, ?, ?, ?)",
            [(fid, m, t, np.asarray(v, dtype=np.float16).tobytes()) for fid, m, t, v in rows],
        )

    def vector(self, file_id: int, model: str) -> np.ndarray | None:
        row = self.db.execute("SELECT vec FROM vectors WHERE file_id=? AND model=?", (file_id, model)).fetchone()
        return np.frombuffer(row[0], dtype=np.float16).astype(np.float32) if row else None

    def matrix(self, model: str) -> tuple[np.ndarray, np.ndarray]:
        """(row ids int64[N], vectors float16[N, 768]): images by file id, video segments by -shot id.
        Cached on disk by generation."""
        ids, _, mat = self.matrix_full(model)
        return ids, mat

    def matrix_full(self, model: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(row ids, owning file ids, vectors)."""
        gen = self.generation()
        cdir = paths.cache_dir()
        tag = hashlib.sha1(f"{self.path.resolve()}|{model}".encode()).hexdigest()[:10]
        ids_p, own_p, vec_p = (cdir / f"matrix-{tag}-{gen}.{x}.npy" for x in ("ids", "own", "vec"))
        if ids_p.exists() and own_p.exists() and vec_p.exists():
            try:
                return np.load(ids_p), np.load(own_p), np.load(vec_p, mmap_mode="r")
            except (OSError, ValueError):
                pass
        rows = self.db.execute(
            "SELECT v.file_id AS id, v.file_id AS own, v.vec FROM vectors v JOIN files f ON f.id=v.file_id "
            "WHERE v.model=? AND f.kind IS NOT 'video' "
            "UNION ALL SELECT -s.id, s.file_id, s.vec FROM shots s WHERE s.model=? ORDER BY 2, 1",
            (model, model),
        ).fetchall()
        ids = np.array([r[0] for r in rows], dtype=np.int64)
        own = np.array([r[1] for r in rows], dtype=np.int64)
        mat = (
            np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float16).reshape(len(rows), -1)
            if rows
            else np.zeros((0, 768), np.float16)
        )
        try:
            cdir.mkdir(parents=True, exist_ok=True)
            for old in cdir.glob(f"matrix-{tag}-*.npy"):
                old.unlink(missing_ok=True)
            for p, arr in ((ids_p, ids), (own_p, own), (vec_p, mat)):
                tmp = p.with_suffix(f".tmp{os.getpid()}.npy")
                np.save(tmp, arr)
                os.replace(tmp, p)
        except OSError:
            pass  # cache is an optimisation only
        return ids, own, mat

    def put_shots(self, file_id: int, model: str, tokens: int, segments, vecs):
        self.db.execute("DELETE FROM shots WHERE file_id=?", (file_id,))
        self.db.executemany(
            "INSERT INTO shots(file_id, idx, t0, t1, tm, model, tokens, vec) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(file_id, i, s.t0, s.t1, s.tm, model, tokens, np.asarray(v, dtype=np.float16).tobytes())
             for i, (s, v) in enumerate(zip(segments, vecs))],
        )

    def shots_by_ids(self, ids) -> dict[int, sqlite3.Row]:
        ids = [int(i) for i in ids]
        out = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            q = f"SELECT id, file_id, idx, t0, t1, tm FROM shots WHERE id IN ({','.join('?' * len(chunk))})"
            for row in self.db.execute(q, chunk):
                out[row["id"]] = row
        return out

    def shot_vector(self, shot_id: int, model: str) -> np.ndarray | None:
        row = self.db.execute("SELECT vec FROM shots WHERE id=? AND model=?", (shot_id, model)).fetchone()
        return np.frombuffer(row[0], dtype=np.float16).astype(np.float32) if row else None

    # --- files ------------------------------------------------------------------------------
    def files_by_ids(self, ids) -> dict[int, sqlite3.Row]:
        ids = [int(i) for i in ids]
        out = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            q = (
                "SELECT f.*, r.path AS root FROM files f JOIN roots r ON r.id = f.root_id "
                f"WHERE f.id IN ({','.join('?' * len(chunk))})"
            )
            for row in self.db.execute(q, chunk):
                out[row["id"]] = row
        return out

    def find_file(self, abspath: str) -> sqlite3.Row | None:
        """Look up an absolute path among the roots (longest matching root wins)."""
        for r in sorted(self.roots(), key=lambda r: -len(r["path"])):
            root = r["path"].rstrip("/") + "/"
            if abspath.startswith(root):
                return self.db.execute(
                    "SELECT f.*, ? AS root FROM files f WHERE root_id=? AND relpath=?",
                    (r["path"], r["id"], abspath[len(root):]),
                ).fetchone()
        return None


class _Tx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, *a):
        self.db.execute("ROLLBACK" if exc_type else "COMMIT")
