"""External read-only API (/api/v1) — API key + localhost gate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import external_api as ea
import grokchats as gc
import gdownloader as g


def test_key_generate_verify_revoke(tmp_path):
    assert ea.load_key_record(tmp_path) is None
    key = ea.generate_key(tmp_path)
    assert key.startswith("gk_") and ea.verify_key(tmp_path, key)
    assert ea.verify_key(tmp_path, f"Bearer {key}")
    assert not ea.verify_key(tmp_path, "gk_wrong")
    rec = ea.load_key_record(tmp_path)
    assert "hash" in rec and key not in Path(tmp_path / "api_key.json").read_text()
    ea.revoke_key(tmp_path)
    assert not ea.verify_key(tmp_path, key)


def test_list_and_query(tmp_path):
    # chats
    for acct, cid, tag in [("a1", "c1", "Work"), ("a2", "c2", "Work"), ("a1", "c3", "Other")]:
        gc.save_conversation(tmp_path, acct, {
            "id": cid, "title": f"T-{cid}", "create_time": "2026-01-01T00:00:00Z",
            "modify_time": "2026-01-02T00:00:00Z"}, [
            {"id": "q", "sender": "user", "text": f"hello {cid}", "time": "2026-01-01T00:00:00Z", "attachments": []}])
    gc.touch_stamp(tmp_path)
    meta = gc.load_meta(tmp_path / "x")
    gc.update_meta(meta, ["a1/c1", "a2/c2"], add=["Work"])
    gc.update_meta(meta, ["a1/c3"], add=["Other"])
    gc.save_meta(tmp_path / "chat_meta.json", meta)
    db = gc.ensure_index(tmp_path)
    # media index stub
    import sqlite3
    con = sqlite3.connect(tmp_path / "index.db")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS media(
      id TEXT PRIMARY KEY, media_type TEXT, prompt TEXT, normalized_prompt TEXT,
      model TEXT, created_at TEXT, parent_id TEXT, source_url TEXT, local_path TEXT,
      href TEXT, thumb TEXT, subtitles TEXT, canvas_id TEXT, canvas_name TEXT,
      thumb_w INT, thumb_h INT, media_w INT, media_h INT, has_subtitles INT,
      size_bytes INT, api_generated INT, preset TEXT, is_root_celebrity INT,
      r_rated INT, moderated INT, is_ext INT);
    CREATE TABLE IF NOT EXISTS media_tags(media_id TEXT, tag TEXT, source TEXT);
    CREATE TABLE IF NOT EXISTS media_accounts(media_id TEXT, account_id TEXT);
    """)
    con.execute("INSERT INTO media(id,media_type,prompt,created_at,local_path,source_url,media_w,media_h) VALUES(?,?,?,?,?,?,?,?)",
                ("m1", "image", "a cat", "2026-01-01T00:00:00Z", "media/ab/m1.jpg", "https://assets.grok.com/x", 100, 80))
    con.execute("INSERT INTO media_tags VALUES('m1','Work','user')")
    con.execute("INSERT INTO media_accounts VALUES('m1','a1')")
    con.commit(); con.close()
    (tmp_path / "gallery" / "media" / "ab").mkdir(parents=True)
    (tmp_path / "gallery" / "media" / "ab" / "m1.jpg").write_bytes(b"x")
    json.dump([{"id": "a1", "name": "icaro3d"}, {"id": "a2", "name": "etobare"}],
              open(tmp_path / "grok_accounts.json", "w"))

    tags = ea.list_tags(tmp_path, tmp_path / "index.db")
    by = {t["name"]: t for t in tags}
    assert by["Work"] == {"name": "Work", "chats": 2, "media": 1, "total": 3}
    assert by["Other"]["chats"] == 1

    chats = ea.query_chats_by_tag(tmp_path, "Work", include="full")
    assert chats["total"] == 2
    assert chats["items"][0]["messages"][0]["text"].startswith("hello")
    assert chats["items"][0]["url"].startswith("https://grok.com/c/")

    media = ea.query_media_by_tag(tmp_path / "index.db", tmp_path / "gallery", "Work",
                                  base_url="http://127.0.0.1:8080", accounts_file=tmp_path / "grok_accounts.json")
    assert media["total"] == 1
    it = media["items"][0]
    assert it["local_url"] == "http://127.0.0.1:8080/media/media/ab/m1.jpg"
    assert it["local_path"].endswith("m1.jpg")
    assert it["account_labels"] == ["icaro3d"]


def test_server_v1(monkeypatch, tmp_path):
    import server
    key = ea.generate_key(tmp_path)
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "DB_FILE", tmp_path / "index.db")
    monkeypatch.setattr(server, "GALLERY_DIR", tmp_path / "gallery")
    monkeypatch.setattr(server, "MEDIA_DIR", tmp_path / "gallery" / "media")
    monkeypatch.setattr(server, "ACCOUNTS_FILE", tmp_path / "grok_accounts.json")
    monkeypatch.setattr(server, "CHAT_META_FILE", tmp_path / "chat_meta.json")
    monkeypatch.setattr(server, "SOURCE_DELETED_FILE", tmp_path / "source_deleted.json")
    monkeypatch.setattr(server, "_auth_mode", lambda: "off", raising=False)
    monkeypatch.setattr(server, "_admin_configured", lambda: False)
    monkeypatch.setattr(server, "_load_settings", lambda: {})
    # Need chats seeded - call helper pieces
    for acct, cid, tags in [("a1", "c1", ["Work"])]:
        gc.save_conversation(tmp_path, acct, {
            "id": cid, "title": "T", "create_time": "2026-01-01T00:00:00Z",
            "modify_time": "2026-01-01T00:00:00Z"}, [
            {"id": "q", "sender": "user", "text": "hi", "time": "2026-01-01T00:00:00Z", "attachments": []}])
    gc.touch_stamp(tmp_path); gc.build_index(tmp_path)
    meta = {"version": 1, "chats": {"a1/c1": {"tags": ["Work"]}}}
    gc.save_meta(tmp_path / "chat_meta.json", meta)
    json.dump([{"id": "a1", "name": "icaro3d"}], open(tmp_path / "grok_accounts.json", "w"))

    c = server.app.test_client()
    # no key
    assert c.get("/api/v1/tags", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).status_code in (401, 403)
    h = {"Authorization": f"Bearer {key}"}
    # non-localhost blocked
    r = c.get("/api/v1/tags", headers=h, environ_overrides={"REMOTE_ADDR": "8.8.8.8"})
    assert r.status_code == 403
    r = c.get("/api/v1/tags", headers=h, environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 200 and r.get_json()["ok"]
    assert any(t["name"] == "Work" for t in r.get_json()["tags"])
    r = c.get("/api/v1/tag/Work?kind=chats&include=full", headers={"X-API-Key": key},
              environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
    j = r.get_json()
    assert j["chats"]["total"] == 1 and j["chats"]["items"][0]["messages"]
    r = c.get("/api/v1/chats/a1/c1", headers=h, environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
    assert r.get_json()["title"] == "T"
    # generate endpoint needs session when auth on — with auth off it still works
    g = c.post("/api/external-api/generate")
    assert g.status_code == 200 and g.get_json()["key"].startswith("gk_")
