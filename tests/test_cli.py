import json
import os

from mitsuke.cli import main


def run(capsys, *args):
    code = main(["--json", *args])
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    return code, lines


def test_end_to_end(library, capsys):
    code, out = run(capsys, "add", str(library))
    assert code == 0 and out[0]["added"][0]["new"]
    code, out = run(capsys, "index")
    events = [e["event"] for e in out]
    assert code == 0 and events[0] == "sync" and events[-1] == "done"
    assert next(e for e in out if e["event"] == "indexed")["ok"] == 7

    code, out = run(capsys, "search", "雪の中の鳥居", "-k", "3")
    assert code == 0 and out[0]["query"] == "雪の中の鳥居" and len(out[0]["results"]) == 3
    hit = out[0]["results"][0]
    assert os.path.exists(hit["path"]) and os.path.exists(hit["thumb"])

    code, out = run(capsys, "similar", str(library / "photo.jpg"), "-k", "2")
    assert out[0]["in_library"] and all(not r["path"].endswith("photo.jpg") for r in out[0]["results"])
    code, out = run(capsys, "similar", f"#{hit['id']}", "-k", "1")
    assert code == 0 and out[0]["results"][0]["id"] != hit["id"]

    code, out = run(capsys, "status")
    assert out[0]["indexed"] == 7 and out[0]["pending"] == 0 and out[0]["quality"] == "default"
    code, out = run(capsys, "roots")
    assert out[0]["roots"][0]["ok"] == 7

    code, out = main(["search", "x", "--json", "--kind", "png", "-k", "50"]), None
    res = json.loads(capsys.readouterr().out)["results"]
    assert len(res) == 6 and {r["kind"] for r in res} == {"png"}

    code, out = run(capsys, "remove", str(library))
    assert out[0]["removed"][0]["files"] == 7


def test_empty_library_hint(library, capsys):
    """Empty results on an empty library say what to do (like the MCP server's hint)."""
    code, out = run(capsys, "search", "torii")
    assert code == 0 and out[0]["results"] == [] and "mitsuke add" in out[0]["hint"]
    run(capsys, "add", str(library))
    _, out = run(capsys, "search", "torii")
    assert "mitsuke index" in out[0]["hint"]
    run(capsys, "index")
    _, out = run(capsys, "search", "torii", "--kind", "raw")
    assert out[0]["results"] == [] and "hint" not in out[0]


def test_errors_are_json(capsys, tmp_path):
    code, out = run(capsys, "index")
    assert code == 1 and "error" in out[0]
    code, out = run(capsys, "add", str(tmp_path / "nope"))
    assert code == 1 and "not a folder" in out[0]["error"]
    code, out = run(capsys, "similar", str(tmp_path / "missing.png"))
    assert code == 1 and "not found" in out[0]["error"]
    # usage errors too, with --json before or after the command
    import pytest

    for argv in (["--json", "frobnicate"], ["search", "x", "--tex", "1", "--json"], ["search", "-k", "abc", "--json"]):
        with pytest.raises(SystemExit) as e:
            main(argv)
        out = json.loads(capsys.readouterr().out)
        assert e.value.code == 2 and out["error"] and out["usage"].startswith("usage: mitsuke")
    with pytest.raises(SystemExit):
        main(["frobnicate"])
    assert capsys.readouterr().out == ""  # without --json: argparse's usual message on stderr
    code, out = run(capsys, "skill", "show")
    assert code == 0 and out[0]["text"].startswith("---\nname: mitsuke")


def test_remove_checks_every_folder_first(library, capsys, tmp_path):
    run(capsys, "add", str(library))
    code, out = run(capsys, "remove", str(library), str(tmp_path / "nope"))
    assert code == 1 and "not a library folder" in out[0]["error"]
    _, out = run(capsys, "roots")
    assert len(out[0]["roots"]) == 1  # nothing was removed


def test_unexpected_errors_are_json_too(capsys, tmp_path, monkeypatch):
    f = tmp_path / "a file"
    f.write_text("x")
    code, out = run(capsys, "skill", "install", "--dir", str(f))
    assert code == 1 and out[0]["error"]
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "index.sqlite").write_bytes(b"this is not a database" * 100)
    code, out = run(capsys, "status")
    assert code == 1 and "index database cannot be read" in out[0]["error"]


def test_overlapping_roots_rejected(library, capsys):
    run(capsys, "add", str(library))
    code, out = run(capsys, "add", str(library / "set0"))
    assert code == 1 and "overlaps" in out[0]["error"]
    # several folders: a missing one is caught before anything is added; an overlap says what was added already
    other = library.parent / "other"
    other.mkdir()
    code, out = run(capsys, "add", str(other), str(library / "nope"))
    assert code == 1 and "not a folder" in out[0]["error"]
    assert [r["path"] for r in run(capsys, "roots")[1][0]["roots"]] == [str(library)]
    code, out = run(capsys, "add", str(other), str(library / "set1"))
    assert code == 1 and "overlaps" in out[0]["error"] and f"added before this error: {other}" in out[0]["error"]


def test_index_limit_then_continue(library, capsys):
    run(capsys, "add", str(library))
    code, out = run(capsys, "index", "--limit", "0")
    assert code == 1 and "--limit must be at least 1" in out[0]["error"]
    code, out = run(capsys, "index", "--limit", "3")
    assert next(e for e in out if e["event"] == "done")["pending"] == 4
    code, out = run(capsys, "index")
    assert next(e for e in out if e["event"] == "indexed")["ok"] == 4
    (library / "broken.png").write_text("not an image " * 50)
    run(capsys, "index")
    code, out = run(capsys, "index", "--retry-errors")
    assert out[0] == {"event": "retry", "files": 1} and next(e for e in out if e["event"] == "indexed")["error"] == 1


def test_human_output(library, capsys):
    main(["add", str(library)])
    main(["index"])
    main(["search", "sunset"])
    text = capsys.readouterr().out
    assert "indexed 7 images" in text and "1. " in text


def test_doctor_json(capsys):
    code, out = run(capsys, "doctor", "--no-embed")
    names = {c["name"] for c in out[0]["checks"]}
    assert {"platform", "python", "decode", "index", "model"} <= names
    assert out[0]["status"] in ("ok", "warn", "fail")
    decode = next(c for c in out[0]["checks"] if c["name"] == "decode")
    assert "jpg" in decode["detail"] and "png" in decode["detail"]


def test_unicode_normalisation(tmp_path, capsys):
    """macOS names can be NFC or NFD (ガ vs カ + ゛); a path typed in the other form is the same file / folder."""
    import unicodedata

    from conftest import make_image

    nfc, nfd = (unicodedata.normalize(f, str(tmp_path / "ガイド")) for f in ("NFC", "NFD"))
    make_image(nfc + "/a.png", seed=1)
    make_image(unicodedata.normalize("NFD", nfc + "/パゴダ.png"), seed=2)  # a decomposed name on disk
    _, out = run(capsys, "add", nfc)
    code, again = run(capsys, "add", nfd)
    assert code == 0 and again[0]["added"][0]["new"] is False  # not a second root indexing everything twice
    run(capsys, "index")
    for form in ("NFC", "NFD"):
        code, info = run(capsys, "show", unicodedata.normalize(form, nfc + "/パゴダ.png"))
        assert code == 0 and info[0]["path"].endswith(".png") and info[0]["status"] == "ok"
    code, res = run(capsys, "search", "x", "--root", nfd, "-k", "5")
    assert code == 0 and len(res[0]["results"]) == 2
    code, out = run(capsys, "remove", nfd)
    assert code == 0 and out[0]["removed"][0]["files"] == 2


def test_doctor_embed_with_fake(capsys):
    code, out = run(capsys, "doctor")
    embed = next(c for c in out[0]["checks"] if c["name"] == "embed")
    assert embed["status"] == "ok"
