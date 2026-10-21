"""`mitsuke serve`: the local web app. Listens on 127.0.0.1 only; standard library HTTP server, no build step.

- All model work (EmbeddingGemma 2) runs on one dedicated thread (`Engine`): MLX is not meant to be
  driven from many threads at once, and one search at a time is plenty for one person.
- A background `Indexer` keeps the library current (sync → embed → watch), unless another `mitsuke index`
  holds the lock, in which case the page just searches.
- Every request gets its own SQLite connection (WAL: readers never block the indexer).
- State-changing requests must carry `X-Mitsuke: 1` (a page on another site cannot set it), and the Host header
  must be local (blocks DNS rebinding).
"""

from __future__ import annotations

import fcntl
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np

from . import __version__, library, paths
from .model import QUALITY_TOKENS, model_id

WEB = Path(__file__).with_name("web")
DEFAULT_PORT = 8737
BROWSER_IMAGE_KINDS = {"jpeg", "png", "webp", "gif", "avif", "bmp"}
PREVIEW_SIDE = 2560
THUMB_RE = re.compile(r"^[0-9a-f]{40}(-\d+)?\.webp$")
UPLOAD_RE = re.compile(r"^[0-9a-f]{32}\.webp$")


# --- model thread -----------------------------------------------------------------------------

class Engine:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mitsuke-model")
        self._emb = None
        self.name = model_id()

    def run(self, fn, *a, **kw):
        return self.pool.submit(fn, *a, **kw).result()

    def _embedder(self):
        if self._emb is None:
            from .model import get_embedder

            self._emb = get_embedder()
        return self._emb

    def warm(self):
        self.pool.submit(self._embedder)

    @property
    def loaded(self) -> bool:
        return self._emb is not None

    @property
    def batch_size(self) -> int:
        return self.run(lambda: self._embedder().batch_size)

    def embed_texts(self, texts):
        return self.run(lambda: self._embedder().embed_texts(texts))

    def embed_images(self, images, tokens: int = 140):
        return self.run(lambda: self._embedder().embed_images(images, tokens=tokens))


# --- background indexer -----------------------------------------------------------------------

class Indexer(threading.Thread):
    def __init__(self, engine: Engine, store_factory):
        super().__init__(name="mitsuke-indexer", daemon=True)
        self.engine = engine
        self.store_factory = store_factory
        self.wake = threading.Event()
        self.stopping = False
        self.lockf = None
        self.state = {"state": "starting"}

    def kick(self):
        """Folders changed: rescan and restart the watcher."""
        self.wake.set()

    def stop(self):
        self.stopping = True
        self.wake.set()

    def _lock(self) -> bool:
        if self.lockf:
            return True
        paths.home_dir().mkdir(parents=True, exist_ok=True)
        f = open(paths.home_dir() / "index.lock", "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            return False
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        self.lockf = f
        return True

    def _pass(self, store, root_ids=None):
        from . import indexer

        self.state = {**self.state, "state": "scanning"}
        st = indexer.sync(store, root_ids)
        tokens = int(store.get_meta("tokens", QUALITY_TOKENS["default"]))
        indexer.requeue_for(store, self.engine.name, tokens)
        if indexer.pending_count(store):
            self.state = {"state": "indexing", "done": 0, "total": indexer.pending_count(store)}

            def progress(ev):
                self.state = {"state": "indexing", **{k: ev[k] for k in ("done", "total", "per_s", "eta_s")}}

            counts = indexer.embed_pending(store, self.engine, tokens, on_progress=progress)
            self.state = {"state": "idle", "last": counts}
        else:
            self.state = {**{k: v for k, v in self.state.items() if k == "last"}, "state": "idle"}
        if st.offline:
            self.state["offline"] = st.offline

    def run(self):
        from . import watch

        while not self.stopping:
            if not self._lock():
                self.state = {"state": "external"}  # `mitsuke index` is running elsewhere
                self.wake.wait(10)
                self.wake.clear()
                continue
            store = self.store_factory()
            try:
                self.wake.clear()
                self._pass(store)
                roots = {r["id"]: r["path"] for r in store.roots() if os.path.isdir(r["path"])}
                if not roots:
                    self.state = {**self.state, "state": "idle"}
                    self.wake.wait()
                    continue
                self.state = {**self.state, "state": "watching"}

                def on_change(dirty):
                    self._pass(store, dirty)
                    self.state = {**self.state, "state": "watching"}

                watch.run(store, roots, on_change, stop=self.wake)
            except Exception as e:  # noqa: BLE001 — keep serving searches even if indexing breaks
                traceback.print_exc()
                self.state = {"state": "error", "message": f"{type(e).__name__}: {e}"[:300]}
                self.wake.wait(30)
            finally:
                store.close()


# --- helpers ----------------------------------------------------------------------------------

def thumb_url(p: str | None) -> str | None:
    return f"/t/{os.path.basename(p)}" if p else None


def web_hit(h: dict) -> dict:
    out = dict(h)
    out["name"] = os.path.basename(h["path"])
    out["dir"] = os.path.dirname(h["path"])
    out["thumb"] = thumb_url(h.get("thumb"))
    if h.get("poster"):
        out["poster"] = thumb_url(h["poster"])
    if "shots" in h:
        out["shots"] = [{**s, "thumb": thumb_url(s.get("thumb"))} for s in h["shots"]]
    return out


def file_row(store, fid: int):
    return store.files_by_ids([fid]).get(fid)


def row_hit(store, r) -> dict:
    return web_hit(library.row_hit(r))


def folder_tree(store) -> list[dict]:
    from .cli import _root_stats

    roots = _root_stats(store)
    for r in roots:
        subs = store.db.execute(
            "SELECT substr(relpath, 1, instr(relpath, '/') - 1) AS top, COUNT(*) FROM files "
            "WHERE root_id=? AND status='ok' AND instr(relpath, '/') > 0 GROUP BY top ORDER BY top", (r["id"],)).fetchall()
        r["children"] = [{"name": k, "path": os.path.join(r["path"], k), "count": v} for k, v in subs]
        r["name"] = os.path.basename(r["path"]) or r["path"]
    return roots


class Api:
    """Request handlers that do not touch HTTP details (also what the tests call)."""

    def __init__(self, engine: Engine, indexer: Indexer | None):
        self.engine = engine
        self.indexer = indexer
        self.uploads: dict[str, np.ndarray] = {}
        self._local = threading.local()

    @property
    def store(self):
        s = getattr(self._local, "store", None)
        if s is None:
            from .store import Store

            s = self._local.store = Store()
        return s

    def tokens(self) -> int:
        return int(self.store.get_meta("tokens", QUALITY_TOKENS["default"]))

    # status / folders
    def status(self) -> dict:
        from .indexer import pending_count

        store = self.store
        counts = dict(store.db.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())
        kinds = dict(store.db.execute("SELECT kind, COUNT(*) FROM files WHERE status='ok' GROUP BY kind").fetchall())
        return {
            "version": __version__, "model": self.engine.name, "model_loaded": self.engine.loaded,
            "indexed": counts.get("ok", 0), "pending": pending_count(store), "errors": counts.get("error", 0),
            "kinds": kinds, "roots": len(store.roots()),
            "indexer": self.indexer.state if self.indexer else {"state": "off"},
            "platform": sys.platform,
        }

    def folders(self) -> dict:
        return {"folders": folder_tree(self.store)}

    def add_folder(self, path: str) -> dict:
        try:
            r = library.add_folder(self.store, path)
        except library.BadRequest as e:
            raise ApiError(400, str(e)) from None
        if self.indexer:
            self.indexer.kick()
        return r

    def remove_folder(self, rid: int) -> dict:
        n = self.store.remove_root(rid)
        if self.indexer:
            self.indexer.kick()
        return {"removed": rid, "files": n}

    # search
    def _query_vectors(self, images) -> tuple[list[np.ndarray], set[int], set[int]]:
        images = images or []
        for im in images:
            if "upload" in im and str(im["upload"]) not in self.uploads:
                raise ApiError(404, "image not in the index (yet)")
        lib = [im for im in images if "upload" not in im]
        try:
            vecs, ex_files, ex_shots = library.query_vectors(self.store, self.engine.name, lib)
        except library.NotFound:
            raise ApiError(404, "image not in the index (yet)") from None
        return vecs + [self.uploads[str(im["upload"])] for im in images if "upload" in im], ex_files, ex_shots

    def search(self, q: dict) -> dict:
        from .search import combined

        t = time.perf_counter()
        text = (q.get("text") or "").strip()
        vecs, ex_files, ex_shots = self._query_vectors(q.get("images"))
        tv = self.engine.embed_texts([text])[0] if text else None
        hits = combined(
            self.store, self.engine.name, text_vec=tv, image_vecs=vecs,
            text_weight=float(q.get("text_weight", 0.5)), k=min(int(q.get("k", 100)), 500),
            offset=int(q.get("offset", 0)), root=q.get("root") or None, kinds=q.get("kinds") or None,
            exclude=ex_files, exclude_shots=ex_shots,
        )
        return {"results": [web_hit(h) for h in hits], "ms": round((time.perf_counter() - t) * 1000)}

    def browse(self, q: dict) -> dict:
        """No query: newest first (by file time), optionally inside a folder / of some kinds."""
        from .search import expand_kinds

        k, offset = min(int(q.get("k", 100)), 500), int(q.get("offset", 0))
        where, args = "f.status='ok'", []
        kinds, invert = expand_kinds(q.get("kinds") or None)
        if kinds:
            where += f" AND f.kind {'NOT ' if invert else ''}IN ({','.join('?' * len(kinds))})"
            args += kinds
        if q.get("root"):
            root = os.path.abspath(os.path.expanduser(q["root"])).rstrip("/")
            where += " AND (r.path || '/' || f.relpath) LIKE ? ESCAPE '\\'"
            args.append(root.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%")
        base = f"FROM files f JOIN roots r ON r.id=f.root_id WHERE {where}"
        rows = self.store.db.execute(
            f"SELECT f.*, r.path AS root {base} ORDER BY f.mtime_ns DESC, f.id DESC LIMIT ? OFFSET ?",
            args + [k, offset]).fetchall()
        total = self.store.db.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
        return {"results": [row_hit(self.store, r) for r in rows], "total": total}

    def item(self, fid: int) -> dict:
        out = web_hit(library.item(self.store, fid))
        out["view"] = f"/api/view/{fid}"
        return out

    def upload(self, body: bytes, filename: str) -> dict:
        import tempfile

        from .decode import load_preview

        if not body:
            raise ApiError(400, "empty upload")
        suffix = os.path.splitext(filename or "")[1].lower() or ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix) as f:
            f.write(body)
            f.flush()
            try:
                im = load_preview(f.name, 1024).image
            except Exception as e:  # noqa: BLE001
                raise ApiError(415, f"cannot read this image: {type(e).__name__}") from None
        vec = self.engine.embed_images([im], tokens=self.tokens())[0]
        uid = uuid.uuid4().hex
        self.uploads[uid] = vec
        d = paths.cache_dir() / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        im.thumbnail((512, 512))
        im.save(d / f"{uid}.webp", "WEBP", quality=80)
        return {"upload": uid, "thumb": f"/u/{uid}.webp"}

    # boards
    def boards(self) -> dict:
        return {"boards": [{**b, "covers": [thumb_url(c) for c in b["covers"]]} for b in library.list_boards(self.store)]}

    def create_board(self, name: str) -> dict:
        return library.create_board(self.store, name)

    def rename_board(self, bid: int, name: str) -> dict:
        return library.rename_board(self.store, bid, name)

    def delete_board(self, bid: int) -> dict:
        return library.delete_board(self.store, bid)

    def board(self, bid: int) -> dict:
        b = library.board(self.store, bid)
        return {**b, "results": [web_hit(h) for h in b["results"]]}

    def board_add(self, bid: int, items: list[dict]) -> dict:
        library.board_add(self.store, bid, items)
        return self.board(bid)

    def board_remove(self, bid: int, items: list[dict]) -> dict:
        library.board_remove(self.store, bid, items)
        return self.board(bid)

    # desktop actions (user-initiated)
    def reveal(self, fid: int, mode: str = "reveal") -> dict:
        r = file_row(self.store, fid)
        if r is None:
            raise ApiError(404, "no such file")
        p = os.path.join(r["root"], r["relpath"])
        if sys.platform == "darwin" and os.environ.get("MITSUKE_NO_DESKTOP") != "1":
            subprocess.run(["open", "-R", p] if mode == "reveal" else ["open", p], check=False)
        return {"path": p}

    def pick_folder(self) -> dict:
        if sys.platform != "darwin" or os.environ.get("MITSUKE_NO_DESKTOP") == "1":
            raise ApiError(501, "type the folder path instead")
        r = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Mitsuke")'],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return {"path": None}
        return {"path": r.stdout.strip().rstrip("/") or "/"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# --- HTTP -------------------------------------------------------------------------------------

def make_handler(api: Api, allowed_hosts: set[str]):
    class H(BaseHTTPRequestHandler):
        server_version = f"Mitsuke/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet; errors still go to stderr via traceback
            if os.environ.get("MITSUKE_HTTP_LOG"):
                super().log_message(fmt, *args)

        # plumbing
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in allowed_hosts

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, status: int = 200):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                       {"Cache-Control": "no-store"})

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 200 * 2**20:
                raise ApiError(413, "too large")
            return self.rfile.read(n) if n else b""

        def _jbody(self) -> dict:
            b = self._body()
            return json.loads(b) if b else {}

        def _file(self, p: Path, ctype: str | None = None, cache: str = "no-cache"):
            try:
                st = p.stat()
            except OSError:
                return self._json({"error": "not found"}, 404)
            ctype = ctype or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            size = st.st_size
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            status = 200
            if rng and (m := re.match(r"bytes=(\d*)-(\d*)$", rng.strip())):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", cache)
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(p, "rb") as f:
                f.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = f.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def _view(self, fid: int):
            """Original file when the browser can show it, else a cached large JPEG preview."""
            r = file_row(api.store, fid)
            if r is None:
                return self._json({"error": "not found"}, 404)
            p = Path(r["root"]) / r["relpath"]
            if r["kind"] == "video" or (r["kind"] in BROWSER_IMAGE_KINDS and r["size"] < 60 * 2**20):
                return self._file(p, "video/mp4" if p.suffix.lower() in (".mp4", ".m4v") else None)
            cdir = paths.cache_dir() / "previews"
            out = cdir / f"{r['sha1']}.jpg"
            if not out.exists():
                from .decode import load_preview

                try:
                    im = load_preview(p, PREVIEW_SIDE).image
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": f"cannot decode: {type(e).__name__}"}, 415)
                cdir.mkdir(parents=True, exist_ok=True)
                tmp = out.with_suffix(f".tmp{threading.get_ident()}")
                im.save(tmp, "JPEG", quality=90)
                os.replace(tmp, out)
            return self._file(out, "image/jpeg", "max-age=3600")

        # routing
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

        def do_PATCH(self):
            self._route("PATCH")

        def _route(self, method: str):
            try:
                if not self._host_ok():
                    return self._json({"error": "bad host"}, 403)
                u = urlparse(self.path)
                path = unquote(u.path)
                if method != "GET" and self.headers.get("X-Mitsuke") != "1":
                    return self._json({"error": "missing X-Mitsuke header"}, 403)
                if method == "GET":
                    if path in ("/", "/index.html"):
                        return self._file(WEB / "index.html", "text/html; charset=utf-8")
                    if path.startswith("/static/"):
                        name = path[len("/static/"):]
                        if "/" in name or name.startswith(".") or not (WEB / name).is_file():
                            return self._json({"error": "not found"}, 404)
                        return self._file(WEB / name)
                    if path.startswith("/t/"):
                        name = path[3:]
                        if not THUMB_RE.match(name):
                            return self._json({"error": "not found"}, 404)
                        return self._file(paths.thumbs_dir() / name[:2] / name, "image/webp", "max-age=86400")
                    if path.startswith("/u/"):
                        name = path[3:]
                        if not UPLOAD_RE.match(name):
                            return self._json({"error": "not found"}, 404)
                        return self._file(paths.cache_dir() / "uploads" / name, "image/webp")
                    if m := re.match(r"^/api/view/(\d+)$", path):
                        return self._view(int(m.group(1)))
                    if path == "/api/status":
                        return self._json(api.status())
                    if path == "/api/folders":
                        return self._json(api.folders())
                    if path == "/api/boards":
                        return self._json(api.boards())
                    if m := re.match(r"^/api/boards/(\d+)$", path):
                        return self._json(api.board(int(m.group(1))))
                    if m := re.match(r"^/api/item/(\d+)$", path):
                        return self._json(api.item(int(m.group(1))))
                if method == "POST":
                    if path == "/api/search":
                        return self._json(api.search(self._jbody()))
                    if path == "/api/browse":
                        return self._json(api.browse(self._jbody()))
                    if path == "/api/upload":
                        return self._json(api.upload(self._body(), unquote(self.headers.get("X-Filename") or "")))
                    if path == "/api/folders":
                        return self._json(api.add_folder(self._jbody().get("path", "")))
                    if path == "/api/folders/pick":
                        return self._json(api.pick_folder())
                    if path == "/api/boards":
                        return self._json(api.create_board(self._jbody().get("name", "")))
                    if m := re.match(r"^/api/boards/(\d+)/items$", path):
                        return self._json(api.board_add(int(m.group(1)), self._jbody().get("items", [])))
                    if m := re.match(r"^/api/(reveal|open)/(\d+)$", path):
                        return self._json(api.reveal(int(m.group(2)), m.group(1)))
                if method == "PATCH" and (m := re.match(r"^/api/boards/(\d+)$", path)):
                    return self._json(api.rename_board(int(m.group(1)), self._jbody().get("name", "")))
                if method == "DELETE":
                    if m := re.match(r"^/api/boards/(\d+)$", path):
                        return self._json(api.delete_board(int(m.group(1))))
                    if m := re.match(r"^/api/boards/(\d+)/items$", path):
                        return self._json(api.board_remove(int(m.group(1)), self._jbody().get("items", [])))
                    if m := re.match(r"^/api/folders/(\d+)$", path):
                        return self._json(api.remove_folder(int(m.group(1))))
                return self._json({"error": "not found"}, 404)
            except ApiError as e:
                return self._json({"error": str(e)}, e.status)
            except library.NotFound as e:
                return self._json({"error": str(e)}, 404)
            except library.BadRequest as e:
                return self._json({"error": str(e)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                return None
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    return H


class Server:
    """Owns the HTTP server, the model thread and the indexer. `start()` returns once listening."""

    def __init__(self, port: int = DEFAULT_PORT, index: bool = True, warm: bool = True, announce: bool = True):
        from .store import Store

        self.engine = Engine()
        self.indexer = Indexer(self.engine, Store) if index else None
        self.api = Api(self.engine, self.indexer)
        self.httpd = None
        for p in range(port, port + 20) if port else [0]:
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", p), make_handler(self.api, {"127.0.0.1", "localhost"}))
                break
            except OSError:
                continue
        if self.httpd is None:
            raise OSError(f"no free port from {port}")
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        self.warm = warm
        self.announce = announce

    def start(self):
        if self.warm:
            self.engine.warm()
        if self.indexer:
            self.indexer.start()
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="mitsuke-http", daemon=True)
        self.thread.start()
        if self.announce:  # lets `mitsuke web` and the MCP server link to this page
            info = library.serve_file()
            info.parent.mkdir(parents=True, exist_ok=True)
            info.write_text(json.dumps({"pid": os.getpid(), "url": self.url, "version": __version__}))
        return self

    def stop(self):
        if self.announce:
            try:
                if json.loads(library.serve_file().read_text()).get("pid") == os.getpid():
                    library.serve_file().unlink()
            except (OSError, ValueError):
                pass
        if self.indexer:
            self.indexer.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.engine.pool.shutdown(wait=False, cancel_futures=True)
