"""Grok Imagine collections -> grok:<name> auxiliary tags (grokcollections.py + index).

All HTTP is mocked. Run: python -m pytest test_grok_collections.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

import db
import gdownloader as g
import grokcollections as gc

AUTH = g.RequestSpec(method="POST", url="https://grok.com/x", headers={}, cookies={"sso": "s"}, body=None)
ASSET = "https://assets.grok.com/users/u0000000-0000-0000-0000-000000000000/generated/{}/image.jpg"


def uid(n: int) -> str:
    return f"{n:08d}-0000-4000-8000-000000000000"


def post(pid, children=(), video_ids=()):
    return {
        "id": pid, "mediaType": "MEDIA_POST_TYPE_IMAGE", "mediaUrl": ASSET.format(pid), "prompt": "p",
        "childPosts": [{"id": c, "mediaType": "MEDIA_POST_TYPE_VIDEO",
                        "mediaUrl": ASSET.format(c).replace("image.jpg", "generated_video.mp4")} for c in children],
        "videos": [{"id": v} for v in video_ids],
    }


class FakeGrok:
    def __init__(self, collections, posts_by_collection, page_size=2, ignore_filter=False, fail=None):
        self.collections = collections
        self.posts = posts_by_collection
        self.page_size = page_size
        self.ignore_filter = ignore_filter
        self.fail = fail
        self.calls = []

    def request(self, _client, spec):
        body = json.loads(spec.body or "{}")
        self.calls.append((spec.url, body))
        if self.fail:
            raise self.fail
        if spec.url == gc.GROK_COLLECTION_LIST_ENDPOINT:
            return {"collections": self.collections}
        assert spec.url.startswith(g.GROK_FAVORITES_ENDPOINT)
        cid = body["filter"]["collectionId"]
        if self.ignore_filter:
            cid = next(iter(self.posts))
        items = self.posts.get(cid, [])
        start = int(body.get("cursor") or 0)
        page = items[start:start + self.page_size]
        nxt = str(start + self.page_size) if start + self.page_size < len(items) else None
        return {"posts": page, "nextCursor": nxt}


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(g.time, "sleep", lambda _s: None)
    monkeypatch.setattr(gc.time, "sleep", lambda _s: None)

    def install(f):
        monkeypatch.setattr(g, "request_json_with_backoff", f.request)
        return f
    return install


COLLS = [
    {"id": "c-kira", "name": "amh_kira_v4", "isDefault": False, "updateTime": "2026-10-01T00:00:00Z"},
    {"id": "c-moon", "name": "moon tactics", "isDefault": False, "updateTime": "2026-10-02T00:00:00Z"},
    {"id": "c-liked", "name": "Liked", "isDefault": True, "updateTime": "2026-10-03T00:00:00Z"},
]


def test_lists_paginates_and_maps_posts_children_and_assets(tmp_path, fake, capsys):
    p1, p2, p3, kid, vid, gone = uid(1), uid(2), uid(3), uid(10), uid(11), uid(99)
    f = fake(FakeGrok(COLLS, {
        "c-kira": [post(p1, children=[kid]), post(p2), post(gone)],   # 2 pages
        "c-moon": [post(p3, video_ids=[vid])],
        "c-liked": [post(p1)],
    }))
    library = {p1, kid, p2, vid}  # p3 itself isn't held, only its video; `gone` not at all
    state = tmp_path / "grok_collections.json"
    assert gc.sync_collections(None, AUTH, library, "default", state) == 0
    acct = json.loads(state.read_text())["accounts"]["default"]
    assert set(acct["collections"]) == {"c-kira", "c-moon"}  # isDefault ("Liked") skipped
    assert sorted(acct["members"]["c-kira"]) == sorted([p1, kid, p2])
    assert acct["members"]["c-moon"] == [vid]
    assert acct["listed"] == {"c-kira": 3, "c-moon": 1}
    assert acct["collections"]["c-kira"]["name"] == "amh_kira_v4" and acct["synced_at"]
    pages = [b for u, b in f.calls if b.get("filter", {}).get("collectionId") == "c-kira"]
    assert len(pages) == 2 and pages[1]["cursor"] == "2"
    assert not any(b.get("filter", {}).get("collectionId") == "c-liked" for _u, b in f.calls)
    out = capsys.readouterr().out
    assert "1 listed post(s) aren't in the library yet" in out and "Liked" in out


def test_full_refresh_drops_removed_members_and_keeps_other_accounts(tmp_path, fake):
    p1, p2 = uid(1), uid(2)
    state = tmp_path / "grok_collections.json"
    fake(FakeGrok(COLLS[:1], {"c-kira": [post(p1), post(p2)]}))
    gc.sync_collections(None, AUTH, {p1, p2}, "default", state)
    fake(FakeGrok([{"id": "c-x", "name": "other", "isDefault": False}], {"c-x": [post(p1)]}))
    gc.sync_collections(None, AUTH, {p1, p2}, "2172d829", state)
    fake(FakeGrok(COLLS[:1], {"c-kira": [post(p2)]}))  # p1 taken out on Grok
    gc.sync_collections(None, AUTH, {p1, p2}, "default", state)
    accts = json.loads(state.read_text())["accounts"]
    assert accts["default"]["members"] == {"c-kira": [p2]}
    assert accts["2172d829"]["members"] == {"c-x": [p1]}  # untouched


def test_ignored_filter_keeps_previous_tags(tmp_path, fake, capsys):
    p1, p2 = uid(1), uid(2)
    state = tmp_path / "grok_collections.json"
    fake(FakeGrok(COLLS[:2], {"c-kira": [post(p1)], "c-moon": [post(p2)]}))
    gc.sync_collections(None, AUTH, {p1, p2}, "default", state)
    before = state.read_text()
    fake(FakeGrok(COLLS[:2], {"c-kira": [post(p1)], "c-moon": [post(p2)]}, ignore_filter=True))
    assert gc.sync_collections(None, AUTH, {p1, p2}, "default", state) == 1
    assert state.read_text() == before
    assert "ignoring the collectionId filter" in capsys.readouterr().out


def test_main_reports_http_errors_without_cookies(tmp_path, fake, monkeypatch, capsys):
    import httpx
    (tmp_path / "grok_auth.txt").write_text("curl 'https://grok.com/rest/media/post/list' -H 'Cookie: sso=SECRETCOOKIE'")
    (tmp_path / "metadata.json").write_text("[]")
    req = httpx.Request("POST", gc.GROK_COLLECTION_LIST_ENDPOINT)
    fake(FakeGrok([], {}, fail=httpx.HTTPStatusError("401", request=req, response=httpx.Response(401, request=req))))
    monkeypatch.chdir(tmp_path)
    assert gc.main(["--curl", "grok_auth.txt"]) == 1
    out = capsys.readouterr().out
    assert "HTTP 401" in out and "SECRETCOOKIE" not in out
    assert not (tmp_path / "grok_collections.json").exists()


def _index(tmp_path: Path, collections: dict) -> Path:
    meta = tmp_path / "metadata.json"
    meta.write_text(json.dumps([
        {"id": "a", "media_type": "image", "prompt": "red fox", "created_at": "2026-09-01T10:00:00Z",
         "local_path": "media/images/aa/a.jpg", "accounts": ["default"]},
        {"id": "b", "media_type": "image", "prompt": "blue fox", "created_at": "2026-09-02T10:00:00Z",
         "local_path": "media/images/aa/b.jpg", "accounts": ["2172d829"]},
        {"id": "c", "media_type": "video", "prompt": "green owl", "created_at": "2026-09-03T10:00:00Z",
         "local_path": "media/videos/aa/c.mp4", "accounts": ["default"]},
    ]))
    (tmp_path / "grok_collections.json").write_text(json.dumps({"version": 1, "accounts": collections}))
    (tmp_path / "tags.json").write_text(json.dumps({"items": {"a": ["mine"]}}))
    dbfile = tmp_path / "index.db"
    db.build_index(dbfile, meta, tmp_path / "gallery")
    return dbfile


COLL_STATE = {
    "default": {"collections": {"c1": {"name": "amh_kira_v4"}}, "members": {"c1": ["a", "c", "not-held"]}},
    "2172d829": {"collections": {"c2": {"name": "moon tactics"}, "c3": {"name": "amh_kira_v4"}},
                 "members": {"c2": ["b"], "c3": ["b"]}},
}


def test_index_exposes_grok_tags_as_their_own_source(tmp_path):
    dbfile = _index(tmp_path, COLL_STATE)
    ids = lambda **kw: sorted(i["id"] for i in db.query_media(dbfile, view="all", **kw)["items"])
    assert ids(tags=["grok:amh_kira_v4"]) == ["a", "b", "c"]
    assert ids(tags=["grok:amh_kira_v4"], tag_source="grok") == ["a", "b", "c"]
    assert ids(tags=["grok:amh_kira_v4"], tag_source="user") == []
    assert ids(tags=["grok:amh_kira_v4"], account="default") == ["a", "c"]
    assert ids(tags=["grok:moon tactics", "grok:amh_kira_v4"], tag_mode="all") == ["b"]
    assert ids(q="kira") == ["a", "b", "c"]  # searchable too
    item = next(i for i in db.query_media(dbfile, view="all")["items"] if i["id"] == "a")
    assert item["grok_tags"] == ["grok:amh_kira_v4"] and item["user_tags"] == ["mine"]
    assert not any(t.startswith("grok:") for t in item["tags"])
    f = db.facets(dbfile, view="all")
    assert {t["name"]: t["count"] for t in f["grok_tags"]} == {"grok:amh_kira_v4": 3, "grok:moon tactics": 1}
    assert not any(t["name"].startswith("grok:") for t in f["tags"] + f["user_tags"])
    # Editing user tags keeps the grok tags searchable.
    db.set_user_tags(dbfile, {"a": ["other"]})
    assert ids(q="kira") == ["a", "b", "c"]


def test_index_without_collections_file(tmp_path):
    dbfile = _index(tmp_path, {})
    (tmp_path / "grok_collections.json").unlink()
    db.build_index(dbfile, tmp_path / "metadata.json", tmp_path / "gallery")
    assert db.facets(dbfile, view="all")["grok_tags"] == []


def _server():
    os.environ.setdefault("GROK_DATA_DIR", tempfile.mkdtemp())
    import server
    return server


def test_sync_runs_collections_per_account_and_a_failure_is_not_fatal(monkeypatch):
    server = _server()
    calls = []
    monkeypatch.setattr(server, "_load_accounts", lambda: [
        {"id": "default", "name": "acc1", "active": True}, {"id": "2172d829", "name": "acc2", "active": True}])
    monkeypatch.setattr(server, "_account_configured", lambda _id: True)
    monkeypatch.setattr(server, "_autonomous_enabled", lambda: False)

    def step(label, args):
        calls.append(args)
        return 1 if args[2] == "collections" else 0  # collections always fails here
    monkeypatch.setattr(server, "_run_step", step)
    server._sync_worker()
    seq = [a[2] for a in calls]
    assert seq.count("collections") == 2
    i = seq.index("collections")
    assert seq[i - 1] == "conversations" and seq.index("index") > max(j for j, s in enumerate(seq) if s == "collections")
    coll = [a for a in calls if a[2] == "collections"]
    assert [a[a.index("--account") + 1] for a in coll] == ["default", "2172d829"]
    assert server._sync["step"] == "done" and server._sync["returncode"] == 0
    calls.clear()
    server._sync_worker("2172d829", True)  # single-account deep sync also tags
    assert [a[a.index("--account") + 1] for a in calls if a[2] == "collections"] == ["2172d829"]


def test_collections_skipped_when_the_accounts_download_failed(monkeypatch):
    server = _server()
    calls = []
    monkeypatch.setattr(server, "_load_accounts", lambda: [{"id": "default", "name": "acc1", "active": True}])
    monkeypatch.setattr(server, "_account_configured", lambda _id: True)
    monkeypatch.setattr(server, "_autonomous_enabled", lambda: False)
    monkeypatch.setattr(server, "_run_step", lambda label, args: calls.append(args) or (1 if args[2] == "download" else 0))
    server._sync_worker()
    assert "collections" not in [a[2] for a in calls]


def test_deleting_an_account_forgets_its_collections(monkeypatch, tmp_path):
    server = _server()
    path = server.DATA_DIR / "grok_collections.json"
    server.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "accounts": {"x1": {}, "keep": {}}}))
    server._drop_grok_collections("x1")
    assert set(json.loads(path.read_text())["accounts"]) == {"keep"}
    path.unlink()


def test_cli_wiring(monkeypatch):
    import grokive
    seen = []
    monkeypatch.setattr(grokive, "run", lambda cmd: seen.append(cmd) or 0)
    monkeypatch.setattr("sys.argv", ["grokive.py", "collections", "--curl", "grok_accounts/abc.txt", "--account", "abc"])
    assert grokive.main() == 0
    assert seen[0][1].endswith("grokcollections.py") and seen[0][2:] == ["--curl", "grok_accounts/abc.txt", "--account", "abc"]
