"""Agent-facing CLI commands: moodboards, image + text queries, show, web links."""

import json

import numpy as np
import pytest

from mitsuke.cli import main


def run(capsys, *args):
    code = main(["--json", *args])
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    return code, lines[-1] if lines else None


@pytest.fixture
def indexed(library, capsys):
    main(["add", str(library)])
    main(["index"])
    capsys.readouterr()
    return library


def test_boards(indexed, capsys):
    code, b = run(capsys, "board", "create", "Snow", "refs", "--add", str(indexed / "photo.jpg"))
    assert code == 0 and b["name"] == "Snow refs" and len(b["results"]) == 1 and b["web"] is None
    bid, first = b["id"], b["results"][0]["id"]
    _, res = run(capsys, "search", "torii", "-k", "3")
    ids = [h["id"] for h in res["results"] if h["id"] != first][:2]
    code, b = run(capsys, "board", "add", str(bid), *[f"#{i}" for i in ids])
    assert [h["id"] for h in b["results"]] == [first, *ids]
    _, lst = run(capsys, "board", "list")
    assert lst["boards"][0]["count"] == 3 and len(lst["boards"][0]["covers"]) == 3
    code, b = run(capsys, "board", "remove", f"#{bid}", str(first))
    assert code == 0 and len(b["results"]) == 2
    _, r = run(capsys, "board", "rename", str(bid), "Snow")
    assert r["name"] == "Snow"
    code, err = run(capsys, "board", "add", str(bid), str(indexed / "notes.txt"))
    assert code == 1 and "not in the library" in err["error"]
    code, err = run(capsys, "board", "show", "999")
    assert code == 1 and "no board" in err["error"]
    _, r = run(capsys, "board", "delete", str(bid))
    assert r["deleted"] == bid
    _, lst = run(capsys, "board", "list")
    assert lst["boards"] == []


def test_combined_search(indexed, capsys):
    _, text = run(capsys, "search", "雪の中の鳥居", "-k", "7")
    first = text["results"][0]["id"]
    # only the image: the image itself is left out
    code, img = run(capsys, "search", "--like", f"#{first}", "-k", "10")
    assert code == 0 and len(img["results"]) == 6 and first not in [h["id"] for h in img["results"]]
    # text weight 1 = the text order (minus the query image)
    _, both = run(capsys, "search", "雪の中の鳥居", "--like", str(first), "--text-weight", "1", "-k", "3")
    assert [h["id"] for h in both["results"]] == [h["id"] for h in text["results"] if h["id"] != first][:3]
    assert both["text_weight"] == 1.0 and both["images"] == [str(first)]
    # an image outside the library is embedded on the fly; the same picture finds itself
    _, out = run(capsys, "search", "--like", str(indexed / ".hidden" / "skip.png"), "-k", "1")
    assert len(out["results"]) == 1 and out["images"] == [str(indexed / ".hidden" / "skip.png")]
    _, out = run(capsys, "search", "--like", str(indexed / "photo.jpg"), "-k", "1")
    assert not out["results"][0]["path"].endswith("photo.jpg")  # library file → excluded from its own results
    # a moodboard as the query
    _, b = run(capsys, "board", "create", "B", "--add", f"#{first}")
    _, out = run(capsys, "search", "--board", str(b["id"]), "-k", "3")
    assert len(out["results"]) == 3 and first not in [h["id"] for h in out["results"]] and out["board"] == b["id"]
    code, err = run(capsys, "search")
    assert code == 1 and "description" in err["error"]
    code, err = run(capsys, "search", "x", "--like", "#999")
    assert code == 1 and "not in the index" in err["error"]


def test_show(indexed, capsys):
    code, info = run(capsys, "show", str(indexed / "photo.jpg"))
    assert code == 0 and info["kind"] == "jpeg" and info["exists"] and info["boards"] == [] and info["web"] is None
    code, again = run(capsys, "show", f"#{info['id']}")
    assert again["path"] == info["path"]
    code, err = run(capsys, "show", "#999")
    assert code == 1


def test_web_links(indexed, capsys, monkeypatch):
    from mitsuke.server import Server

    code, out = run(capsys, "web", "torii")
    assert code == 0 and out["running"] is False and out["url"] is None
    monkeypatch.setenv("MITSUKE_NO_DESKTOP", "1")
    srv = Server(port=0, index=False, warm=False).start()
    try:
        _, out = run(capsys, "web", "雪 の 鳥居", "--like", "#3", "--text-weight", "0.7", "--kind", "photo")
        assert out["url"] == srv.url + "#q=%E9%9B%AA%20%E3%81%AE%20%E9%B3%A5%E5%B1%85&img=f3&w=0.7&kind=photo"
        _, out = run(capsys, "web", "--board", "2")
        assert out["url"] == srv.url + "#board=2"
        _, res = run(capsys, "search", "torii", "-k", "1")
        assert res["web"] == srv.url + "#q=torii"
    finally:
        srv.stop()
    _, out = run(capsys, "web")
    assert out["running"] is False  # serve.json removed on stop


def test_web_start(indexed, capsys, monkeypatch):
    """`mitsuke web --start` launches the page in the background and returns a working link."""
    import os
    import signal
    import time
    import urllib.request
    from pathlib import Path

    from mitsuke import library

    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parent.parent / "src"))
    code, out = run(capsys, "web", "--start", "--port", "0", "torii")
    pid = json.loads(library.serve_file().read_text())["pid"]
    try:
        assert code == 0 and out["running"] and out["url"].endswith("/#q=torii")
        with urllib.request.urlopen(out["url"].split("#")[0] + "api/status", timeout=5) as r:
            assert json.loads(r.read())["indexed"] == 7
        _, again = run(capsys, "web", "--start")  # already running → same server
        assert again["url"].split("#")[0] == out["url"].split("#")[0]
    finally:
        os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not library.serve_file().exists():
            break
        time.sleep(0.1)
    assert not library.serve_file().exists()


def test_skill_install(tmp_path, capsys):
    code, out = run(capsys, "skill", "install", "--dir", str(tmp_path / "skills"))
    md = tmp_path / "skills" / "mitsuke" / "SKILL.md"
    assert code == 0 and out["installed"] == str(md.parent) and md.read_text().startswith("---\nname: mitsuke\n")
    _, out = run(capsys, "skill", "path", "--dir", str(tmp_path / "skills"))
    assert out["installed"] == str(md.parent)


def test_agent_friendly_flags(indexed, capsys):
    """Seen in the claude -p run: `--k 12` used to be read as an abbreviation of `--kind` → silently empty."""
    code, out = run(capsys, "search", "torii", "--k", "4")
    assert code == 0 and len(out["results"]) == 4
    code, out = run(capsys, "search", "torii", "--top", "2")
    assert len(out["results"]) == 2
    code, err = run(capsys, "search", "torii", "--kind", "12")
    assert code == 1 and "unknown kind 12" in err["error"]
    code, out = run(capsys, "search", "torii", "--kind", "photo,png", "-k", "50")
    assert code == 0 and len(out["results"]) == 7


def test_search_reports_pending(library, capsys):
    main(["add", str(library)])
    main(["index", "--limit", "3"])
    capsys.readouterr()
    _, res = run(capsys, "search", "torii", "-k", "10")
    assert res["pending"] == 4 and "4 files are not indexed yet" in res["note"] and len(res["results"]) == 3
    assert main(["search", "torii"]) == 0 and "note: 4 files" in capsys.readouterr().out
    main(["index"])
    capsys.readouterr()
    _, res = run(capsys, "search", "torii")
    assert res["pending"] == 0 and "note" not in res
    _, res = run(capsys, "similar", str(library / "photo.jpg"))
    assert res["pending"] == 0
