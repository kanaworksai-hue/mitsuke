"""Library operations shared by the CLI, the MCP server and the web app: moodboards, file details, query images,
links into the web app.

Hits and boards here carry absolute paths (thumbnails included); the web app maps them to URLs.
Image references (`ref`) everywhere: `12` / `#12` / `f12` = indexed file id, `s34` = video shot id, or a path.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request

from . import paths, thumbs
from .search import _hit

WEB_KINDS = {"photo", "raw", "psd", "hdr", "video"}  # filters the web app has a button for


class NotFound(LookupError):
    pass


class BadRequest(ValueError):
    pass


# --- references -------------------------------------------------------------------------------

def parse_ref(store, ref) -> dict:
    """→ {"file": id} / {"shot": id} / {"path": p, "file": id or None}."""
    if isinstance(ref, int):
        return {"file": ref}
    if isinstance(ref, dict):
        return ref
    s = str(ref).strip()
    if not os.path.exists(s):
        if m := re.fullmatch(r"s(\d+)", s):
            return {"shot": int(m.group(1))}
        if m := re.fullmatch(r"[f#]?(\d+)", s):
            return {"file": int(m.group(1))}
    p = os.path.abspath(os.path.expanduser(s))
    if not os.path.exists(p):
        raise NotFound(f"not found: {s}")
    row = store.find_file(os.path.realpath(p)) or store.find_file(p)
    return {"path": p, "file": row["id"] if row else None}


def board_item(store, ref) -> dict:
    """A board entry must be in the library: {"file": id, "shot": id or 0}."""
    r = parse_ref(store, ref)
    if "shot" in r:
        s = store.shots_by_ids([r["shot"]]).get(r["shot"])
        if s is None:
            raise NotFound(f"no video shot s{r['shot']}")
        return {"file": s["file_id"], "shot": s["id"]}
    if r.get("file") is None:
        raise BadRequest(f"not in the library (add its folder and index first): {r['path']}")
    if not store.files_by_ids([r["file"]]):
        raise NotFound(f"no indexed file #{r['file']}")
    return {"file": int(r["file"]), "shot": int(r.get("shot") or 0)}


def query_vectors(store, model: str, refs, embed_images=None, tokens: int = 140):
    """Vectors for query images → (vectors, file ids to leave out, shot ids to leave out). Library files use their
    stored vectors; other paths are embedded with `embed_images(list of PIL) → array` (only called if needed)."""
    from pathlib import Path

    from .decode import load_preview
    from .indexer import PREVIEW_SIDE

    vecs, ex_files, ex_shots = [], set(), set()
    for ref in refs or []:
        r = parse_ref(store, ref)
        if "shot" in r:
            v = store.shot_vector(int(r["shot"]), model)
            ex_shots.add(int(r["shot"]))
        elif r.get("file") is not None:
            v = store.vector(int(r["file"]), model)
            ex_files.add(int(r["file"]))
        else:
            v = None
        if v is None and r.get("path"):
            if embed_images is None:
                raise BadRequest("this image is not indexed")
            try:
                im = load_preview(Path(r["path"]), PREVIEW_SIDE).image
            except Exception as e:  # noqa: BLE001
                raise BadRequest(f"cannot read this image: {type(e).__name__}") from None
            v = embed_images([im])[0]
        if v is None:
            raise NotFound(f"not in the index (yet): {ref}")
        vecs.append(v)
    return vecs, ex_files, ex_shots


def board_refs(store, bid: int) -> list[dict]:
    if store.db.execute("SELECT 1 FROM boards WHERE id=?", (bid,)).fetchone() is None:
        raise NotFound(f"no board #{bid}")
    return [{"shot": i["shot_id"]} if i["shot_id"] else {"file": i["file_id"]}
            for i in store.db.execute("SELECT file_id, shot_id FROM board_items WHERE board_id=? ORDER BY added_at", (bid,))]


# --- folders ----------------------------------------------------------------------------------

def add_folder(store, path: str) -> dict:
    p = os.path.realpath(os.path.abspath(os.path.expanduser(str(path).strip())))
    if not os.path.isdir(p):
        raise BadRequest(f"not a folder: {path}")
    for r in store.roots():  # nested roots would index the same files twice
        rp = r["path"]
        if p != rp and (p.startswith(rp + "/") or rp.startswith(p + "/")):
            raise BadRequest(f"{p} overlaps with an existing folder {rp}; remove one of them first")
    rid, new = store.add_root(p)
    return {"id": rid, "path": p, "new": new}


def index_note(store) -> dict:
    """`pending` for search results; with a `note` when files still wait for embedding (results may be incomplete)."""
    from .indexer import pending_count

    n = pending_count(store)
    if not n:
        return {"pending": 0}
    return {"pending": n, "note": f"{n} files are not indexed yet, so results may be incomplete "
                                  "(indexing is running, or start it with index; status shows progress)"}


# --- files ------------------------------------------------------------------------------------

def row_hit(r) -> dict:
    return _hit(r, 0.0)


def item(store, fid: int) -> dict:
    r = store.files_by_ids([fid]).get(fid)
    if r is None:
        raise NotFound(f"no indexed file #{fid}")
    out = row_hit(r)
    out.pop("score", None)
    out["name"] = os.path.basename(out["path"])
    try:
        st = os.stat(out["path"])
        out.update(bytes=st.st_size, mtime=st.st_mtime, exists=True)
    except OSError:
        out.update(exists=False)
    out["root"] = r["root"]
    out["status"] = r["status"]
    out["method"] = r["method"]
    if r["kind"] == "video":
        out["shots"] = [
            {"shot": s["id"], "t0": s["t0"], "t1": s["t1"], "time": s["tm"],
             "thumb": str(thumbs.thumb_path(f"{r['sha1']}-{s['idx']}"))}
            for s in store.db.execute("SELECT id, idx, t0, t1, tm FROM shots WHERE file_id=? ORDER BY idx", (fid,))
        ]
    out["boards"] = [b for (b,) in store.db.execute("SELECT DISTINCT board_id FROM board_items WHERE file_id=?", (fid,))]
    return out


# --- boards -----------------------------------------------------------------------------------

def list_boards(store) -> list[dict]:
    rows = store.db.execute(
        "SELECT b.id, b.name, b.updated_at, COUNT(i.file_id) n FROM boards b LEFT JOIN board_items i ON i.board_id=b.id "
        "GROUP BY b.id ORDER BY b.updated_at DESC").fetchall()
    out = []
    for b in rows:
        covers = store.db.execute(
            "SELECT f.sha1, i.shot_id, s.idx FROM board_items i JOIN files f ON f.id=i.file_id "
            "LEFT JOIN shots s ON s.id=i.shot_id WHERE i.board_id=? ORDER BY i.added_at DESC LIMIT 4", (b["id"],)).fetchall()
        out.append({"id": b["id"], "name": b["name"], "count": b["n"], "updated_at": b["updated_at"],
                    "covers": [str(thumbs.thumb_path(c["sha1"] + (f"-{c['idx']}" if c["shot_id"] else "")))
                               for c in covers if c["sha1"]]})
    return out


def _name(name) -> str:
    return (name or "").strip()[:120] or "Untitled"


def create_board(store, name: str) -> dict:
    now = time.time()
    cur = store.db.execute("INSERT INTO boards(name, created_at, updated_at) VALUES (?, ?, ?)", (_name(name), now, now))
    return {"id": cur.lastrowid, "name": _name(name)}


def _need_board(store, bid: int):
    b = store.db.execute("SELECT * FROM boards WHERE id=?", (bid,)).fetchone()
    if b is None:
        raise NotFound(f"no board #{bid}")
    return b


def rename_board(store, bid: int, name: str) -> dict:
    _need_board(store, bid)
    store.db.execute("UPDATE boards SET name=?, updated_at=? WHERE id=?", (_name(name), time.time(), bid))
    return {"id": bid, "name": _name(name)}


def delete_board(store, bid: int) -> dict:
    _need_board(store, bid)
    store.db.execute("DELETE FROM boards WHERE id=?", (bid,))
    return {"deleted": bid}


def board(store, bid: int) -> dict:
    b = _need_board(store, bid)
    items = store.db.execute("SELECT file_id, shot_id FROM board_items WHERE board_id=? ORDER BY added_at", (bid,)).fetchall()
    files = store.files_by_ids({i["file_id"] for i in items})
    shots = store.shots_by_ids([i["shot_id"] for i in items if i["shot_id"]])
    res = []
    for i in items:
        r = files.get(i["file_id"])
        if r is None:
            continue
        h = row_hit(r)
        h.pop("score", None)
        s = shots.get(i["shot_id"])
        if s is not None:
            h.update(shot=s["id"], time=s["tm"], t0=s["t0"], t1=s["t1"],
                     thumb=str(thumbs.thumb_path(f"{r['sha1']}-{s['idx']}")))
        res.append(h)
    return {"id": bid, "name": b["name"], "results": res}


def board_add(store, bid: int, items: list[dict]) -> dict:
    _need_board(store, bid)
    now = time.time()
    with store.tx() as db:
        for i, it in enumerate(items):
            db.execute("INSERT OR IGNORE INTO board_items(board_id, file_id, shot_id, added_at) VALUES (?, ?, ?, ?)",
                       (bid, int(it["file"]), int(it.get("shot") or 0), now + i * 1e-3))
        db.execute("UPDATE boards SET updated_at=? WHERE id=?", (now, bid))
    return board(store, bid)


def board_remove(store, bid: int, items: list[dict]) -> dict:
    _need_board(store, bid)
    with store.tx() as db:
        for it in items:
            db.execute("DELETE FROM board_items WHERE board_id=? AND file_id=? AND shot_id=?",
                       (bid, int(it["file"]), int(it.get("shot") or 0)))
    return board(store, bid)


# --- the web app ------------------------------------------------------------------------------

def serve_file():
    return paths.home_dir() / "serve.json"


def running_server() -> str | None:
    """URL of a `mitsuke serve` running on this index, or None."""
    try:
        info = json.loads(serve_file().read_text())
        os.kill(int(info["pid"]), 0)
        with urllib.request.urlopen(info["url"] + "api/status", timeout=2) as r:
            if r.status == 200:
                return info["url"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def web_url(base: str, text: str = "", refs=(), board_id: int | None = None, show_board: int | None = None,
            text_weight: float = 0.5, kinds=None, root: str | None = None) -> str:
    """A link that opens the web app on this search (the page keeps its state in the URL hash)."""
    p = {}
    if text:
        p["q"] = text
    toks = []
    for r in refs or []:
        if "shot" in r:
            toks.append(f"s{r['shot']}")
        elif r.get("file") is not None:
            toks.append(f"f{r['file']}")
    if board_id:
        toks.append(f"b{board_id}")
    if toks:
        p["img"] = ",".join(toks)
    if toks and text and text_weight != 0.5:
        p["w"] = f"{text_weight:g}"
    if kinds and len(kinds) == 1 and kinds[0] in WEB_KINDS:
        p["kind"] = kinds[0]
    if root:
        p["root"] = root
    if show_board:
        p["board"] = str(show_board)
    return base + ("#" + urllib.parse.urlencode(p, quote_via=urllib.parse.quote) if p else "")
