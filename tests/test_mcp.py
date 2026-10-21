"""`mitsuke mcp` end to end: a real subprocess speaking JSON-RPC on stdin/stdout (fake embedder)."""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SRC = str(Path(__file__).resolve().parent.parent / "src")


class Client:
    def __init__(self, env):
        self.p = subprocess.Popen([sys.executable, "-m", "mitsuke", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8")
        self.n = 0

    def send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def request(self, method, params=None):
        self.n += 1
        self.send({"jsonrpc": "2.0", "id": self.n, "method": method, **({"params": params} if params is not None else {})})
        line = self.p.stdout.readline()
        assert line, self.p.stderr.read()
        msg = json.loads(line)
        assert msg["id"] == self.n
        return msg

    def tool(self, tool_name, ok=True, **args):
        r = self.request("tools/call", {"name": tool_name, "arguments": args})["result"]
        assert bool(r.get("isError")) is not ok, r
        if not ok:
            return r["content"][0]["text"]
        if "structuredContent" in r:
            assert json.loads(r["content"][0]["text"]) == r["structuredContent"]
            return r["structuredContent"]
        return r

    def close(self):
        self.p.stdin.close()
        assert self.p.wait(10) == 0


@pytest.fixture
def mcp(isolated_home, monkeypatch):
    env = {**os.environ, "PYTHONPATH": SRC, "MITSUKE_NO_DESKTOP": "1"}
    c = Client(env)
    yield c
    if c.p.poll() is None:
        c.close()


def test_protocol(mcp):
    init = mcp.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "0"}})["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "mitsuke"
    assert "tools" in init["capabilities"] and init["instructions"]
    mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})  # no reply expected
    assert mcp.request("ping")["result"] == {}
    tools = {t["name"]: t for t in mcp.request("tools/list")["result"]["tools"]}
    assert {"search", "similar", "view_images", "board_add", "status", "add_folder", "index",
            "web_link"} <= set(tools)
    assert all(t["inputSchema"]["type"] == "object" for t in tools.values())
    assert mcp.request("nope")["error"]["code"] == -32601
    assert "unknown tool" in mcp.tool("nope", ok=False)
    newer = mcp.request("initialize", {"protocolVersion": "2099-01-01"})["result"]["protocolVersion"]
    assert newer == "2025-11-25"


def test_tools_end_to_end(mcp, library, tmp_path):
    mcp.request("initialize", {"protocolVersion": "2025-06-18"})
    st = mcp.tool("status")
    assert st["indexed"] == 0 and st["folders"] == []
    empty = mcp.tool("search", query="torii")
    assert empty["results"] == [] and "empty" in empty["hint"]
    assert "no folders" in mcp.tool("index", ok=False)

    assert mcp.tool("add_folder", path=str(library))["new"]
    assert "not a folder" in mcp.tool("add_folder", ok=False, path=str(tmp_path / "nope"))
    ix = mcp.tool("index", wait_seconds=60)
    assert ix["done"] and ix["indexed"] == 7 and ix["pending"] == 0 and ix["indexing"]["state"] == "done"

    res = mcp.tool("search", query="雪の中の鳥居", k=3)
    assert res["count"] == 3 and os.path.exists(res["results"][0]["thumb"]) and res["web"] is None
    assert res["pending"] == 0 and "note" not in res
    first = res["results"][0]["id"]
    both = mcp.tool("search", query="雪の中の鳥居", like=[first], text_weight=1.0, k=2)
    assert [h["id"] for h in both["results"]] == [h["id"] for h in res["results"] if h["id"] != first][:2]
    assert "unknown kind" in mcp.tool("search", ok=False, query="x", kinds=["12"])
    only = mcp.tool("search", query="x", kinds=["jpeg"], k=10)
    assert [h["kind"] for h in only["results"]] == ["jpeg"]
    sim = mcp.tool("similar", image=str(library / "photo.jpg"), k=10)
    assert sim["count"] == 6 and all(not h["path"].endswith("photo.jpg") for h in sim["results"])

    sheet = mcp.request("tools/call", {"name": "view_images", "arguments": {"images": [first, str(library / "set0/img0.png")]}})["result"]
    img = sheet["content"][0]
    assert img["type"] == "image" and img["mimeType"] == "image/jpeg" and base64.b64decode(img["data"])[:2] == b"\xff\xd8"

    b = mcp.tool("board_create", name="Snow", images=[first])
    assert b["name"] == "Snow" and [h["id"] for h in b["results"]] == [first]
    b = mcp.tool("board_add", board=b["id"], images=[h["id"] for h in sim["results"][:2]])
    assert len(b["results"]) == 3
    like = mcp.tool("search", board=b["id"], k=10)
    assert like["count"] == 4  # 7 files minus the 3 on the board
    assert mcp.tool("list_boards")["boards"][0]["count"] == 3
    assert len(mcp.tool("board_remove", board=b["id"], images=[first])["results"]) == 2
    assert mcp.tool("board_rename", board=b["id"], name="Snow 2")["name"] == "Snow 2"
    info = mcp.tool("file_info", image=sim["results"][0]["id"])
    assert info["exists"] and info["boards"] == [b["id"]]
    assert mcp.tool("open_file", image=info["id"], reveal=True)["path"] == info["path"]
    assert "no board" in mcp.tool("board_show", ok=False, board=999)
    assert "not in the library" in mcp.tool("board_add", ok=False, board=b["id"], images=[str(library / "notes.txt")])
    assert mcp.tool("board_delete", board=b["id"])["deleted"] == b["id"]

    link = mcp.tool("web_link", query="torii")
    assert link["running"] is False and "start" in link["hint"]
    assert mcp.tool("remove_folder", folder=str(library))["files"] == 7
    mcp.close()


def test_index_lock_shared(mcp, library):
    """While someone else holds index.lock (e.g. `mitsuke serve`), `index` steps aside and searches still work."""
    import fcntl

    from mitsuke import paths

    mcp.request("initialize", {})
    mcp.tool("add_folder", path=str(library))
    paths.home_dir().mkdir(parents=True, exist_ok=True)
    with open(paths.home_dir() / "index.lock", "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ix = mcp.tool("index", wait_seconds=10)
        assert ix["indexing"]["state"] == "external" and ix["indexed"] == 0
        fcntl.flock(f, fcntl.LOCK_UN)
    ix = mcp.tool("index", wait_seconds=60)
    assert ix["indexed"] == 7


def test_stdout_stays_clean(mcp):
    """Stray prints inside tools go to stderr, never into the protocol stream."""
    mcp.request("initialize", {})
    r = mcp.request("tools/call", {"name": "status", "arguments": {}})
    assert r["result"]["structuredContent"]["version"]
    mcp.p.stdin.write("not json\n")
    mcp.p.stdin.flush()
    assert json.loads(mcp.p.stdout.readline())["error"]["code"] == -32700


def test_no_voice_tool(mcp):
    """Voice search was removed (users have their own dictation); the server must not offer it."""
    mcp.request("initialize", {})
    assert "voice_search" not in {t["name"] for t in mcp.request("tools/list")["result"]["tools"]}
