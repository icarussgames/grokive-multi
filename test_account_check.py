"""Per-account "Check auth" and "Sync this account" (Config → Grok accounts).

The check makes ONE authenticated request (a 1-post /rest/media/post/list) with the
account's saved cURL and classifies the answer; the HTTP call is mocked here. The
per-account sync runs the normal pipeline for just that account in the shared job slot.

Run: python -m pytest test_account_check.py
"""

from __future__ import annotations

import json
import os
import tempfile

import httpx
import pytest

SECRET = "sso=TOPSECRETCOOKIE123; cf_clearance=CFSECRET456"
CURL = ("curl 'https://grok.com/rest/media/post/list' -H 'User-Agent: Mozilla/5.0' "
        f"-H 'Cookie: {SECRET}' --data-raw '{{\"limit\":40}}'")


def _server():
    # Lazy import: see test_accounts._server.
    os.environ.setdefault("GROK_DATA_DIR", tempfile.mkdtemp())
    import server
    return server


@pytest.fixture
def srv(tmp_path, monkeypatch):
    server = _server()
    accounts_dir = tmp_path / "grok_accounts"
    accounts_dir.mkdir()
    monkeypatch.setattr(server, "CURL_FILE", tmp_path / "grok_auth.txt")
    monkeypatch.setattr(server, "LEGACY_CURL_FILE", tmp_path / "curl_samples.txt")
    monkeypatch.setattr(server, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(server, "ACCOUNTS_FILE", tmp_path / "grok_accounts.json")
    (tmp_path / "grok_auth.txt").write_text(CURL, encoding="utf-8")
    (accounts_dir / "2172d829.txt").write_text(CURL.replace("TOPSECRET", "OTHER"), encoding="utf-8")
    (tmp_path / "grok_accounts.json").write_text(json.dumps([
        {"id": "default", "name": "twitter-acc1", "active": True},
        {"id": "2172d829", "name": "epsilontaumake", "active": False},
        {"id": "deadbeef", "name": "no-session", "active": True},
    ]), encoding="utf-8")
    server._auth_checks.clear()
    with server._sync_lock:
        server._sync["running"] = False
    return server


def _client(server):
    c = server.app.test_client()
    with c.session_transaction() as sess:
        sess["authed"] = True
    return c


def _fake_post(monkeypatch, server, status=200, text='{"posts":[]}', headers=None, exc=None):
    calls = []

    def fake(url, headers=None, content=None, timeout=None, **_kw):
        calls.append({"url": url, "headers": headers, "body": content})
        if exc is not None:
            raise exc
        return httpx.Response(status, text=text, headers=hdrs, request=httpx.Request("POST", url))

    hdrs = headers or {}
    monkeypatch.setattr(server.httpx, "post", fake)
    return calls


def test_check_ok_uses_one_item_post_list_with_the_accounts_cookies(srv, monkeypatch):
    calls = _fake_post(monkeypatch, srv)
    r = _client(srv).post("/api/accounts/default/check")
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True and j["status"] == 200
    assert j["message"] == "Session OK" and j["checked_at"]
    assert len(calls) == 1
    assert calls[0]["url"] == "https://grok.com/rest/media/post/list"
    assert json.loads(calls[0]["body"])["limit"] == 1
    assert "TOPSECRETCOOKIE123" in calls[0]["headers"]["Cookie"]  # sent to Grok...
    assert "TOPSECRET" not in r.get_data(as_text=True)              # ...never returned
    # Each account uses its own file.
    _client(srv).post("/api/accounts/2172d829/check")
    assert "OTHERCOOKIE123" in calls[-1]["headers"]["Cookie"]


@pytest.mark.parametrize("status,text,headers,expect", [
    (401, '{"error":"unauthorized"}', None, "Expired / 401"),
    (403, "<html><title>Just a moment...</title></html>", None, "Cloudflare"),
    (403, "", {"cf-mitigated": "challenge"}, "cf_clearance"),
    (403, '{"error":"nope"}', None, "Forbidden / 403"),
    (429, "", None, "Rate limited"),
    (500, "", None, "HTTP 500"),
    (200, "<html>sign in</html>", None, "not with JSON"),
])
def test_check_classifies_failures(srv, monkeypatch, status, text, headers, expect):
    _fake_post(monkeypatch, srv, status=status, text=text, headers=headers)
    j = _client(srv).post("/api/accounts/default/check").get_json()
    assert j["ok"] is False and j["status"] == status
    assert expect in j["message"]


def test_check_network_error_and_missing_session(srv, monkeypatch):
    calls = _fake_post(monkeypatch, srv, exc=httpx.ConnectError("boom " + SECRET))
    j = _client(srv).post("/api/accounts/default/check").get_json()
    assert j["ok"] is False and j["status"] == "network" and "SECRET" not in json.dumps(j)
    j = _client(srv).post("/api/accounts/deadbeef/check").get_json()
    assert j["status"] == "no-session" and len(calls) == 1  # no request without a session
    assert _client(srv).post("/api/accounts/nope/check").status_code == 404


def test_check_all_records_results_on_the_account_list(srv, monkeypatch):
    _fake_post(monkeypatch, srv, status=401)
    c = _client(srv)
    res = {x["id"]: x for x in c.post("/api/accounts/check").get_json()["results"]}
    assert set(res) == {"default", "2172d829", "deadbeef"}
    assert res["default"]["status"] == 401 and res["deadbeef"]["status"] == "no-session"
    listed = {a["id"]: a for a in c.get("/api/accounts").get_json()["accounts"]}
    assert listed["default"]["last_check"]["message"].startswith("Expired")
    # Pasting a fresh cURL clears the stale verdict.
    c.post("/api/accounts/default", json={"curl": CURL})
    listed = {a["id"]: a for a in c.get("/api/accounts").get_json()["accounts"]}
    assert listed["default"]["last_check"] is None


def test_sync_one_account_runs_the_pipeline_for_just_that_account(srv, monkeypatch):
    calls, labels = [], []
    monkeypatch.setattr(srv, "_run_step", lambda label, args: (labels.append(label), calls.append(args)) and 0)
    monkeypatch.setattr(srv, "_autonomous_enabled", lambda: False)
    srv._sync_worker("2172d829")  # paused accounts can still be synced on request
    per = [a for a in calls if a[2] in ("download", "agents", "conversations")]
    assert [a[2] for a in per] == ["download", "agents", "conversations"]
    assert all(a[a.index("--account") + 1] == "2172d829" and "grok_accounts/2172d829.txt" in a for a in per)
    assert calls[0][2] == "reindex" and "index" in [a[2] for a in calls]
    assert "download [epsilontaumake]" in labels  # pill/log name the account
    assert srv._sync["step"] == "done"


def test_sync_one_account_routes(srv, monkeypatch):
    started = []
    monkeypatch.setattr(srv, "start_sync", lambda acct=None: started.append(acct) or True)
    c = _client(srv)
    r = c.post("/api/accounts/2172d829/sync")
    assert r.status_code == 200 and r.get_json()["account"]["name"] == "epsilontaumake"
    r = c.post("/api/sync", json={"account": "default"})
    assert r.status_code == 200 and started[-1]["id"] == "default"
    assert c.post("/api/accounts/deadbeef/sync").status_code == 400  # no session
    assert c.post("/api/accounts/nope/sync").status_code == 404
    monkeypatch.setattr(srv, "start_sync", lambda acct=None: False)
    r = c.post("/api/accounts/default/sync")
    assert r.status_code == 409 and "already running" in r.get_json()["error"]


def test_status_reports_the_single_account(srv, monkeypatch):
    monkeypatch.setattr(srv.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    assert srv.start_sync({"id": "2172d829", "name": "epsilontaumake", "active": False}) is True
    st = _client(srv).get("/api/sync/status").get_json()
    assert st["running"] and st["account"] == {"id": "2172d829", "name": "epsilontaumake"}
    assert srv.start_sync() is False  # slot busy
    with srv._sync_lock:
        srv._sync["running"] = False
