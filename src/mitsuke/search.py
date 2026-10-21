"""Brute-force cosine search over the cached vector matrix (100k images is a ~10 ms matmul).

Matrix rows are images (row id = file id) and video segments (row id = -shot id). Results are grouped per file:
a video appears once, at its best segment, with its other matching segments listed under `shots`.
Image + text queries fuse z-scores (each signal standardised over the library, then weighted).
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from . import thumbs
from .decode import load_preview
from .indexer import PREVIEW_SIDE

MAX_SHOTS_PER_HIT = 6
KIND_GROUPS = {"image": None, "hdr": ["exr", "hdr"], "photo": ["jpeg", "heic", "png", "webp", "tiff", "avif"]}


def known_kinds() -> set[str]:
    from .decode import SUPPORTED_EXTS
    from .scan import kind_of

    return {kind_of("x" + e) for e in SUPPORTED_EXTS} | set(KIND_GROUPS)


def check_kinds(kinds):
    """Unknown kinds would silently match nothing; say so instead."""
    bad = [k for k in kinds or [] if k.lower() not in known_kinds()]
    if bad:
        raise ValueError(f"unknown kind {', '.join(bad)}; use one of: {', '.join(sorted(known_kinds()))}")
    return kinds


def expand_kinds(kinds) -> tuple[list[str] | None, bool]:
    """User kinds → (kinds for SQL, invert). "image" means everything except video."""
    if not kinds:
        return None, False
    out = []
    for k in kinds:
        k = k.lower()
        if k == "image":
            return ["video"], True
        out += KIND_GROUPS.get(k) or [k]
    return out, False


def _allowed_files(store, root: str | None, kinds) -> np.ndarray | None:
    kinds, invert = expand_kinds(kinds)
    if not root and not kinds:
        return None
    where, args = [], []
    if root:
        root = os.path.abspath(os.path.expanduser(root)).rstrip("/")
        where.append("(r.path = ? OR r.path LIKE ? OR ? LIKE r.path || '/%')")
        args += [root, root + "/%", root]
    if kinds:
        where.append(f"f.kind {'NOT ' if invert else ''}IN ({','.join('?' * len(kinds))})")
        args += kinds
    q = "SELECT f.id, r.path AS root, f.relpath FROM files f JOIN roots r ON r.id=f.root_id WHERE " + " AND ".join(where)
    allowed = []
    for row in store.db.execute(q, args):
        # --root may also be a sub-folder of a root
        if root and not os.path.join(row["root"], row["relpath"]).startswith(root + "/"):
            continue
        allowed.append(row["id"])
    return np.array(allowed, dtype=np.int64)


def scores(mat, qvec) -> np.ndarray:
    q = np.asarray(qvec, dtype=np.float32)
    # float16 → float32 in chunks: BLAS speed without a full-size float32 copy of the library
    if not len(mat):
        return np.zeros(0, np.float32)
    return np.concatenate([np.asarray(mat[i : i + 32768], dtype=np.float32) @ q for i in range(0, len(mat), 32768)])


def zscore(s: np.ndarray) -> np.ndarray:
    return (s - s.mean()) / (s.std() + 1e-9) if len(s) else s


def unit_mean(vecs) -> np.ndarray:
    m = np.mean(np.asarray(vecs, dtype=np.float32), axis=0)
    return m / max(float(np.linalg.norm(m)), 1e-12)


def _hit(r, score: float) -> dict:
    return {
        "id": r["id"],
        "score": round(float(score), 4),
        "path": os.path.join(r["root"], r["relpath"]),
        "kind": r["kind"],
        "width": r["width"],
        "height": r["height"],
        "thumb": str(thumbs.thumb_path(r["sha1"])) if r["sha1"] else None,
        **({"duration": r["duration"]} if r["kind"] == "video" else {}),
    }


def _shot(s, sha1, score: float) -> dict:
    return {"shot": s["id"], "time": s["tm"], "t0": s["t0"], "t1": s["t1"], "score": round(float(score), 4),
            "thumb": str(thumbs.thumb_path(f"{sha1}-{s['idx']}")) if sha1 else None}


def top_hits(store, ids, owners, sc, k: int = 20, root=None, kinds=None, exclude=None, exclude_shots=None, offset: int = 0):
    """Best `k` files (after skipping `offset`) for the row scores `sc`."""
    if not len(ids) or k <= 0:
        return []
    sc = np.asarray(sc, dtype=np.float32).copy()
    allowed = _allowed_files(store, root, kinds)
    if allowed is not None:
        sc[~np.isin(owners, allowed)] = -np.inf
    if exclude:
        sc[np.isin(owners, np.array(list(exclude), dtype=np.int64))] = -np.inf
    if exclude_shots:
        sc[np.isin(ids, -np.array(list(exclude_shots), dtype=np.int64))] = -np.inf
    finite = int(np.isfinite(sc).sum())
    want = offset + k
    m = min(finite, want * 3 + 64)
    while True:
        top = np.argpartition(-sc, m - 1)[:m] if m < len(sc) else np.arange(len(sc))
        top = top[np.isfinite(sc[top])]
        top = top[np.argsort(-sc[top], kind="stable")]
        files_seen = len(set(owners[top].tolist()))
        if files_seen >= want or m >= finite:
            break
        m = min(finite, m * 2)
    files = store.files_by_ids(set(owners[top].tolist()))
    shots = store.shots_by_ids([-int(ids[j]) for j in top if ids[j] < 0])
    order, by_file = [], {}
    for j in top:
        fid = int(owners[j])
        r = files.get(fid)
        if r is None:
            continue
        rid = int(ids[j])
        h = by_file.get(fid)
        if h is None:
            if len(order) >= want:
                continue
            h = _hit(r, sc[j])
            by_file[fid] = h
            order.append(h)
            if rid < 0 and (s := shots.get(-rid)):
                best = _shot(s, r["sha1"], sc[j])
                h.update(time=best["time"], t0=best["t0"], t1=best["t1"], shot=best["shot"], thumb=best["thumb"] or h["thumb"],
                         poster=h["thumb"], shots=[best])
        elif rid < 0 and "shots" in h and len(h["shots"]) < MAX_SHOTS_PER_HIT and (s := shots.get(-rid)):
            h["shots"].append(_shot(s, r["sha1"], sc[j]))
    return order[offset:want]


def rank(store, model: str, qvec: np.ndarray, k: int = 20, root=None, kinds=None, exclude: set | None = None, offset: int = 0):
    ids, owners, mat = store.matrix_full(model)
    return top_hits(store, ids, owners, scores(mat, qvec), k=k, root=root, kinds=kinds, exclude=exclude, offset=offset)


def search_text(store, embedder, query: str, **kw):
    return rank(store, embedder.name, embedder.embed_texts([query])[0], **kw)


def combined(store, model: str, text_vec=None, image_vecs=(), text_weight: float = 0.5, **kw):
    """Text, images, or both. Several images are averaged (e.g. a whole moodboard). With both, each signal is
    z-scored over the library and mixed by `text_weight` (0 = only the images, 1 = only the text)."""
    ids, owners, mat = store.matrix_full(model)
    parts = []
    if len(image_vecs):
        parts.append((1.0 - text_weight, scores(mat, unit_mean(image_vecs))))
    if text_vec is not None:
        parts.append((text_weight, scores(mat, text_vec)))
    if not parts:
        return []
    if len(parts) == 1:
        sc = parts[0][1]
    else:
        sc = sum(w * zscore(s) for w, s in parts)
    return top_hits(store, ids, owners, sc, **kw)


def resolve_image(store, ref: str):
    """`ref` is a file id (`123` / `#123`) or a path. Returns (file_id or None, absolute path or None)."""
    s = ref.lstrip("#")
    if s.isdigit() and not os.path.exists(ref):
        row = store.files_by_ids([int(s)]).get(int(s))
        if row is None:
            raise FileNotFoundError(f"no indexed file with id {s}")
        return row["id"], os.path.join(row["root"], row["relpath"])
    p = os.path.abspath(os.path.expanduser(ref))
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    row = store.find_file(os.path.realpath(p)) or store.find_file(p)
    return (row["id"] if row else None), p


def image_vector(store, model: str, ref: str, get_embedder, tokens: int):
    """Stored vector for library files (no model load needed); a fresh embedding for anything else.
    `get_embedder` is only called when the model is really needed."""
    fid, path = resolve_image(store, ref)
    if fid is not None:
        v = store.vector(fid, model)
        if v is not None:
            return fid, path, v
    return fid, path, get_embedder().embed_images([load_preview(Path(path), PREVIEW_SIDE).image], tokens=tokens)[0]
