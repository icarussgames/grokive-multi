"""User (hand-assigned) media tags: tags.json + the /api/tags routes + the index mirror.

Pins: bulk add/remove with case-insensitive reuse, rename + merge, delete, color,
tags surviving a full index rebuild, purge on hard delete, the tag_source/tag_mode
filters and facets, and that locked-collection media never leak through tags/counts.

Run: python -m pytest test_user_tags.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp())
os.environ.setdefault("GROK_DATA_DIR", str(_tmp))  # first importer of `server` wins

import db  # noqa: E402
import server  # noqa: E402


def _reset() -> None:
    server.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for path in (server.COLLECTIONS_FILE, server.COLLECTION_GROUPS_FILE, server.METADATA_FILE,
                 server.LIBRARY_FILE, server.DB_FILE, server.USER_TAGS_FILE, server.DELETED_FILE):
        path.unlink(missing_ok=True)
    server._locked_cache["mtime"] = None
    server._locked_cache["collections"] = {}
    server._locked_cache["groups"] = {}
    server._locked_cache["group_collections"] = {}
    server.METADATA_FILE.write_text(json.dumps([
        {"id": "a", "media_type": "video", "prompt": "a red fox runs through snow",
         "created_at": "2026-07-01", "local_path": "media/videos/aa/a.mp4"},
        {"id": "b", "media_type": "image", "prompt": "a blue whale sings",
         "created_at": "2026-07-02", "local_path": "media/images/bb/b.jpg"},
        {"id": "c", "media_type": "video", "prompt": "city lights at night, slow push-in",
         "created_at": "2026-07-03", "local_path": "media/videos/cc/c.mp4"},
        {"id": "v", "media_type": "video", "prompt": "a private vault clip",
         "created_at": "2026-07-04", "local_path": "media/videos/vv/v.mp4"},
    ]), encoding="utf-8")
    server.rebuild_db(wait=True)


def _lock_v() -> None:
    server.COLLECTIONS_FILE.write_text(json.dumps([
        {"id": "vault", "name": "Vault", "ids": ["v"], "created_at": "2026-07-01", "updated_at": "2026-07-01",
         "locked": True, "pass_hash": server.generate_password_hash("pw"), "locked_at": "2026-07-01 10:00:00"},
    ]), encoding="utf-8")
    server._locked_cache["mtime"] = None


def _client():
    c = server.app.test_client()
    with c.session_transaction() as sess:
        sess["authed"] = True
    return c


def _media(c, **params) -> list[str]:
    r = c.get("/api/media", query_string={"view": "all", **params})
    assert r.status_code == 200, r.data
    return sorted(it["id"] for it in r.get_json()["items"])


def _tag(c, ids, add=(), remove=()):
    r = c.post("/api/media/tags", json={"ids": list(ids), "add": list(add), "remove": list(remove)})
    assert r.status_code == 200, r.data
    return r.get_json()


def _tags(c) -> dict:
    return {t["name"]: t for t in c.get("/api/tags").get_json()["tags"]}


def test_bulk_add_remove_and_case_insensitive_reuse():
    _reset()
    c = _client()
    out = _tag(c, ["a", "b"], add=["Nature", "  animals  "])
    assert out["changed"] == 2
    assert out["items"] == {"a": ["Nature", "animals"], "b": ["Nature", "animals"]}
    out = _tag(c, ["c"], add=["nature"])  # reuses the registry spelling
    assert out["items"]["c"] == ["Nature"]
    assert {n: t["count"] for n, t in _tags(c).items()} == {"animals": 2, "Nature": 3}
    out = _tag(c, ["a", "c"], remove=["NATURE"])
    assert out["items"] == {"a": ["animals"], "c": []}
    assert _tags(c)["Nature"]["count"] == 1
    # the lightbox / list payloads carry user tags apart from prompt tags
    item = next(it for it in c.get("/api/media", query_string={"view": "all"}).get_json()["items"] if it["id"] == "a")
    assert item["user_tags"] == ["animals"] and "animals" not in item["tags"]
    assert c.post("/api/media/tags", json={"ids": ["a"]}).status_code == 400
    assert c.post("/api/media/tags", json={"ids": [], "add": ["x"]}).status_code == 400


def test_filter_any_all_and_source():
    _reset()
    c = _client()
    _tag(c, ["a", "b"], add=["animals"])
    _tag(c, ["a", "c"], add=["favs"])
    assert _media(c, tags=["animals", "favs"]) == ["a", "b", "c"]  # any (default)
    assert _media(c, tags=["animals", "favs"], tag_mode="all") == ["a"]
    assert _media(c, tags=["animals"], tag_source="user") == ["a", "b"]
    assert _media(c, tags=["animals"], tag_source="auto") == []
    # user tags are full-text searchable too
    assert _media(c, q="favs") == ["a", "c"]
    f = c.get("/api/facets", query_string={"view": "all"}).get_json()
    assert {t["name"]: t["count"] for t in f["user_tags"]} == {"animals": 2, "favs": 2}
    assert all(t["name"] not in ("animals", "favs") for t in f["tags"])  # auto facet unchanged


def test_rename_merge_delete_color():
    _reset()
    c = _client()
    _tag(c, ["a"], add=["dogs"])
    _tag(c, ["a", "b"], add=["pets"])
    r = c.post("/api/tags/color", json={"name": "dogs", "color": "#FF0000"}).get_json()
    assert r["color"] == "#ff0000"
    assert c.post("/api/tags/color", json={"name": "dogs", "color": "red"}).status_code == 400
    r = c.post("/api/tags/rename", json={"from": "dogs", "to": "Hounds"}).get_json()
    assert r["name"] == "Hounds" and not r["merged"]
    assert _media(c, tags=["Hounds"]) == ["a"] and _media(c, tags=["dogs"]) == []
    r = c.post("/api/tags/rename", json={"from": "Hounds", "to": "PETS"}).get_json()
    assert r["merged"] and r["name"] == "pets"
    tags = _tags(c)
    assert set(tags) == {"pets"} and tags["pets"]["count"] == 2 and tags["pets"]["color"] == "#ff0000"
    stored = json.loads(server.USER_TAGS_FILE.read_text(encoding="utf-8"))
    assert stored["items"]["a"] == ["pets"]  # merged without duplicates
    assert c.post("/api/tags/rename", json={"from": "nope", "to": "x"}).status_code == 404
    r = c.post("/api/tags/delete", json={"name": "Pets"}).get_json()
    assert r["ok"] and r["removed"] == 2 and _tags(c) == {}
    assert _media(c, tags=["pets"]) == []


def test_user_tags_survive_full_rebuild_and_old_schema():
    _reset()
    c = _client()
    _tag(c, ["b"], add=["ocean"])
    server.rebuild_db(wait=True)
    assert _media(c, tags=["ocean"]) == ["b"]
    # a pre-`source` index (older install) migrates on the next build
    server.DB_FILE.unlink()
    import sqlite3
    conn = sqlite3.connect(server.DB_FILE)
    conn.executescript("CREATE TABLE media_tags (media_id TEXT, tag TEXT, PRIMARY KEY (media_id, tag));")
    conn.commit()
    conn.close()
    server.rebuild_db(wait=True)
    assert _media(c, tags=["ocean"], tag_source="user") == ["b"]


def test_hard_delete_purges_assignments():
    _reset()
    c = _client()
    _tag(c, ["a", "b"], add=["gone"])
    r = c.post("/api/media/delete", json={"ids": ["a"]})
    assert r.status_code == 200, r.data
    stored = json.loads(server.USER_TAGS_FILE.read_text(encoding="utf-8"))
    assert "a" not in stored["items"] and stored["items"]["b"] == ["gone"]
    assert _tags(c)["gone"]["count"] == 1


def test_locked_media_never_leak():
    _reset()
    c = _client()
    _tag(c, ["v"], add=["secret-only"])
    _tag(c, ["v", "a"], add=["shared"])
    _lock_v()
    tags = _tags(c)
    assert "secret-only" not in tags  # a hidden-only tag's name is withheld
    assert tags["shared"]["count"] == 1
    f = c.get("/api/facets", query_string={"view": "all"}).get_json()
    assert {t["name"]: t["count"] for t in f["user_tags"]} == {"shared": 1}
    assert _media(c, tags=["shared"]) == ["a"]
    assert _media(c, q="secret") == []
    # edits skip hidden ids; rename/delete/color of a hidden-only tag is "not found"
    out = _tag(c, ["v", "b"], add=["new"])
    assert "v" not in out["items"] and out["changed"] == 1
    assert c.post("/api/tags/delete", json={"name": "secret-only"}).status_code == 404
    assert c.post("/api/tags/rename", json={"from": "secret-only", "to": "x"}).status_code == 404
    assert c.post("/api/tags/color", json={"name": "secret-only", "color": "#000000"}).status_code == 404
    r = c.post("/api/tags/rename", json={"from": "shared", "to": "both"}).get_json()
    assert r["renamed"] == 1  # the hidden item's rename isn't counted back


def test_corrupt_tags_file_is_never_overwritten():
    _reset()
    c = _client()
    server.USER_TAGS_FILE.write_text("{not json", encoding="utf-8")
    r = c.post("/api/media/tags", json={"ids": ["a"], "add": ["x"]})
    assert r.status_code == 503
    assert server.USER_TAGS_FILE.read_text(encoding="utf-8") == "{not json"
    assert c.get("/api/tags").get_json()["tags"] == []  # reads degrade to empty


def test_backup_includes_tags_file():
    assert server._BACKUP_TARGETS["tags.json"] == server.USER_TAGS_FILE
    assert "tags.json" in server._BACKUP_JSON_NAMES


def test_set_user_tags_skips_unindexed_ids():
    _reset()
    assert db.set_user_tags(server.DB_FILE, {"a": ["x"], "missing": ["y"]}) == 1
