"""Per-account attribution: gdownloader records which Grok account listed each item
(`accounts` in metadata.json, including already-held items it skips), unattributed
records stay "unknown", and index.db / the API filter + count by account.

Run: python -m pytest test_accounts.py
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

import db
import gdownloader


def _server():
    """Import `server` lazily: its env (data dir, auth) is fixed by whichever test module
    imports it first, so importing at collection time here would change the environment
    the other server test modules (collected later) expect."""
    os.environ.setdefault("GROK_DATA_DIR", tempfile.mkdtemp())
    import server
    return server


def _args(tmp: Path) -> argparse.Namespace:
    return argparse.Namespace(metadata=tmp / "metadata.json", failures=tmp / "failed.json", quiet=True)


def test_account_from_curl_path():
    assert gdownloader.account_from_curl_path(Path("grok_accounts/2172d829.txt")) == "2172d829"
    assert gdownloader.account_from_curl_path(Path("grok_auth.txt")) == "default"
    assert gdownloader.account_from_curl_path(Path("curl_samples.txt")) == "default"


def test_ingest_and_skip_existing_both_record_the_account(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gdownloader, "DELETED_IDS", set())
    args = _args(tmp_path)
    url = "https://assets.grok.com/users/u/generated/aaa/image.jpg"
    held = {"id": "held", "media_type": "image", "prompt": "p", "source_url": url,
            "local_path": "media/images/xx/held.jpg"}
    by_id = {"held": dict(held)}
    # An item already on disk but not in metadata is "indexed" (no network) -> new record.
    shard = gdownloader.media_shard("fresh")
    f = tmp_path / "gallery" / "media" / "images" / shard / "fresh.jpg"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    monkeypatch.setattr(gdownloader, "ACCOUNT_ID", "2172d829")
    raw_fresh = {"id": "fresh", "source_url": "https://assets.grok.com/users/u/generated/fresh/image.jpg",
                 "media_type": "image", "prompt": "q"}
    assert gdownloader.process_item(None, None, raw_fresh, by_id, args) is False  # indexed, not downloaded
    assert by_id["fresh"]["accounts"] == ["2172d829"]
    # Already held + unchanged URL: skipped, but still attributed (serial and batch paths).
    gdownloader.process_item(None, None, {"id": "held", "source_url": url}, by_id, args)
    assert by_id["held"]["accounts"] == ["2172d829"]
    monkeypatch.setattr(gdownloader, "ACCOUNT_ID", "default")
    monkeypatch.setattr(gdownloader, "DOWNLOAD_WORKERS", 3)
    gdownloader.process_items(None, None, [{"id": "held", "source_url": url}], by_id, args)
    assert by_id["held"]["accounts"] == ["2172d829", "default"]  # an item can belong to both
    gdownloader.process_items(None, None, [{"id": "held", "source_url": url}], by_id, args)
    assert by_id["held"]["accounts"] == ["2172d829", "default"]  # no duplicates


def test_refresh_metadata_listing_attributes_without_downloading(monkeypatch):
    monkeypatch.setattr(gdownloader, "ACCOUNT_ID", "default")
    by_id = {"a": {"id": "a", "prompt": "x", "created_at": "2026-01-01", "model": "m"}}
    assert gdownloader.patch_existing_record({"id": "a"}, by_id) is True
    assert by_id["a"]["accounts"] == ["default"]
    assert gdownloader.patch_existing_record({"id": "a"}, by_id) is False
    assert gdownloader.patch_existing_record({"id": "missing"}, by_id) is False  # never creates records


def _write_library() -> None:
    server = _server()
    server.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for path in (server.COLLECTIONS_FILE, server.COLLECTION_GROUPS_FILE, server.LIBRARY_FILE,
                 server.DB_FILE, server.USER_TAGS_FILE):
        path.unlink(missing_ok=True)
    server._locked_cache["mtime"] = None
    server.METADATA_FILE.write_text(json.dumps([
        {"id": "d1", "media_type": "image", "prompt": "one", "created_at": "2026-09-26", "local_path": "media/images/aa/d1.jpg",
         "accounts": ["default"]},
        {"id": "e1", "media_type": "video", "prompt": "two", "created_at": "2026-09-27", "local_path": "media/videos/aa/e1.mp4",
         "accounts": ["2172d829"]},
        {"id": "both", "media_type": "image", "prompt": "three", "created_at": "2026-09-27", "local_path": "media/images/aa/b.jpg",
         "accounts": ["default", "2172d829"]},
        {"id": "old", "media_type": "image", "prompt": "four", "created_at": "2026-09-20", "local_path": "media/images/aa/o.jpg"},
        {"id": "import_1", "media_type": "image", "prompt": "", "created_at": "2026-09-21", "local_path": "media/images/aa/i.jpg"},
    ]), encoding="utf-8")
    server.rebuild_db(wait=True)


def _client():
    c = _server().app.test_client()
    with c.session_transaction() as sess:
        sess["authed"] = True
    return c


def _ids(c, **params) -> list[str]:
    r = c.get("/api/media", query_string={"view": "all", **params})
    assert r.status_code == 200, r.data
    return sorted(it["id"] for it in r.get_json()["items"])


def test_filter_by_account_unknown_and_all():
    _write_library()
    c = _client()
    assert _ids(c) == ["both", "d1", "e1", "import_1", "old"]
    assert _ids(c, account="all") == ["both", "d1", "e1", "import_1", "old"]
    assert _ids(c, account="default") == ["both", "d1"]
    assert _ids(c, account="2172d829") == ["both", "e1"]
    assert _ids(c, account=db.UNKNOWN_ACCOUNT) == ["old"]  # unattributed is NOT folded into default
    assert _ids(c, account=db.LOCAL_ACCOUNT) == ["import_1"]
    assert _ids(c, account="2172d829", q="three") == ["both"]  # composes with search
    item = next(it for it in c.get("/api/media", query_string={"view": "all"}).get_json()["items"] if it["id"] == "both")
    assert item["accounts"] == ["default", "2172d829"]


def test_facets_count_accounts_and_scope_other_facets():
    _write_library()
    c = _client()
    f = c.get("/api/facets", query_string={"view": "all"}).get_json()
    assert {a["id"]: a["count"] for a in f["accounts"]} == {
        "default": 2, "2172d829": 2, db.UNKNOWN_ACCOUNT: 1, db.LOCAL_ACCOUNT: 1}
    # The account switch scopes the other facets but keeps listing every account.
    f = c.get("/api/facets", query_string={"view": "all", "account": "2172d829"}).get_json()
    assert sum(m["count"] for m in f["models"]) == 2
    assert {a["id"] for a in f["accounts"]} == {"default", "2172d829", db.UNKNOWN_ACCOUNT, db.LOCAL_ACCOUNT}


def test_sync_passes_account_ids_to_the_cli(monkeypatch):
    server = _server()
    calls = []
    monkeypatch.setattr(server, "_load_accounts", lambda: [
        {"id": "default", "name": "twitter-acc1", "active": True},
        {"id": "2172d829", "name": "epsilontaumake", "active": True}])
    monkeypatch.setattr(server, "_account_configured", lambda _id: True)
    monkeypatch.setattr(server, "_run_step", lambda label, args: calls.append(args) or 0)
    monkeypatch.setattr(server, "_autonomous_enabled", lambda: False)
    server._sync_worker()
    per_account = [a for a in calls if a[2] in ("download", "agents", "conversations")]
    assert len(per_account) == 6
    assert all(a[a.index("--account") + 1] == ("default" if "grok_auth.txt" in a else "2172d829") for a in per_account)

    calls.clear()
    server._attribute_worker(None)
    attr = [a for a in calls if a[2] == "attribute"]
    assert [a[a.index("--account") + 1] for a in attr] == ["default", "2172d829"]
    assert calls[-1][2] == "index"
