"""`mitsuke mcp`: an MCP server on stdin/stdout, so an agent can search the user's images with tools.

Plain JSON-RPC 2.0, one message per line (the MCP stdio transport) — no SDK, no extra dependencies. Tools return
structured JSON (also as text, for clients that ignore `structuredContent`); `view_images` returns one contact-sheet
image so the agent can look at results.

- The model is loaded on first use and stays loaded (one model thread, as in `mitsuke serve`).
- `index` runs one pass (scan → embed) in the background, holding the same `index.lock` as `mitsuke index` and
  `mitsuke serve`, so they never index at the same time; searches always work.
- stdout carries protocol messages only: everything else that writes to stdout (libraries, progress bars) is sent
  to stderr.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import traceback

from . import __version__, library, thumbs

PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = """Mitsuke searches the user's own image and video folders on this Mac (photos, RAW, PSD, EXR/HDR, video \
shots) by description in any language, by example image, or both. Typical flow: `search` (or `similar`) → show the user \
the best paths (and `view_images` to look at them yourself) → `board_add` to collect picks on a moodboard → `web_link` \
to give the user a link to the page. Image references: file id (12), video shot (s34) or an absolute path. If the \
library is empty, ask the user which folders to add, then `add_folder` and `index`."""

_ref = {"oneOf": [{"type": "integer"}, {"type": "string"}],
        "description": "file id (12), video shot (\"s34\") or an absolute image path"}
_filters = {
    "k": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20, "description": "number of results"},
    "root": {"type": "string", "description": "only inside this folder (a library folder or a sub-folder)"},
    "kinds": {"type": "array", "items": {"type": "string"},
              "description": "only these types: image (= all but video), photo, raw, psd, hdr, video, jpeg, png, exr, …"},
}
_query = {
    "query": {"type": "string", "description": "what to find, in any language"},
    "like": {"type": "array", "items": _ref, "description": "example images to search by (averaged)"},
    "board": {"type": "integer", "description": "search by the images on this moodboard"},
    "text_weight": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5,
                    "description": "with words and images: 0 = only the images, 1 = only the words"},
}


def _tool(name, title, description, props=None, required=(), read_only=True, destructive=False):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(required)},
            "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive, "openWorldHint": False}}


TOOLS = [
    _tool("search", "Search images",
          "Find images and video shots in the user's library by a description (any language), example images, a "
          "moodboard, or a mix. Results: id, score, path, kind, size, thumbnail path (512 px WebP); videos also the "
          "best shot time.", {**_query, **_filters}),
    _tool("similar", "Find similar images", "Images that look like one image (in the library or any path).",
          {"image": _ref, **_filters}, ["image"]),
    _tool("view_images", "Look at images",
          "One contact-sheet image of up to 24 thumbnails labelled with their ids, to look at results yourself.",
          {"images": {"type": "array", "items": _ref, "maxItems": 24}}, ["images"]),
    _tool("file_info", "File details", "Path, type, size, thumbnail, video shots and moodboards of one indexed file.",
          {"image": _ref}, ["image"]),
    _tool("open_file", "Open or reveal a file",
          "Open a file in its default app, or reveal it in Finder (only when the user asks).",
          {"image": _ref, "reveal": {"type": "boolean", "default": False}}, ["image"], read_only=False),
    _tool("list_boards", "List moodboards", "Moodboards with item counts."),
    _tool("board_show", "Show a moodboard", "The items on a moodboard.", {"board": {"type": "integer"}}, ["board"]),
    _tool("board_create", "Create a moodboard", "A new moodboard, optionally with images on it.",
          {"name": {"type": "string"}, "images": {"type": "array", "items": _ref}}, ["name"], read_only=False),
    _tool("board_add", "Add to a moodboard", "Put library images or video shots on a moodboard.",
          {"board": {"type": "integer"}, "images": {"type": "array", "items": _ref}}, ["board", "images"], read_only=False),
    _tool("board_remove", "Remove from a moodboard", "Take images off a moodboard (files are untouched).",
          {"board": {"type": "integer"}, "images": {"type": "array", "items": _ref}}, ["board", "images"], read_only=False),
    _tool("board_rename", "Rename a moodboard", "", {"board": {"type": "integer"}, "name": {"type": "string"}},
          ["board", "name"], read_only=False),
    _tool("board_delete", "Delete a moodboard", "Delete a moodboard (the images are untouched).",
          {"board": {"type": "integer"}}, ["board"], read_only=False, destructive=True),
    _tool("status", "Library status", "Folders, what is indexed / pending / failed, and whether indexing is running."),
    _tool("add_folder", "Add a folder", "Add a folder to the library (then call `index`).",
          {"path": {"type": "string"}}, ["path"], read_only=False),
    _tool("remove_folder", "Remove a folder", "Remove a folder from the library (the files are untouched).",
          {"folder": {"oneOf": [{"type": "integer"}, {"type": "string"}], "description": "folder id or path"}},
          ["folder"], read_only=False, destructive=True),
    _tool("index", "Index the library",
          "Index new and changed files in the background. Waits up to `wait_seconds` and returns progress; call "
          "`status` later for the rest. Safe to call again.",
          {"wait_seconds": {"type": "number", "minimum": 0, "maximum": 600, "default": 60}}, read_only=False),
    _tool("web_link", "Link to the web app",
          "A link that opens Mitsuke's local web page on a search or a moodboard (for the user). With start=true the "
          "page is started in the background if it is not running.",
          {**{k: v for k, v in _query.items()}, "start": {"type": "boolean", "default": False}}, read_only=False),
]


class ToolError(Exception):
    pass


class Mitsuke:
    """Tool implementations. One store per thread; model work goes through the shared engine."""

    def __init__(self, index: bool = True):
        from .model import model_id
        from .server import Engine

        self.engine = Engine()
        self.model = model_id()
        self.can_index = index
        self.job = None  # background index pass
        self.job_state = {"state": "off"}
        self._local = threading.local()

    @property
    def store(self):
        s = getattr(self._local, "store", None)
        if s is None:
            from .store import Store

            s = self._local.store = Store()
        return s

    def tokens(self) -> int:
        from .model import QUALITY_TOKENS

        return int(self.store.get_meta("tokens", QUALITY_TOKENS["default"]))

    # helpers
    def _hits(self, text, refs, text_weight, k=20, root=None, kinds=None):
        from .search import check_kinds, combined

        check_kinds(kinds)

        vecs, ex_files, ex_shots = library.query_vectors(
            self.store, self.model, refs, lambda ims: self.engine.embed_images(ims, tokens=self.tokens()))
        tv = self.engine.embed_texts([text])[0] if text else None
        return combined(self.store, self.model, text_vec=tv, image_vecs=vecs, text_weight=text_weight,
                        k=max(1, min(int(k), 200)), root=root or None, kinds=kinds or None,
                        exclude=ex_files, exclude_shots=ex_shots)

    def _empty_hint(self):
        n = self.store.db.execute("SELECT COUNT(*) FROM files WHERE status='ok'").fetchone()[0]
        if n:
            return None
        if not self.store.roots():
            return "the library is empty: ask the user which folders to add, then add_folder and index"
        return "nothing indexed yet: call index (or wait for it to finish; see status)"

    def _web(self, text="", refs=(), board=None, show_board=None, text_weight=0.5, kinds=None, root=None):
        base = library.running_server()
        if not base:
            return None
        return library.web_url(base, text, [library.parse_ref(self.store, r) for r in refs or []], board_id=board,
                               show_board=show_board, text_weight=text_weight, kinds=kinds, root=root)

    def _file_id(self, ref) -> int:
        r = library.parse_ref(self.store, ref)
        if "shot" in r:
            return library.board_item(self.store, r)["file"]
        if r.get("file") is None:
            raise ToolError(f"not in the library: {r['path']}")
        return int(r["file"])

    def _search_result(self, hits, extra, web):
        out = {**extra, "count": len(hits), **library.index_note(self.store), "results": hits, "web": web}
        if not hits and (hint := self._empty_hint()):
            out["hint"] = hint
        return out

    # tools
    def search(self, query="", like=None, board=None, text_weight=0.5, k=20, root=None, kinds=None):
        text = (query or "").strip()
        refs = list(like or [])
        if board:
            refs += library.board_refs(self.store, int(board))
        if not text and not refs:
            raise ToolError("give a query, example images (like) or a board")
        hits = self._hits(text, refs, float(text_weight), k, root, kinds)
        return self._search_result(hits, {"query": text}, self._web(text, like, board, None, float(text_weight), kinds, root))

    def similar(self, image, k=20, root=None, kinds=None):
        hits = self._hits("", [image], 0.0, k, root, kinds)
        return self._search_result(hits, {"image": image}, self._web(refs=[image], kinds=kinds, root=root))

    def view_images(self, images):
        cells = []
        for ref in images[:24]:
            r = library.parse_ref(self.store, ref)
            if r.get("path") and r.get("file") is None:  # any image on disk, not only the library
                cells.append((os.path.basename(r["path"]), r["path"]))
                continue
            it = library.board_item(self.store, r)
            f = self.store.files_by_ids([it["file"]])[it["file"]]
            if it["shot"]:
                s = self.store.shots_by_ids([it["shot"]])[it["shot"]]
                cells.append((f"s{it['shot']}", thumbs.thumb_path(f"{f['sha1']}-{s['idx']}")))
            else:
                cells.append((f"#{f['id']}", thumbs.thumb_path(f["sha1"]) if f["sha1"] else None))
        if not cells:
            raise ToolError("no images given")
        return _ImageResult(contact_sheet(cells), {"images": [c[0] for c in cells]})

    def file_info(self, image):
        info = library.item(self.store, self._file_id(image))
        info["web"] = self._web(refs=[info["id"]])
        return info

    def open_file(self, image, reveal=False):
        info = library.item(self.store, self._file_id(image))
        if not info["exists"]:
            raise ToolError(f"the file is missing: {info['path']}")
        if sys.platform == "darwin" and os.environ.get("MITSUKE_NO_DESKTOP") != "1":
            subprocess.run(["open", "-R", info["path"]] if reveal else ["open", info["path"]], check=False)
        return {"path": info["path"], "action": "reveal" if reveal else "open"}

    def list_boards(self):
        return {"boards": library.list_boards(self.store)}

    def board_show(self, board):
        b = library.board(self.store, int(board))
        return {**b, "web": self._web(show_board=b["id"])}

    def board_create(self, name, images=None):
        b = library.create_board(self.store, name)
        if images:
            library.board_add(self.store, b["id"], [library.board_item(self.store, r) for r in images])
        return self.board_show(b["id"])

    def board_add(self, board, images):
        library.board_add(self.store, int(board), [library.board_item(self.store, r) for r in images])
        return self.board_show(board)

    def board_remove(self, board, images):
        library.board_remove(self.store, int(board), [library.board_item(self.store, r) for r in images])
        return self.board_show(board)

    def board_rename(self, board, name):
        return library.rename_board(self.store, int(board), name)

    def board_delete(self, board):
        return library.delete_board(self.store, int(board))

    def status(self):
        from .cli import _root_stats
        from .indexer import pending_count

        roots = _root_stats(self.store)
        kinds = dict(self.store.db.execute("SELECT kind, COUNT(*) FROM files WHERE status='ok' GROUP BY kind"))
        errors = [{"path": os.path.join(r["root"], r["relpath"]), "error": r["error"]} for r in self.store.db.execute(
            "SELECT r.path root, f.relpath, f.error FROM files f JOIN roots r ON r.id=f.root_id "
            "WHERE f.status='error' ORDER BY f.indexed_at DESC LIMIT 5")]
        return {"version": __version__, "model": self.model, "model_loaded": self.engine.loaded,
                "indexed": sum(r["ok"] for r in roots), "pending": pending_count(self.store),
                "errors": sum(r["error"] for r in roots), "folders": roots, "kinds": kinds, "recent_errors": errors,
                "indexing": self.job_state, "web": library.running_server()}

    def add_folder(self, path):
        return {**library.add_folder(self.store, path), "next": "call index to index it"}

    def remove_folder(self, folder):
        s = str(folder).lstrip("#")
        p = os.path.realpath(os.path.abspath(os.path.expanduser(s)))
        match = [r for r in self.store.roots() if r["path"] == p or str(r["id"]) == s]
        if not match:
            raise ToolError(f"not a library folder: {folder} (see status)")
        return {"removed": match[0]["path"], "files": self.store.remove_root(match[0]["id"])}

    def index(self, wait_seconds=60):
        if not self.can_index:
            raise ToolError("indexing is turned off for this server (--no-index); run `mitsuke index` instead")
        if not self.store.roots():
            raise ToolError("no folders yet: add_folder first")
        if self.job is None or not self.job.is_alive():
            self.job = threading.Thread(target=self._index_pass, name="mitsuke-mcp-index", daemon=True)
            self.job_state = {"state": "starting"}
            self.job.start()
        t = time.time()
        while self.job.is_alive() and time.time() - t < float(wait_seconds):
            self.job.join(0.25)
        st = self.status()
        return {"indexing": st["indexing"], "indexed": st["indexed"], "pending": st["pending"], "errors": st["errors"],
                "done": not self.job.is_alive()}

    def _index_pass(self):
        from .server import Indexer
        from .store import Store

        ix = Indexer(self.engine, Store)
        if not ix._lock():
            self.job_state = {"state": "external", "detail": "another `mitsuke index` / `mitsuke serve` is indexing; "
                              "it keeps the index current, searches work meanwhile"}
            return
        store = Store()
        ix.state = self.job_state
        stop = threading.Event()

        def mirror():  # Indexer updates ix.state; keep ours in step for `status`
            while not stop.wait(0.2):
                self.job_state = ix.state

        m = threading.Thread(target=mirror, daemon=True)
        m.start()
        try:
            ix._pass(store)
            self.job_state = {**ix.state, "state": "done"}
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.job_state = {"state": "error", "message": f"{type(e).__name__}: {e}"[:300]}
        finally:
            stop.set()
            store.close()
            ix.lockf.close()  # releases the lock

    def web_link(self, query="", like=None, board=None, text_weight=0.5, start=False):
        from .cli import CliError, start_server

        if start:
            try:
                start_server()
            except CliError as e:
                raise ToolError(str(e)) from None
        text = (query or "").strip()
        url = self._web(text, like, board if (text or like) else None, None if (text or like) else board, float(text_weight))
        if url is None:
            return {"running": False, "url": None, "hint": "call web_link with start=true (or run `mitsuke serve`)"}
        return {"running": True, "url": url}


class _ImageResult:
    def __init__(self, jpeg: bytes, meta: dict):
        self.jpeg, self.meta = jpeg, meta


def contact_sheet(cells, cell: int = 256) -> bytes:
    """Thumbnails in a grid with their labels → JPEG bytes."""
    from PIL import Image, ImageDraw, ImageFont

    cols = min(len(cells), 6) or 1
    rows = (len(cells) + cols - 1) // cols
    label_h = 22
    sheet = Image.new("RGB", (cols * cell, rows * (cell + label_h)), (24, 24, 26))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=16)
    except TypeError:
        font = ImageFont.load_default()
    for i, (label, thumb) in enumerate(cells):
        x, y = i % cols * cell, i // cols * (cell + label_h)
        try:
            try:
                im = Image.open(thumb).convert("RGB")
            except OSError:  # RAW / PSD / EXR … given by path
                from .decode import load_preview

                im = load_preview(thumb, cell).image.convert("RGB")
            im.thumbnail((cell - 8, cell - 8))
            sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
        except Exception:  # noqa: BLE001 — a cell without a picture is still useful
            draw.text((x + 8, y + cell // 2), "(no thumbnail)", fill=(160, 160, 160), font=font)
        draw.text((x + 6, y + cell + 2), label, fill=(235, 235, 235), font=font)
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=82)
    return buf.getvalue()


# --- JSON-RPC over stdio ----------------------------------------------------------------------

def _clean(obj):
    """JSON-safe (numpy scalars, paths)."""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=lambda o: o.item() if hasattr(o, "item") else str(o)))


class Server:
    def __init__(self, out, index: bool = True):
        self.out = out
        self.write_lock = threading.Lock()
        self.app = None
        self.index = index

    def send(self, msg):
        with self.write_lock:
            self.out.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.out.flush()

    def call_tool(self, name, args):
        import base64

        if name not in {t["name"] for t in TOOLS}:
            return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
        if self.app is None:
            self.app = Mitsuke(index=self.index)
        try:
            res = getattr(self.app, name)(**(args or {}))
        except TypeError as e:
            return {"content": [{"type": "text", "text": f"bad arguments: {e}"}], "isError": True}
        except (ToolError, library.NotFound, library.BadRequest, ValueError) as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        if isinstance(res, _ImageResult):
            return {"content": [{"type": "image", "data": base64.b64encode(res.jpeg).decode(), "mimeType": "image/jpeg"},
                                {"type": "text", "text": json.dumps(res.meta, ensure_ascii=False)}]}
        res = _clean(res)
        return {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}], "structuredContent": res}

    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            if isinstance(msg, dict) and "id" in msg and "method" not in msg:
                return None  # a response to something we never ask; ignore
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:  # notification
            return None
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                result = {
                    "protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "mitsuke", "title": "Mitsuke 見つけ", "version": __version__},
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self.call_tool(params.get("name"), params.get("arguments"))
            elif method in ("resources/list", "prompts/list", "resources/templates/list"):
                result = {method.split("/")[0] if "templates" not in method else "resourceTemplates": []}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def run(self, inp):
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if isinstance(msg, list):  # batch (protocol 2025-03-26)
                replies = [r for r in (self.handle(m) for m in msg) if r is not None]
                if replies:
                    self.send(replies)
            elif (reply := self.handle(msg)) is not None:
                self.send(reply)
        return 0


def serve_stdio(index: bool = True) -> int:
    # Keep the real stdout for protocol messages; point fd 1 at stderr so stray prints cannot corrupt the stream.
    proto = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    try:
        from huggingface_hub.utils import disable_progress_bars

        disable_progress_bars()
    except ImportError:
        pass
    inp = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    try:
        return Server(proto, index=index).run(inp)
    except KeyboardInterrupt:
        return 0
