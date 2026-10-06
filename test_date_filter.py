"""Date navigation: period=last30/last60/m:YYYY-MM bounds, the months facet, and how
they combine with the other filters. Run: python -m pytest test_date_filter.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile

import db

NOW = dt.datetime.now(dt.timezone.utc)


def iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.123456Z")


def _server():
    os.environ.setdefault("GROK_DATA_DIR", tempfile.mkdtemp())
    import server
    return server


ITEMS = [
    # 2026-03-01T02:00Z is still February 28 in UTC-3 (and March in UTC).
    {"id": "feb_edge", "media_type": "image", "prompt": "fox", "model": "m1", "created_at": "2026-03-01T02:00:00.5Z", "accounts": ["default"]},
    {"id": "feb", "media_type": "video", "prompt": "owl", "model": "m2", "created_at": "2026-02-10T12:00:00Z", "accounts": ["2172d829"]},
    {"id": "mar", "media_type": "image", "prompt": "fox", "model": "m1", "created_at": "2026-03-15T12:00:00Z", "accounts": ["default"]},
    {"id": "dec", "media_type": "image", "prompt": "cat", "model": "m1", "created_at": "2025-12-31T23:30:00Z", "accounts": ["default"]},
    {"id": "d10", "media_type": "image", "prompt": "fox", "model": "m1", "created_at": iso(NOW - dt.timedelta(days=10)), "accounts": ["default"]},
    {"id": "d45", "media_type": "image", "prompt": "fox", "model": "m2", "created_at": iso(NOW - dt.timedelta(days=45)), "accounts": ["2172d829"]},
    {"id": "d90", "media_type": "image", "prompt": "fox", "model": "m1", "created_at": iso(NOW - dt.timedelta(days=90)), "accounts": ["default"]},
    {"id": "nodate", "media_type": "image", "prompt": "fox", "model": "m1", "created_at": None},
]


def _write_library():
    server = _server()
    server.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for path in (server.COLLECTIONS_FILE, server.COLLECTION_GROUPS_FILE, server.LIBRARY_FILE,
                 server.DB_FILE, server.USER_TAGS_FILE):
        path.unlink(missing_ok=True)
    (server.DATA_DIR / "grok_collections.json").unlink(missing_ok=True)
    server._locked_cache["mtime"] = None
    server.METADATA_FILE.write_text(json.dumps([
        {**it, "local_path": f"media/images/aa/{it['id']}.jpg"} for it in ITEMS]), encoding="utf-8")
    server.USER_TAGS_FILE.write_text(json.dumps({"items": {"mar": ["keep"], "feb_edge": ["keep"]}}))
    server.rebuild_db(wait=True)
    c = server.app.test_client()
    with c.session_transaction() as sess:
        sess["authed"] = True
    return server, c


def ids(c, **params):
    r = c.get("/api/media", query_string={"view": "all", **params})
    assert r.status_code == 200, r.data
    return sorted(i["id"] for i in r.get_json()["items"])


def test_period_range_bounds_are_local_midnights_in_utc():
    server = _server()
    assert server._period_range("m:2026-02", -180) == ("2026-02-01T03:00:00", "2026-03-01T03:00:00")
    assert server._period_range("m:2026-12", 0) == ("2026-12-01", "2027-01-01")
    assert server._period_range("m:2026-13", 0) == (None, None)
    assert server._period_range("m:bogus", 0) == (None, None)
    s30, e30 = server._period_range("last30", 0)
    s60, _ = server._period_range("last60", 0)
    today = NOW.date()
    assert s30 == (today - dt.timedelta(days=29)).isoformat() and e30 == (today + dt.timedelta(days=1)).isoformat()
    assert s60 == (today - dt.timedelta(days=59)).isoformat()


def test_month_and_last_n_days_filters():
    _, c = _write_library()
    assert ids(c, period="m:2026-02", tz_offset=-180) == ["feb", "feb_edge"]
    assert ids(c, period="m:2026-02", tz_offset=0) == ["feb"]
    assert ids(c, period="m:2026-03", tz_offset=0) == ["feb_edge", "mar"]
    assert ids(c, period="m:2025-12", tz_offset=-180) == ["dec"]
    assert ids(c, period="m:2025-12", tz_offset=60) == []  # already Jan 1 at UTC+1
    assert ids(c, period="last30", tz_offset=-180) == ["d10"]
    assert ids(c, period="last60", tz_offset=-180) == ["d10", "d45"]
    assert "nodate" in ids(c) and "nodate" not in ids(c, period="last60")


def test_date_combines_with_other_filters_and_sort():
    _, c = _write_library()
    assert ids(c, period="last60", account="2172d829") == ["d45"]
    assert ids(c, period="last60", models="m1") == ["d10"]
    assert ids(c, period="m:2026-03", tz_offset=0, tags="keep") == ["feb_edge", "mar"]
    assert ids(c, period="m:2026-03", tz_offset=0, q="fox", type="image") == ["feb_edge", "mar"]
    r = c.get("/api/media", query_string={"view": "all", "period": "m:2026-03", "tz_offset": 0, "sort": "old"}).get_json()
    assert [i["id"] for i in r["items"]] == ["feb_edge", "mar"]


def test_months_facet_counts_local_months_and_ignores_the_date_filter():
    _, c = _write_library()
    f = c.get("/api/facets", query_string={"view": "all", "tz_offset": -180}).get_json()
    months = {m["month"]: m["count"] for m in f["months"]}
    assert months["2026-02"] == 2 and "2026-03" in months and months["2025-12"] == 1
    assert [m["month"] for m in f["months"]] == sorted(months, reverse=True)
    assert sum(months.values()) == len(ITEMS) - 1  # undated items have no month
    f0 = c.get("/api/facets", query_string={"view": "all", "tz_offset": 0}).get_json()
    assert {m["month"]: m["count"] for m in f0["months"]}["2026-03"] == 2
    # Picking a month keeps listing every month (it doesn't count its own dimension)…
    fm = c.get("/api/facets", query_string={"view": "all", "tz_offset": 0, "period": "m:2026-02"}).get_json()
    assert {m["month"] for m in fm["months"]} == {m["month"] for m in f0["months"]}
    # …but narrows the other facets, and the other filters narrow the months.
    assert sum(m["count"] for m in fm["models"]) == 1
    fa = c.get("/api/facets", query_string={"view": "all", "tz_offset": 0, "account": "2172d829"}).get_json()
    assert sum(m["count"] for m in fa["months"]) == 2
