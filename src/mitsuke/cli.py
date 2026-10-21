"""`mitsuke` command line. Every command takes `--json` (one JSON object on stdout; `index` streams JSON lines)."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

from . import __version__, paths
from .model import QUALITY_TOKENS, model_id

EXIT_ERROR, EXIT_INTERRUPTED = 1, 130


class CliError(Exception):
    pass


class Out:
    def __init__(self, as_json: bool):
        self.json = as_json
        self.tty = sys.stderr.isatty()
        self._progress = False

    def emit(self, obj):
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    def say(self, text: str = ""):
        if not self.json:
            self.end_progress()
            print(text, flush=True)

    def progress(self, text: str):
        if self.json:
            return
        if self.tty:
            sys.stderr.write("\r\033[K" + text)
            sys.stderr.flush()
            self._progress = True

    def end_progress(self):
        if self._progress:
            sys.stderr.write("\n")
            self._progress = False


def _store():
    from .store import Store

    return Store()


def _abs(p: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(p)))


def _tokens(store, quality: str | None) -> int:
    if quality:
        return QUALITY_TOKENS[quality]
    return int(store.get_meta("tokens", QUALITY_TOKENS["default"]))


def _fmt_eta(s):
    if s is None:
        return "?"
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


# --- commands --------------------------------------------------------------------------------

def cmd_add(a, out: Out):
    from . import library

    store = _store()
    added = []
    for f in a.folders:
        r = _lib_call(library.add_folder, store, f)
        p, new = r["path"], r["new"]
        added.append(r)
        out.say(f"{'added' if new else 'already added'}: {p}")
    if out.json:
        out.emit({"added": added})
    else:
        out.say("run `mitsuke index` to index the new folders")


def cmd_remove(a, out: Out):
    store = _store()
    removed = []
    for f in a.folders:
        p = _abs(f)
        match = [r for r in store.roots() if r["path"] == p or str(r["id"]) == f.lstrip("#")]
        if not match:
            raise CliError(f"not a library folder: {f} (see `mitsuke roots`)")
        n = store.remove_root(match[0]["id"])
        removed.append({"path": match[0]["path"], "files": n})
        out.say(f"removed {match[0]['path']} ({n} files dropped from the index; your images are untouched)")
    if out.json:
        out.emit({"removed": removed})


def _root_stats(store):
    rows = store.db.execute(
        "SELECT r.id, r.path, f.status, COUNT(f.id) n FROM roots r LEFT JOIN files f ON f.root_id=r.id "
        "GROUP BY r.id, f.status ORDER BY r.path"
    ).fetchall()
    roots = {}
    for r in rows:
        d = roots.setdefault(r["id"], {"id": r["id"], "path": r["path"], "online": os.path.isdir(r["path"]),
                                       "ok": 0, "pending": 0, "error": 0})
        if r["status"]:
            d[r["status"]] = r["n"]
    return list(roots.values())


def cmd_roots(a, out: Out):
    roots = _root_stats(_store())
    if out.json:
        out.emit({"roots": roots})
        return
    if not roots:
        out.say("no folders yet: mitsuke add <folder>")
    for r in roots:
        flag = "" if r["online"] else "  [offline]"
        out.say(f"#{r['id']}  {r['path']}  ({r['ok']} indexed, {r['pending']} pending, {r['error']} errors){flag}")


def _lock():
    paths.home_dir().mkdir(parents=True, exist_ok=True)
    f = open(paths.home_dir() / "index.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise CliError("another `mitsuke index` is already running") from None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def _raise_interrupt(*_):
    raise KeyboardInterrupt


def cmd_index(a, out: Out):
    from . import indexer

    lock = _lock()  # noqa: F841 — held for the life of the process
    # Stop cleanly on Ctrl-C and on `kill` (agents run `index --watch` in the background), even if the parent
    # shell started us with SIGINT ignored.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    store = _store()
    if not store.roots():
        raise CliError("no folders yet: mitsuke add <folder>")
    tokens = _tokens(store, a.quality)
    store.set_meta("tokens", tokens)
    model = model_id()
    embedder = None

    def get_embedder():
        nonlocal embedder
        if embedder is None:
            from .model import get_embedder as ge

            out.progress("loading model…")
            t = time.perf_counter()
            embedder = ge()
            if out.json:
                out.emit({"event": "model", "model": embedder.name, "load_s": round(time.perf_counter() - t, 1)})
        return embedder

    def progress(ev):
        if out.json:
            out.emit(ev)
        else:
            out.progress(f"indexing {ev['done']}/{ev['total']}  {ev['per_s']} img/s  eta {_fmt_eta(ev['eta_s'])}")

    def one_pass(root_ids=None, quiet=False):
        st = indexer.sync(store, root_ids)
        requeued = indexer.requeue_for(store, model, tokens)
        sync_ev = {"event": "sync", "new": st.new, "changed": st.changed, "moved": st.moved, "deleted": st.deleted,
                   "touched": st.touched, "unchanged": st.unchanged, "requeued": requeued, "offline": st.offline}
        if out.json:
            if not quiet or st.any():
                out.emit(sync_ev)
        elif not quiet or st.any():
            parts = [f"{k} {v}" for k, v in sync_ev.items() if k not in ("event", "offline", "unchanged") and v]
            out.say("scan: " + (", ".join(parts) if parts else "no changes") + (f" (offline: {', '.join(st.offline)})" if st.offline else ""))
        pending = indexer.pending_count(store)
        if pending:
            counts = indexer.embed_pending(store, get_embedder(), tokens, limit=a.limit, on_progress=progress)
            if out.json:
                out.emit({"event": "indexed", **counts})
            else:
                out.say(f"indexed {counts['ok']} images ({counts['error']} errors) in {_fmt_eta(counts['seconds'])}"
                        + (" — see `mitsuke status` for errors" if counts["error"] else ""))
        return st

    if a.retry_errors:
        n = indexer.retry_errors(store)
        out.say(f"retrying {n} files that failed before")
    try:
        one_pass()
        if a.watch:
            from . import watch

            roots = {r["id"]: r["path"] for r in store.roots() if os.path.isdir(r["path"])}
            if out.json:
                out.emit({"event": "watching", "roots": list(roots.values())})
            else:
                out.say(f"watching {len(roots)} folders for changes (Ctrl-C to stop)")
            watch.run(store, roots, lambda dirty: one_pass(dirty, quiet=True))
        elif out.json:
            out.emit({"event": "done", "pending": indexer.pending_count(store)})
    except KeyboardInterrupt:
        left = indexer.pending_count(store)
        out.end_progress()
        if out.json:
            out.emit({"event": "interrupted", "pending": left})
        else:
            out.say(f"stopped. {left} images still to index — run `mitsuke index` again to continue.")
        return EXIT_INTERRUPTED


def _print_hits(out: Out, store, hits, header: dict):
    from .library import index_note

    note = index_note(store)
    if out.json:
        out.emit({**header, **note, "results": hits})
        return
    if not hits:
        out.say("no results (is anything indexed? `mitsuke status`)")
    for i, h in enumerate(hits, 1):
        at = f"  @ {_fmt_time(h['time'])}" if "time" in h else ""
        out.say(f"{i:>3}. {h['score']:.3f}  {h['path']}{at}")
    if "note" in note:
        out.say(f"note: {note['note']}")


def _fmt_time(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def _open(path: str):
    if sys.platform == "darwin":
        subprocess.run(["open", path], check=False)


def _kinds(a):
    from .search import check_kinds

    kinds = [k.strip().lower() for v in (getattr(a, "kind", None) or []) for k in v.split(",") if k.strip()] or None
    try:
        return check_kinds(kinds)
    except ValueError as e:
        raise CliError(str(e)) from None


def _query_images(store, a):
    from . import library

    refs = list(a.like or [])
    if a.board:
        refs += library.board_refs(store, a.board)
    return refs


def _web_link(text="", refs=(), a=None, board_id=None, show_board=None):
    """Link into the web app when `mitsuke serve` is running (else None)."""
    from . import library

    base = library.running_server()
    if not base:
        return None
    return library.web_url(base, text, [r for r in refs if isinstance(r, dict)], board_id=board_id, show_board=show_board,
                           text_weight=0.5 if getattr(a, "text_weight", None) is None else a.text_weight,
                           kinds=_kinds(a) if a else None,
                           root=getattr(a, "root", None))


def _run_search(store, a, out: Out, text: str, header: dict):
    """Text, images (--like / --board) or both, fused by --text-weight."""
    from . import library
    from .search import combined

    refs = _query_images(store, a)
    if not text and not refs:
        raise CliError("give a description, --like <image> or --board <id>")
    if a.text_weight is not None and not 0 <= a.text_weight <= 1:
        raise CliError("--text-weight must be between 0 and 1")
    lazy = {}

    def emb():
        if "e" not in lazy:
            from .model import get_embedder

            lazy["e"] = get_embedder()
        return lazy["e"]

    try:
        vecs, ex_files, ex_shots = library.query_vectors(store, model_id(), refs, lambda ims: emb().embed_images(ims, tokens=_tokens(store, None)))
    except (library.NotFound, library.BadRequest) as e:
        raise CliError(str(e)) from None
    tv = emb().embed_texts([text])[0] if text else None
    w = 0.5 if a.text_weight is None else a.text_weight
    hits = combined(store, model_id(), text_vec=tv, image_vecs=vecs, text_weight=w, k=a.k, root=a.root,
                    kinds=_kinds(a), exclude=ex_files, exclude_shots=ex_shots)
    parsed = [library.parse_ref(store, r) for r in a.like or []]
    if refs:
        header = {**header, "images": [str(r) for r in a.like or []], **({"board": a.board} if a.board else {}),
                  **({"text_weight": w} if text else {})}
    _print_hits(out, store, hits, {**header, "web": _web_link(text, parsed, a, board_id=a.board)})
    if a.open and hits:
        _open(hits[0]["path"])


def cmd_search(a, out: Out):
    query = " ".join(a.query).strip()
    _run_search(_store(), a, out, query, {"query": query})


def cmd_similar(a, out: Out):
    from .model import get_embedder
    from .search import image_vector, rank

    store = _store()
    try:
        fid, path, vec = image_vector(store, model_id(), a.image, get_embedder, _tokens(store, None))
    except FileNotFoundError as e:
        raise CliError(f"not found: {e}") from None
    hits = rank(store, model_id(), vec, k=a.k, root=a.root, kinds=_kinds(a), exclude={fid} if fid else None)
    _print_hits(out, store, hits, {"image": path, "in_library": fid is not None,
                            "web": _web_link(refs=[{"file": fid}] if fid else [], a=a)})
    if a.open and hits:
        _open(hits[0]["path"])


def cmd_status(a, out: Out):
    from .indexer import pending_count

    store = _store()
    roots = _root_stats(store)
    errors = [
        {"path": os.path.join(r["root"], r["relpath"]), "error": r["error"]}
        for r in store.db.execute(
            "SELECT r.path root, f.relpath, f.error FROM files f JOIN roots r ON r.id=f.root_id "
            "WHERE f.status='error' ORDER BY f.indexed_at DESC LIMIT ?", (a.errors,))
    ]
    kinds = {r[0]: r[1] for r in store.db.execute("SELECT kind, COUNT(*) FROM files WHERE status='ok' GROUP BY kind ORDER BY 2 DESC")}
    tokens = int(store.get_meta("tokens", QUALITY_TOKENS["default"]))
    quality = next((q for q, t in QUALITY_TOKENS.items() if t == tokens), str(tokens))
    info = {
        "version": __version__, "model": model_id(), "quality": quality, "index": str(store.path),
        "index_mb": round(sum(os.path.getsize(p) for p in [store.path, store.path.with_name(store.path.name + "-wal")] if p.exists()) / 2**20, 1),
        "indexed": sum(r["ok"] for r in roots), "pending": pending_count(store), "errors": sum(r["error"] for r in roots),
        "roots": roots, "kinds": kinds, "recent_errors": errors,
    }
    if out.json:
        out.emit(info)
        return
    out.say(f"Mitsuke {info['version']} — {info['indexed']} images indexed, {info['pending']} pending, {info['errors']} errors")
    out.say(f"model {info['model']} · quality {quality} · index {info['index']} ({info['index_mb']} MB)")
    for r in roots:
        out.say(f"  #{r['id']} {r['path']}: {r['ok']} indexed, {r['pending']} pending, {r['error']} errors" + ("" if r["online"] else "  [offline]"))
    if kinds:
        out.say("  by type: " + ", ".join(f"{k} {v}" for k, v in kinds.items()))
    for e in errors:
        out.say(f"  ! {e['path']}: {e['error']}")


def cmd_serve(a, out: Out):
    from .server import Server

    srv = Server(port=a.port, index=not a.no_index).start()
    if out.json:
        out.emit({"event": "listening", "url": srv.url, "indexing": not a.no_index})
    else:
        out.say(f"Mitsuke is running at {srv.url}  (only this Mac can reach it; Ctrl-C to stop)")
        if srv.indexer is not None:
            out.say("indexing and watching your folders in the background")
    if a.open and sys.platform == "darwin":
        subprocess.run(["open", srv.url], check=False)
    signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
        if out.json:
            out.emit({"event": "stopped"})
        else:
            out.say("stopped")


def _lib_call(fn, *args):
    from . import library

    try:
        return fn(*args)
    except (library.NotFound, library.BadRequest) as e:
        raise CliError(str(e)) from None


def _print_board(out: Out, b: dict):
    b = {**b, "web": _web_link(show_board=b["id"])}
    if out.json:
        out.emit(b)
        return
    out.say(f"board #{b['id']} {b['name']} — {len(b['results'])} items" + (f"  {b['web']}" if b["web"] else ""))
    for h in b["results"]:
        at = f"  @ {_fmt_time(h['time'])}" if "time" in h else ""
        shot = f" s{h['shot']}" if "shot" in h else ""
        out.say(f"  #{h['id']}{shot}  {h['path']}{at}")


def cmd_board(a, out: Out):
    from . import library

    store = _store()
    act = a.action
    if act == "list":
        boards = library.list_boards(store)
        if out.json:
            out.emit({"boards": boards})
        elif not boards:
            out.say('no boards yet: mitsuke board create "<name>"')
        for b in boards if not out.json else []:
            out.say(f"#{b['id']}  {b['name']}  ({b['count']} items)")
        return
    if act == "create":
        b = library.create_board(store, " ".join(a.args))
        if a.items:
            _lib_call(library.board_add, store, b["id"], [_lib_call(library.board_item, store, r) for r in a.items])
        return _print_board(out, library.board(store, b["id"]))
    if not a.args or not a.args[0].lstrip("#").isdigit():
        raise CliError(f"usage: mitsuke board {act} <board id> …  (see `mitsuke board list`)")
    bid, rest = int(a.args[0].lstrip("#")), a.args[1:]
    if act == "show":
        return _print_board(out, _lib_call(library.board, store, bid))
    if act in ("add", "remove"):
        if not rest:
            raise CliError(f"usage: mitsuke board {act} <board id> <image or #id or s<shot>>…")
        items = [_lib_call(library.board_item, store, r) for r in rest]
        fn = library.board_add if act == "add" else library.board_remove
        return _print_board(out, _lib_call(fn, store, bid, items))
    if act == "rename":
        r = _lib_call(library.rename_board, store, bid, " ".join(rest))
    else:
        r = _lib_call(library.delete_board, store, bid)
    if out.json:
        out.emit(r)
    else:
        out.say(f"renamed #{bid} to {r['name']}" if act == "rename" else f"deleted board #{bid} (the images are untouched)")


def cmd_show(a, out: Out):
    from . import library

    store = _store()
    ref = _lib_call(library.parse_ref, store, a.image)
    if "shot" in ref:
        ref = _lib_call(library.board_item, store, ref)
    if ref.get("file") is None:
        raise CliError(f"not in the library: {ref['path']}")
    info = _lib_call(library.item, store, ref["file"])
    info["web"] = _web_link(refs=[{"file": info["id"]}])
    if (a.open or a.reveal) and info["exists"] and sys.platform == "darwin":
        subprocess.run(["open", "-R", info["path"]] if a.reveal else ["open", info["path"]], check=False)
    if out.json:
        out.emit(info)
        return
    out.say(f"#{info['id']}  {info['path']}" + ("" if info["exists"] else "  [missing]"))
    dims = f"{info['width']}×{info['height']}" if info.get("width") else "?"
    out.say(f"  {info['kind']} · {dims}" + (f" · {_fmt_time(info['duration'])}" if info.get("duration") else "")
            + (f" · on boards {', '.join(f'#{b}' for b in info['boards'])}" if info["boards"] else ""))
    out.say(f"  thumbnail {info['thumb']}")


def start_server(port: int | None = None, timeout: float = 60) -> str:
    """Start `mitsuke serve` in the background (detached, logs in the data folder); return its URL."""
    from . import library

    url = library.running_server()
    if url:
        return url
    paths.home_dir().mkdir(parents=True, exist_ok=True)
    log = open(paths.home_dir() / "serve.log", "ab")
    cmd = [sys.executable, "-m", "mitsuke", "serve"] + (["--port", str(port)] if port is not None else [])
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    t = time.time()
    while time.time() - t < timeout:
        time.sleep(0.3)
        if url := library.running_server():
            return url
    raise CliError(f"the web app did not start; see {paths.home_dir() / 'serve.log'}")


def cmd_web(a, out: Out):
    from . import library

    store = _store()
    base = start_server(a.port) if a.start else library.running_server()
    text = " ".join(a.query).strip()
    refs = [_lib_call(library.parse_ref, store, r) for r in a.like or []]
    url = library.web_url(base, text, refs, board_id=a.board if text or refs else None,
                          show_board=None if text or refs else a.board, text_weight=a.text_weight, kinds=_kinds(a),
                          root=a.root) if base else None
    if url and a.open and sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
    if out.json:
        out.emit({"running": bool(base), "url": url, **({} if base else {"hint": "mitsuke web --start"})})
    else:
        out.say(url or "the web app is not running: `mitsuke web --start` (or `mitsuke serve`)")


SKILL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill")


def cmd_skill(a, out: Out):
    import shutil

    if a.action == "show":
        print(open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8").read(), end="")
        return
    dest = os.path.join(_abs(a.dir or "~/.claude/skills"), "mitsuke")
    if a.action == "path":
        info = {"skill": SKILL_DIR, "installed": dest if os.path.isfile(os.path.join(dest, "SKILL.md")) else None}
    else:
        os.makedirs(dest, exist_ok=True)
        shutil.copytree(SKILL_DIR, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        info = {"installed": dest}
    if out.json:
        out.emit(info)
    else:
        out.say(f"skill installed in {dest}" if a.action == "install" else f"bundled skill: {SKILL_DIR}\ninstalled: {info['installed'] or 'no'}")


def cmd_mcp(a, out: Out):
    from .mcp import serve_stdio

    return serve_stdio(index=not a.no_index)


def cmd_doctor(a, out: Out):
    from . import doctor

    checks = doctor.run(download=a.download, embed=not a.no_embed)
    worst = "fail" if any(c["status"] == "fail" for c in checks) else "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    if out.json:
        out.emit({"status": worst, "checks": checks})
    else:
        icon = {"ok": "✓", "warn": "!", "fail": "✗"}
        for c in checks:
            out.say(f"{icon[c['status']]} {c['name']}: {c['detail']}" + (f"\n    → {c['fix']}" if c.get("fix") else ""))
        out.say({"ok": "all good", "warn": "usable, with warnings", "fail": "not ready"}[worst])
    return 0 if worst != "fail" else EXIT_ERROR


# --- parser ----------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def __init__(self, *a, **kw):
        kw.setdefault("allow_abbrev", False)  # `--k` must not silently mean `--kind`
        super().__init__(*a, **kw)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS on the sub-command copy so `mitsuke --json search …` and `mitsuke search … --json` both work
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output (for agents and scripts)")
    p = argparse.ArgumentParser(prog="mitsuke", description="Local AI image search (EmbeddingGemma 2).", allow_abbrev=False)
    p.add_argument("--json", action="store_true", help="machine-readable output (for agents and scripts)")
    p.add_argument("--version", action="version", version=f"mitsuke {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command", parser_class=_Parser)

    s = sub.add_parser("add", parents=[common], help="add folders to the library")
    s.add_argument("folders", nargs="+")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("remove", parents=[common], help="remove folders from the library (files are not touched)")
    s.add_argument("folders", nargs="+", help="folder path or #id from `mitsuke roots`")
    s.set_defaults(fn=cmd_remove)
    s = sub.add_parser("roots", parents=[common], help="list library folders")
    s.set_defaults(fn=cmd_roots)

    s = sub.add_parser("index", parents=[common], help="index new and changed images (safe to stop and re-run)")
    s.add_argument("--watch", action="store_true", help="keep running and index changes as they happen")
    s.add_argument("--quality", choices=list(QUALITY_TOKENS), help="fast / default / best (changing it re-indexes)")
    s.add_argument("--limit", type=int, help="index at most N images this run")
    s.add_argument("--retry-errors", action="store_true", help="try files that failed before again")
    s.set_defaults(fn=cmd_index)

    def filters(s, open_help="open the best match"):
        s.add_argument("-k", "--k", "--top", dest="k", type=int, default=20, help="number of results (default 20)")
        s.add_argument("--root", help="only search inside this folder")
        s.add_argument("--kind", action="append", help="only these types, e.g. raw,psd,jpeg,png,heic,tiff,exr,video (or image = all but video)")
        s.add_argument("--open", action="store_true", help=open_help)

    def query_images(s):
        s.add_argument("--like", action="append", metavar="IMAGE", help="also search by this image (path, #id or s<shot>; repeatable)")
        s.add_argument("--board", type=lambda v: int(v.lstrip("#")), help="also search by the images on this moodboard")
        s.add_argument("--text-weight", type=float, help="0 = only the images, 1 = only the words (default 0.5)")

    s = sub.add_parser("search", parents=[common], help="find images by describing them (any language), optionally plus images")
    s.add_argument("query", nargs="*")
    query_images(s)
    filters(s)
    s.set_defaults(fn=cmd_search)
    s = sub.add_parser("similar", parents=[common], help="find images that look like an image")
    s.add_argument("image", help="image path (inside or outside the library) or #id")
    filters(s)
    s.set_defaults(fn=cmd_similar)

    s = sub.add_parser("board", parents=[common], help="moodboards: list | create NAME | show ID | add ID IMAGE… | remove ID IMAGE… | rename ID NAME | delete ID")
    s.add_argument("action", choices=["list", "create", "show", "add", "remove", "rename", "delete"])
    s.add_argument("args", nargs="*", help="board id, name, images (path, #id or s<shot>)")
    s.add_argument("--add", dest="items", action="append", metavar="IMAGE", help="with create: images to put on the new board")
    s.set_defaults(fn=cmd_board)
    s = sub.add_parser("show", parents=[common], help="details of one indexed file (path, thumbnail, boards); --open / --reveal it")
    s.add_argument("image", help="path, #id or s<shot>")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--open", action="store_true", help="open it in its default app")
    g.add_argument("--reveal", action="store_true", help="show it in Finder")
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("web", parents=[common], help="link to the web app (optionally on a search or a board)")
    s.add_argument("query", nargs="*")
    s.add_argument("--like", action="append", metavar="IMAGE")
    s.add_argument("--board", type=lambda v: int(v.lstrip("#")), help="open this board (with a query: search like it)")
    s.add_argument("--text-weight", type=float, default=0.5)
    s.add_argument("--root")
    s.add_argument("--kind", action="append")
    s.add_argument("--start", action="store_true", help="start the web app in the background if it is not running")
    s.add_argument("--port", type=int, help="with --start: port (default 8737)")
    s.add_argument("--open", action="store_true", help="open the link in the browser")
    s.set_defaults(fn=cmd_web)
    s = sub.add_parser("skill", parents=[common], help="the agent skill: install (into ~/.claude/skills) | path | show")
    s.add_argument("action", choices=["install", "path", "show"])
    s.add_argument("--dir", help="skills folder of your agent (default ~/.claude/skills)")
    s.set_defaults(fn=cmd_skill)
    s = sub.add_parser("mcp", parents=[common], help="MCP server on stdin/stdout (for agents: claude mcp add mitsuke -- mitsuke mcp)")
    s.add_argument("--no-index", action="store_true", help="do not offer the indexing tools")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("serve", parents=[common], help="open the web app (local only) and keep the index current")
    s.add_argument("--port", type=int, default=8737, help="port on 127.0.0.1 (default 8737; the next free one is used)")
    s.add_argument("--no-index", action="store_true", help="search only, do not index or watch folders")
    s.add_argument("--open", action="store_true", help="open the page in the browser")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("status", parents=[common], help="what is indexed, pending, failed")
    s.add_argument("--errors", type=int, default=10, help="show the last N errors")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("doctor", parents=[common], help="check the setup")
    s.add_argument("--download", action="store_true", help="download the model if missing")
    s.add_argument("--no-embed", action="store_true", help="skip the model test")
    s.set_defaults(fn=cmd_doctor)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    out = Out(a.json)
    try:
        return a.fn(a, out) or 0
    except CliError as e:
        out.end_progress()
        if out.json:
            out.emit({"error": str(e)})
        else:
            print(f"mitsuke: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        out.end_progress()
        return EXIT_INTERRUPTED
