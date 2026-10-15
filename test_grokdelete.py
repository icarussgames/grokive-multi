"""Delete-on-grok.com (grokdelete + server endpoints). All HTTP mocked."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import gdownloader as g
import grokdelete as gd
import grokchats as gc

AUTH = g.RequestSpec(method="GET", url="https://grok.com/x", headers={"X": "1"}, cookies={"sso": "s"}, body=None)
CID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
MID = "11111111-2222-3333-4444-555555555555"


class FakeResp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


def test_build_specs():
    soft = gd.build_spec(AUTH, "chat", CID)
    assert soft.method == "DELETE" and soft.url.endswith(f"/conversations/soft/{CID}")
    hard = gd.build_spec(AUTH, "chat", CID, hard=True)
    assert "/soft/" not in hard.url and hard.url.endswith(f"/conversations/{CID}")
    med = gd.build_spec(AUTH, "media", MID)
    assert med.method == "POST" and med.url.endswith("/media/post/delete") and json.loads(med.body) == {"id": MID}
    with pytest.raises(ValueError):
        gd.build_spec(AUTH, "chat", "../x")


def test_delete_one_retries_429(monkeypatch):
    calls = []

    def send(_c, _s):
        calls.append(1)
        return FakeResp(429) if len(calls) < 3 else FakeResp(200, "ok")

    monkeypatch.setattr(gd, "_send", send)
    monkeypatch.setattr(gd.time, "sleep", lambda *_: None)
    assert gd.delete_one(None, gd.build_spec(AUTH, "chat", CID))["ok"]
    assert len(calls) == 3
    assert gd.delete_one(None, gd.build_spec(AUTH, "chat", CID))  # 404 already
    monkeypatch.setattr(gd, "_send", lambda *_: FakeResp(404))
    r = gd.delete_one(None, gd.build_spec(AUTH, "chat", CID))
    assert r["ok"] and r["already"]


def test_batch_tries_accounts(monkeypatch):
    monkeypatch.setattr(gd.time, "sleep", lambda *_: None)
    seen = []

    def send(_c, spec):
        acct = "a1" if "a1" in str(id(spec)) else "x"  # not useful
        seen.append(spec.url)
        # first account 404, second 200
        return FakeResp(404 if "/soft/" in spec.url and len(seen) == 1 else 200)

    # Better: track by cookie presence via auth_for
    monkeypatch.setattr(gd, "_send", lambda _c, spec: FakeResp(404 if "acct1" in (spec.headers.get("X-Acct") or "") else 200))

    def auth_for(a):
        return g.RequestSpec(method="GET", url="u", headers={"X-Acct": a}, cookies={"s": "1"}, body=None)

    # redefine send based on header
    def send2(_c, spec):
        return FakeResp(404 if spec.headers.get("X-Acct") == "acct1" else 200)

    monkeypatch.setattr(gd, "_send", send2)
    jobs = [{"kind": "chat", "id": CID, "accounts": ["acct1", "acct2"]}]
    out = gd.run_batch(None, jobs, auth_for, delay=0)
    assert out[0]["ok"] and out[0]["account"] == "acct2"


def test_server_gate_and_job(monkeypatch, tmp_path):
    import server
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "SOURCE_DELETED_FILE", tmp_path / "source_deleted.json")
    monkeypatch.setattr(server, "CHAT_META_FILE", tmp_path / "chat_meta.json")
    monkeypatch.setattr(server, "METADATA_FILE", tmp_path / "metadata.json")
    monkeypatch.setattr(server, "ACCOUNTS_FILE", tmp_path / "grok_accounts.json")
    monkeypatch.setattr(server, "_auth_mode", lambda: "off", raising=False)
    monkeypatch.setattr(server, "_load_settings", lambda: {})
    monkeypatch.setattr(server, "_source_delete_enabled", lambda: False)
    c = server.app.test_client()
    r = c.post("/api/source-delete", json={"kind": "chat", "items": [{"account": "a", "id": CID}], "confirm": True})
    assert r.status_code == 403

    # seed a chat + account curl
    (tmp_path / "chats" / "acct1").mkdir(parents=True)
    gc.save_conversation(tmp_path, "acct1", {"id": CID, "title": "T", "create_time": "2026-01-01T00:00:00Z",
                                              "modify_time": "2026-01-01T00:00:00Z"}, [])
    gc.touch_stamp(tmp_path); gc.build_index(tmp_path)
    (tmp_path / "grok_accounts").mkdir()
    (tmp_path / "grok_accounts" / "acct1.txt").write_text(
        "curl 'https://grok.com/rest/x' -H 'cookie: sso=1'\n", encoding="utf-8")
    json.dump([{"id": "acct1", "name": "A1", "active": True}], open(tmp_path / "grok_accounts.json", "w"))
    monkeypatch.setattr(server, "ACCOUNTS_DIR", tmp_path / "grok_accounts")
    monkeypatch.setattr(server, "_source_delete_enabled", lambda: True)
    monkeypatch.setattr(server, "_load_accounts", lambda: [{"id": "acct1", "name": "A1", "active": True}])
    monkeypatch.setattr(server, "_account_configured", lambda a: True)
    monkeypatch.setattr(server, "_account_auth", lambda a: AUTH)

    # type-confirm for >10
    many = [{"account": "acct1", "id": CID}] * 11
    prev = c.post("/api/source-delete/preview", json={"kind": "chat", "items": many}).get_json()
    assert prev["type_confirm"] and prev["count"] == 11
    bad = c.post("/api/source-delete", json={"kind": "chat", "items": many, "confirm": True})
    assert bad.status_code == 400

    monkeypatch.setattr(gd.time, "sleep", lambda *_: None)
    monkeypatch.setattr(gd, "_send", lambda *_: FakeResp(200, "{}"))
    # Import path: worker imports grokdelete; patch module used by worker
    import grokdelete as gdm
    monkeypatch.setattr(gdm, "_send", lambda *_: FakeResp(200, "{}"))
    monkeypatch.setattr(gdm.time, "sleep", lambda *_: None)
    # Run the worker inline so the test doesn't depend on a background thread.
    monkeypatch.setattr(server.threading, "Thread",
                        lambda target, args, daemon=True: type("T", (), {"start": lambda self: target(*args)})())

    start = c.post("/api/source-delete", json={"kind": "chat", "items": [{"account": "acct1", "id": CID}], "confirm": True})
    assert start.status_code == 200, start.get_json()
    job = start.get_json()["job"]
    st = c.get(f"/api/source-delete/{job}").get_json()
    assert st["succeeded"] == 1 and not st["running"], st
    gone = json.loads((tmp_path / "source_deleted.json").read_text())
    assert f"acct1/{CID}" in gone["chat"]
    # chat list hides by default
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)  # ensure
    listed = c.get("/api/chats").get_json()
    assert listed["total"] == 0
    assert c.get("/api/chats?srcdel=only").get_json()["total"] == 1
    assert c.get("/api/chats?srcdel=all").get_json()["items"][0]["source_deleted"]


def test_probe_cli(monkeypatch, tmp_path, capsys):
    import server
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "_account_auth", lambda a: AUTH)
    monkeypatch.setattr(server, "_account_configured", lambda a: True)
    rc = server._probe_delete(["--probe-delete", "acct1", "chat", CID])
    out = capsys.readouterr().out
    assert rc == 0 and "DELETE" in out and "/soft/" in out and "dry run" in out
