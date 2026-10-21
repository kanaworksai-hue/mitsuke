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


def test_errors_are_json(capsys, tmp_path):
    code, out = run(capsys, "index")
    assert code == 1 and "error" in out[0]
    code, out = run(capsys, "add", str(tmp_path / "nope"))
    assert code == 1 and "not a folder" in out[0]["error"]
    code, out = run(capsys, "similar", str(tmp_path / "missing.png"))
    assert code == 1 and "not found" in out[0]["error"]


def test_overlapping_roots_rejected(library, capsys):
    run(capsys, "add", str(library))
    code, out = run(capsys, "add", str(library / "set0"))
    assert code == 1 and "overlaps" in out[0]["error"]


def test_index_limit_then_continue(library, capsys):
    run(capsys, "add", str(library))
    code, out = run(capsys, "index", "--limit", "3")
    assert next(e for e in out if e["event"] == "done")["pending"] == 4
    code, out = run(capsys, "index")
    assert next(e for e in out if e["event"] == "indexed")["ok"] == 4


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


def test_doctor_embed_with_fake(capsys):
    code, out = run(capsys, "doctor")
    embed = next(c for c in out[0]["checks"] if c["name"] == "embed")
    assert embed["status"] == "ok"
