"""Grok chat backup + search (grokchats.py). All HTTP mocked.

Run: python -m pytest test_grokchats.py
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

import gdownloader as g
import grokchats as gc

AUTH = g.RequestSpec(method="GET", url="https://grok.com/x", headers={"Content-Type": "x"}, cookies={"sso": "s"}, body=None)


def responses_for(cid):
    return {"responses": [
        {"responseId": f"{cid}-a", "sender": "ASSISTANT", "message": f"Answer about pelicans in {cid}",
         "createTime": "2026-04-01T10:00:05Z", "parentResponseId": f"{cid}-q", "model": "grok-3",
         "generatedImageUrls": ["users/u/generated/img1/image.jpg"]},
        {"responseId": f"{cid}-q", "sender": "human", "message": f"Tell me about pelicans {cid}",
         "createTime": "2026-04-01T10:00:00Z", "fileAttachments": ["file-1"]},
    ]}


class Fake:
    def __init__(self, pages, broken=(), empty_responses=()):
        self.pages = pages
        self.broken = set(broken)
        self.empty = set(empty_responses)
        self.fetched = []
        self.list_urls = []

    def __call__(self, _client, spec):
        u = urlparse(spec.url)
        parts = u.path.split("/")
        if u.path.endswith("/responses"):
            cid = parts[-2]
            if cid in self.broken:
                raise RuntimeError("boom")
            self.fetched.append(cid)
            return {"responses": []} if cid in self.empty else responses_for(cid)
        if u.path.endswith("/response-node"):
            if parts[-2] in self.broken:
                raise RuntimeError("boom")
            return {"responseNodes": [{"responseId": "n1", "sender": "human"}]}
        if u.path.endswith("/load-responses"):
            assert spec.method == "POST" and json.loads(spec.body) == {"responseIds": ["n1"]}
            return {"responses": [{"responseId": "n1", "sender": "human", "message": "via fallback"}]}
        self.list_urls.append(spec.url)
        q = parse_qs(u.query)
        assert "kind" not in q  # normal chats, not Imagine
        assert spec.method == "GET" and "Content-Type" not in spec.headers
        i = int(q.get("pageToken", ["0"])[0])
        convs = [{"conversationId": c, "title": f"Chat {c}", "createTime": f"2026-0{m}-01T00:00:00Z",
                  "modifyTime": mt} for c, m, mt in self.pages[i]]
        return {"conversations": convs, "nextPageToken": str(i + 1) if i + 1 < len(self.pages) else None}


@pytest.fixture(autouse=True)
def _nosleep(monkeypatch):
    monkeypatch.setattr(gc.time, "sleep", lambda *_: None)


def run(monkeypatch, tmp_path, fake, **kw):
    monkeypatch.setattr(gc, "_request", fake)
    return gc.sync_account(None, AUTH, tmp_path, "acct1", log=lambda *_: None, **kw)


PAGES = [[("c1", 9, "2026-09-02T00:00:00Z"), ("c2", 8, "2026-08-02T00:00:00Z")],
         [("c3", 4, "2026-04-02T00:00:00Z")]]


def test_full_then_incremental(monkeypatch, tmp_path):
    f = Fake(PAGES)
    r = run(monkeypatch, tmp_path, f)
    assert r["saved"] == 3 and sorted(f.fetched) == ["c1", "c2", "c3"]
    doc = json.loads((tmp_path / "chats/acct1/c1.json").read_text())
    assert doc["url"] == "https://grok.com/c/c1"
    assert [m["sender"] for m in doc["messages"]] == ["user", "assistant"]  # sorted by time
    assert doc["messages"][1]["attachments"][0]["url"].startswith("https://assets.grok.com/users/")
    assert doc["messages"][0]["attachments"][0]["ref"] == "file-1"
    # nothing changed: first page all unchanged -> stop early, nothing fetched
    f2 = Fake(PAGES)
    r2 = run(monkeypatch, tmp_path, f2)
    assert f2.fetched == [] and len(f2.list_urls) == 1
    # c2 modified
    pages = [[("c2", 8, "2026-10-01T00:00:00Z"), ("c1", 9, "2026-09-02T00:00:00Z")], PAGES[1]]
    f3 = Fake(pages)
    run(monkeypatch, tmp_path, f3)
    assert f3.fetched == ["c2"]
    # deep re-reads all
    f4 = Fake(PAGES)
    run(monkeypatch, tmp_path, f4, deep=True)
    assert sorted(f4.fetched) == ["c1", "c2", "c3"]


def test_failed_is_retried(monkeypatch, tmp_path):
    run(monkeypatch, tmp_path, Fake(PAGES, broken={"c3"}))
    state = json.loads((tmp_path / "chat_state.json").read_text())["accounts"]["acct1"]
    assert state["pending"] == ["c3"]
    f = Fake(PAGES)
    run(monkeypatch, tmp_path, f)
    assert f.fetched == ["c3"]


def test_fallback_response_node(monkeypatch, tmp_path):
    monkeypatch.setattr(gc, "_request", Fake(PAGES, empty_responses={"c1"}))
    msgs = gc.fetch_messages(None, AUTH, "c1")
    assert msgs[0]["text"] == "via fallback"


def test_parse_defensive():
    assert gc.parse_responses(None) == []
    assert gc.parse_responses({"data": {"responses": [{"text": [{"type": "text", "text": "hi"}], "role": "user"}]}})[0] == \
        {"id": "0", "parent_id": None, "sender": "user", "text": "hi", "time": None, "model": None, "attachments": []}
    assert gc.parse_conversation_entry({"id": "../evil"}) is None
    e = gc.parse_conversation_entry({"conversationId": "x1", "createTime": 1700000000000})
    assert e["create_time"] == "2023-11-14T22:13:20Z"


def test_index_search_months(monkeypatch, tmp_path):
    run(monkeypatch, tmp_path, Fake(PAGES))
    db = gc.ensure_index(tmp_path)
    res = gc.search(db, "pelican")
    assert res["total"] == 3
    snip = res["items"][0]["snippets"][0]["snippet"]
    assert gc.HL_OPEN + "pelican" in snip.lower()
    assert gc.search(db, "Chat c3")["items"][0]["id"] == "c3"
    assert gc.search(db, "", start="2026-04-01", end="2026-05-01")["items"][0]["id"] == "c3"
    assert gc.search(db, "", sort="old")["items"][0]["id"] == "c3"
    assert gc.search(db, "zzzz")["total"] == 0
    assert gc.search(db, 'a" OR (')["total"] >= 0  # hostile input doesn't crash
    assert [m["month"] for m in gc.months(db)] == ["2026-09", "2026-08", "2026-04"]
    assert gc.search(db, "", account="nobody")["total"] == 0


def test_server_endpoints(monkeypatch, tmp_path):
    import server
    run(monkeypatch, tmp_path, Fake(PAGES))
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "_auth_mode", lambda: "off", raising=False)
    c = server.app.test_client()
    r = c.get("/api/chats?q=pelicans&period=m:2026-09&tz_offset=0")
    if r.status_code in (401, 302):
        pytest.skip("auth on in test env")
    j = r.get_json()
    assert j["total"] == 1 and j["items"][0]["id"] == "c1"
    assert c.get("/api/chats?period=y:2026").get_json()["total"] == 3
    assert c.get("/api/chats/months").get_json()["months"][0]["month"] == "2026-09"
    assert c.get("/api/chats/acct1/c2").get_json()["title"] == "Chat c2"
    assert c.get("/api/chats/acct1/bad%24id").status_code == 404


def test_meta_tags_archive_filters(monkeypatch, tmp_path):
    run(monkeypatch, tmp_path, Fake(PAGES))
    db = gc.ensure_index(tmp_path)
    meta = gc.load_meta(tmp_path / "nope.json")
    gc.update_meta(meta, ["acct1/c1", "acct1/c2"], add=["Work", "blender"])
    gc.update_meta(meta, ["acct1/c2"], remove=["blender"], archived=True)
    gc.save_meta(tmp_path / "chat_meta.json", meta)
    meta = gc.load_meta(tmp_path / "chat_meta.json")
    assert meta["chats"]["acct1/c2"] == {"tags": ["Work"], "archived": True}
    ids = lambda **kw: sorted(i["id"] for i in gc.search(db, meta=meta, **kw)["items"])
    assert ids(archived="active") == ["c1", "c3"]
    assert ids(archived="archived") == ["c2"]
    assert ids(archived="all", tags=["work"]) == ["c1", "c2"]
    assert ids(archived="all", tags=["work", "blender"], tag_mode="all") == ["c1"]
    assert ids(archived="all", q="#work pelicans") == ["c1", "c2"]
    assert ids(archived="active", q="pelicans #blender") == ["c1"]
    it = gc.search(db, meta=meta, archived="archived")["items"][0]
    assert it["tags"] == ["Work"] and it["archived"] is True
    f = gc.facets(db, meta, "active")
    assert f["accounts"] == [{"id": "acct1", "count": 2}] and f["archived"] == 1
    assert {t["name"]: t["count"] for t in f["tags"]} == {"blender": 1, "Work": 1}
    assert f["show"] == {"inbox": 2, "archived": 1, "all": 3,
                         "srcdel_hide": 2, "srcdel_all": 2, "srcdel_only": 0}
    # sync never touches meta
    run(monkeypatch, tmp_path, Fake(PAGES), deep=True)
    assert gc.load_meta(tmp_path / "chat_meta.json") == meta
    gc.update_meta(meta, ["acct1/c2"], remove=["work"], archived=False)
    assert "acct1/c2" not in meta["chats"]


def test_server_meta_endpoint(monkeypatch, tmp_path):
    import server
    run(monkeypatch, tmp_path, Fake(PAGES))
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "CHAT_META_FILE", tmp_path / "chat_meta.json")
    monkeypatch.setattr(server, "_auth_mode", lambda: "off", raising=False)
    c = server.app.test_client()
    r = c.post("/api/chats/meta", json={"chats": [{"account": "acct1", "id": "c1"}], "add": ["x"], "archived": True})
    if r.status_code in (401, 302):
        pytest.skip("auth on")
    assert r.get_json()["ok"]
    assert c.get("/api/chats").get_json()["total"] == 2
    assert c.get("/api/chats?archived=all&tags=x").get_json()["items"][0]["id"] == "c1"
    assert c.get("/api/chats/facets?archived=all").get_json()["tags"] == [{"name": "x", "count": 1}]
    assert c.get("/api/chats/acct1/c1").get_json()["tags"] == ["x"]
    assert c.post("/api/chats/meta", json={"chats": [{"account": "../a", "id": "c1"}]}).status_code == 400


def test_tags_are_cross_account(monkeypatch, tmp_path):
    """A tag name is one shared vocabulary across accounts; facets/search span them."""
    # Two accounts, same tag name on both.
    pages_a = [[("c1", 9, "2026-09-02T00:00:00Z")]]
    pages_b = [[("c2", 8, "2026-08-02T00:00:00Z")]]
    monkeypatch.setattr(gc, "_request", Fake(pages_a))
    gc.sync_account(None, AUTH, tmp_path, "alice", log=lambda *_: None)
    monkeypatch.setattr(gc, "_request", Fake(pages_b))
    gc.sync_account(None, AUTH, tmp_path, "bob", log=lambda *_: None)
    db = gc.ensure_index(tmp_path)
    meta = gc.load_meta(tmp_path / "nope.json")
    gc.update_meta(meta, ["alice/c1", "bob/c2"], add=["Shared"])
    gc.update_meta(meta, ["bob/c2"], add=["BobOnly"])
    f_all = gc.facets(db, meta, "all", None)
    assert {t["name"]: t["count"] for t in f_all["tags"]} == {"BobOnly": 1, "Shared": 2}
    f_alice = gc.facets(db, meta, "all", "alice")
    assert {t["name"]: t["count"] for t in f_alice["tags"]} == {"Shared": 1}
    hits = gc.search(db, tags=["shared"], archived="all", meta=meta)["items"]
    assert sorted((h["account"], h["id"]) for h in hits) == [("alice", "c1"), ("bob", "c2")]
    # Tags that live only on archived chats must still appear in the global facet list
    # the picker uses (archived=all), even though the Inbox panel filter shows none.
    gc.update_meta(meta, ["alice/c1", "bob/c2"], archived=True)
    inbox = gc.facets(db, meta, "active", None)
    assert inbox["tags"] == []  # panel: no tagged chats in Inbox
    assert inbox["show"]["inbox"] == 0 and inbox["show"]["archived"] == 2
    global_tags = gc.facets(db, meta, "all", None, srcdel="all")
    assert {t["name"]: t["count"] for t in global_tags["tags"]} == {"BobOnly": 1, "Shared": 2}
