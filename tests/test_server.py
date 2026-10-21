"""The web server end to end over real HTTP (fake embedder, no model, no browser)."""

import json
import struct
import time
import urllib.error
import urllib.request

import pytest

from mitsuke.server import Server


@pytest.fixture
def server(library, monkeypatch):
    monkeypatch.setenv("MITSUKE_NO_DESKTOP", "1")
    srv = Server(port=0).start()
    yield srv
    srv.stop()


def call(srv, method, path, body=None, raw=None, headers=None, expect=200):
    h = {"X-Mitsuke": "1", **(headers or {})}
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if body is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(srv.url.rstrip("/") + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status, payload, ctype = r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        status, payload, ctype = e.code, e.read(), e.headers.get("Content-Type", "")
    assert status == expect, (status, payload[:300])
    return json.loads(payload) if "json" in ctype else payload


def wait_indexed(srv, n, timeout=30):
    t = time.time()
    while time.time() - t < timeout:
        st = call(srv, "GET", "/api/status")
        if st["indexed"] >= n and st["pending"] == 0 and st["indexer"]["state"] in ("watching", "idle"):
            return st
        time.sleep(0.1)
    raise AssertionError(st)


def test_page_and_static(server):
    html = call(server, "GET", "/")
    assert b"<html" in html and b"/static/app.js" in html
    assert b"function" in call(server, "GET", "/static/app.js")
    call(server, "GET", "/static/../server.py", expect=404)
    call(server, "GET", "/t/../../etc.webp", expect=404)


def test_security(server):
    call(server, "POST", "/api/boards", {"name": "x"}, headers={"X-Mitsuke": "0"}, expect=403)
    call(server, "GET", "/api/status", headers={"Host": "evil.example:8737"}, expect=403)


def test_folders_index_search_boards(server, library):
    r = call(server, "POST", "/api/folders", {"path": str(library)})
    assert r["new"]
    call(server, "POST", "/api/folders", {"path": str(library / "nope")}, expect=400)
    st = wait_indexed(server, 7)
    assert "voice" not in st and st["kinds"]["png"] == 6
    f = call(server, "GET", "/api/folders")["folders"][0]
    assert f["ok"] == 7 and {c["name"]: c["count"] for c in f["children"]} == {"set0": 3, "set1": 3}

    res = call(server, "POST", "/api/search", {"text": "雪の中の鳥居", "k": 5})["results"]
    assert len(res) == 5 and res[0]["thumb"].startswith("/t/") and res[0]["name"]
    assert call(server, "GET", res[0]["thumb"])[:4] == b"RIFF"  # webp

    # similar = image only, the query image itself excluded
    first = res[0]["id"]
    sim = call(server, "POST", "/api/search", {"images": [{"file": first}], "k": 20})["results"]
    assert len(sim) == 6 and first not in [h["id"] for h in sim]
    # combined: text weight 1 → same order as text only (minus the query image)
    both = call(server, "POST", "/api/search", {"text": "雪の中の鳥居", "images": [{"file": first}], "text_weight": 1.0, "k": 4})["results"]
    assert [h["id"] for h in both] == [h["id"] for h in res if h["id"] != first][:4]
    # filters
    only = call(server, "POST", "/api/search", {"text": "x", "kinds": ["jpeg"], "k": 20})["results"]
    assert [h["kind"] for h in only] == ["jpeg"]
    sub = call(server, "POST", "/api/search", {"text": "x", "root": str(library / "set1"), "k": 20})["results"]
    assert len(sub) == 3 and all("/set1/" in h["path"] for h in sub)
    br = call(server, "POST", "/api/browse", {"k": 3})
    assert br["total"] == 7 and len(br["results"]) == 3

    item = call(server, "GET", f"/api/item/{first}")
    assert item["exists"] and item["bytes"] > 0 and item["view"] == f"/api/view/{first}"
    assert len(call(server, "GET", item["view"])) == item["bytes"]  # png: original served
    jpg = next(h for h in call(server, "POST", "/api/browse", {"k": 10})["results"] if h["kind"] == "jpeg")
    part = call(server, "GET", f"/api/view/{jpg['id']}", headers={"Range": "bytes=0-9"}, expect=206)
    assert len(part) == 10 and part[:2] == b"\xff\xd8"
    assert call(server, "POST", f"/api/reveal/{first}")["path"] == item["path"]

    b = call(server, "POST", "/api/boards", {"name": "Snow refs"})
    board = call(server, "POST", f"/api/boards/{b['id']}/items", {"items": [{"file": first}, {"file": sim[0]["id"]}]})
    assert [h["id"] for h in board["results"]] == [first, sim[0]["id"]]
    lst = call(server, "GET", "/api/boards")["boards"]
    assert lst[0]["name"] == "Snow refs" and lst[0]["count"] == 2 and len(lst[0]["covers"]) == 2
    like = call(server, "POST", "/api/search", {"images": [{"file": h["id"]} for h in board["results"]], "k": 10})["results"]
    assert len(like) == 5
    call(server, "PATCH", f"/api/boards/{b['id']}", {"name": "Snow"})
    board = call(server, "DELETE", f"/api/boards/{b['id']}/items", {"items": [{"file": first}]})
    assert board["name"] == "Snow" and len(board["results"]) == 1
    call(server, "DELETE", f"/api/boards/{b['id']}")
    assert call(server, "GET", "/api/boards")["boards"] == []

    rid = call(server, "GET", "/api/folders")["folders"][0]["id"]
    call(server, "DELETE", f"/api/folders/{rid}")
    assert call(server, "GET", "/api/status")["indexed"] == 0


def test_upload(server, library):
    call(server, "POST", "/api/transcribe?lang=ja", raw=b"\0" * 64, expect=404)  # voice search was removed
    call(server, "POST", "/api/folders", {"path": str(library)})
    wait_indexed(server, 7)
    up = call(server, "POST", "/api/upload", raw=(library / "photo.jpg").read_bytes(), headers={"X-Filename": "drop.jpg"})
    assert up["thumb"].startswith("/u/") and call(server, "GET", up["thumb"])[:4] == b"RIFF"
    res = call(server, "POST", "/api/search", {"images": [{"upload": up["upload"]}], "k": 1})["results"]
    assert res[0]["name"] == "photo.jpg"  # the same picture finds itself
    call(server, "POST", "/api/upload", raw=b"not an image", headers={"X-Filename": "x.png"}, expect=415)
    call(server, "POST", "/api/search", {"images": [{"upload": "missing"}]}, expect=404)
