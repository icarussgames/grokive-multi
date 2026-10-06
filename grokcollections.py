"""Grok Imagine collections -> auxiliary ``grok:<name>`` tags.

Grok lets you file posts into named collections on grok.com/imagine/saved. This step
lists one account's collections and each collection's posts, maps them onto media the
library ALREADY holds (nothing is downloaded), and writes the result to
``grok_collections.json`` in the data dir:

    {"version": 1, "accounts": {"<account id>": {
        "collections": {"<collection id>": {"name", "isDefault", "updateTime"}},
        "members": {"<collection id>": ["<media id>", ...]},
        "listed": {"<collection id>": <post ids listed>},
        "synced_at": "<UTC ISO>"}}}

Each run replaces the account's entry wholesale, so an item taken out of a collection on
Grok loses the tag on the next run. The index (db.build_index) turns members into
``media_tags`` rows with source ``grok``.

Endpoints: ``POST /rest/media/collection/list`` ``{}`` ->
``{"collections": [{id, name, isDefault, createTime, updateTime}]}`` (verified live
2026-10). The MEMBERSHIP request is NOT confirmed yet: ``post/list`` with
``filter.collectionId`` was shown live to ignore the filter (it returns a random feed, or
the liked list when ``source`` is LIKED). Its whole shape therefore lives in one place —
``MEMBERSHIP_REQUEST`` below — and can be overridden without a code change by a
``grok_collections_request.json`` in the data dir (same keys). Guards refuse to write
anything when the filter looks ignored (see ``filter_ignored_reason``). The step is
opt-in during Sync (Settings → Automation → "Tag Grok collections on Sync").

Run: python grokcollections.py --curl grok_auth.txt [--account <id>]   (cwd = data dir)
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

import gdownloader as g

GROK_COLLECTION_LIST_ENDPOINT = "https://grok.com/rest/media/collection/list"
# ---- Membership request: the ONE place to change once the real request is known --------
# Every string "{collection_id}" in `endpoint` / `body` is replaced with the collection's
# id. `cursor_field` is a dotted path in the body (POST) or a query param (GET) that gets
# the next-page cursor; `next_cursor_keys` are tried on each response (then gdownloader's
# top-level NEXT_KEYS); `posts_key` holds the page's posts. Override any subset with
# grok_collections_request.json in the data dir, e.g.
#   {"body": {"limit": 40, "source": "MEDIA_POST_SOURCE_X", "filter": {"collectionId": "{collection_id}"}}}
MEMBERSHIP_REQUEST: dict[str, Any] = {
    "method": "POST",
    "endpoint": g.GROK_FAVORITES_ENDPOINT,
    "body": {"limit": 40, "filter": {"collectionId": "{collection_id}"}},
    "cursor_field": "cursor",
    "next_cursor_keys": ["nextCursor", "next_cursor", "cursor"],
    "posts_key": "posts",
}
REQUEST_OVERRIDE_FILE = Path("grok_collections_request.json")
MAX_PAGES_PER_COLLECTION = 500
STATE_FILE = Path("grok_collections.json")
METADATA_FILE = Path("metadata.json")


def membership_request(override_path: Path | None = REQUEST_OVERRIDE_FILE) -> dict[str, Any]:
    """MEMBERSHIP_REQUEST with grok_collections_request.json (if any) merged over it."""
    req = json.loads(json.dumps(MEMBERSHIP_REQUEST))
    if override_path is not None and Path(override_path).exists():
        try:
            extra = json.loads(Path(override_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"collections: can't read {override_path}: {exc}")
        if isinstance(extra, dict):
            req.update({k: v for k, v in extra.items() if k in MEMBERSHIP_REQUEST})
            print(f"collections: using membership request overrides from {override_path}: "
                  f"{', '.join(sorted(k for k in extra if k in MEMBERSHIP_REQUEST))}")
    return req


def _fill(value: Any, collection_id: str) -> Any:
    if isinstance(value, str):
        return value.replace("{collection_id}", collection_id)
    if isinstance(value, dict):
        return {k: _fill(v, collection_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, collection_id) for v in value]
    return value


def _set_path(body: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    node = body
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value


def collection_list_spec(auth_spec: g.RequestSpec) -> g.RequestSpec:
    headers = dict(auth_spec.headers)
    headers["Content-Type"] = "application/json"
    return g.RequestSpec(method="POST", url=GROK_COLLECTION_LIST_ENDPOINT, headers=headers,
                         cookies=auth_spec.cookies, body="{}")


def collection_posts_spec(auth_spec: g.RequestSpec, collection_id: str, req: dict[str, Any],
                          cursor: str | None = None) -> g.RequestSpec:
    """One page of a collection's membership request, built from ``req``."""
    method = str(req.get("method") or "POST").upper()
    url = _fill(str(req["endpoint"]), collection_id)
    headers = dict(auth_spec.headers)
    field = str(req.get("cursor_field") or "cursor")
    if method == "GET":
        if cursor:
            url = url + ("&" if "?" in url else "?") + urlencode({field: cursor})
        return g.RequestSpec(method="GET", url=url, headers=headers, cookies=auth_spec.cookies, body=None)
    headers["Content-Type"] = "application/json"
    body = _fill(req.get("body") or {}, collection_id)
    if cursor:
        _set_path(body, field, cursor)
    return g.RequestSpec(method=method, url=url, headers=headers, cookies=auth_spec.cookies,
                         body=json.dumps(body, separators=(",", ":")))


def _page_posts(page: Any, req: dict[str, Any]) -> list[dict[str, Any]]:
    raw = page.get(str(req.get("posts_key") or "posts")) if isinstance(page, dict) else None
    return [p for p in raw or [] if isinstance(p, dict)]


def _next_cursor(page: Any, req: dict[str, Any]) -> str | None:
    if isinstance(page, dict):
        for key in req.get("next_cursor_keys") or []:
            val = page.get(key)
            if isinstance(val, (str, int)) and str(val):
                return str(val)
        # Top level only: a nested search could pick up a cursor-ish key inside a post.
        for key in g.NEXT_KEYS:
            val = page.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def iter_collection_pages(client: httpx.Client, auth_spec: g.RequestSpec, collection_id: str,
                          req: dict[str, Any], max_pages: int = MAX_PAGES_PER_COLLECTION):
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(max_pages):
        page = g.request_json_with_backoff(client, collection_posts_spec(auth_spec, collection_id, req, cursor))
        yield page
        cursor = _next_cursor(page, req)
        if not cursor or cursor in seen:
            return
        seen.add(cursor)
        time.sleep(0.3)


def first_page_ids(client: httpx.Client, auth_spec: g.RequestSpec, collection_id: str,
                   req: dict[str, Any]) -> list[str]:
    page = next(iter_collection_pages(client, auth_spec, collection_id, req, max_pages=1), None)
    return [str(p.get("id") or "") for p in _page_posts(page, req)]


def filter_ignored_reason(first_pages: dict[str, tuple[str, ...]], listed: dict[str, int]) -> str | None:
    """Why the membership listing looks like it ignored the collection filter, or None.
    post/list fails OPEN on filters it doesn't recognise, so these guard every write."""
    nonempty = {cid: ids for cid, ids in first_pages.items() if ids}
    if len(nonempty) >= 2 and len(set(nonempty.values())) == 1:
        return "every collection listed the same first page"
    # Three or more, so two small collections that happen to be the same size don't trip
    # it (with two, an ignored filter is already caught by the page / repeat checks).
    counts = list(listed.values())
    if len(counts) >= 3 and counts[0] > 0 and len(set(counts)) == 1:
        return f"every collection listed the same number of posts ({counts[0]})"
    return None


def list_collections(client: httpx.Client, auth_spec: g.RequestSpec) -> list[dict[str, Any]]:
    data = g.request_json_with_backoff(client, collection_list_spec(auth_spec))
    raw = data.get("collections") if isinstance(data, dict) else None
    out = []
    for c in raw or []:
        if isinstance(c, dict) and c.get("id"):
            out.append({
                "id": str(c["id"]),
                "name": str(c.get("name") or c["id"]).strip() or str(c["id"]),
                "isDefault": bool(c.get("isDefault")),
                "updateTime": str(c.get("updateTime") or ""),
            })
    return out


def post_candidate_ids(page: Any) -> list[str]:
    """Every id a listed post could be held under in the library: the records the
    favorites path would make from it (extract_grok_media_items — post, child posts,
    images, videos), every nested node id, and the asset id inside each media URL
    (conversation-archived media is keyed by asset id)."""
    ids: list[str] = [str(r["id"]) for r in g.extract_grok_media_items(page)]
    raw = page.get("posts") if isinstance(page, dict) else None
    for post in raw or []:
        if not isinstance(post, dict):
            continue
        for node, _parent in g.grok_post_and_children(post):
            node_id = g.first_value(node, g.ID_KEYS)
            if node_id:
                ids.append(str(node_id))
            for key in ("mediaUrl", "thumbnailImageUrl", "hdMediaUrl", "url"):
                val = node.get(key)
                if isinstance(val, str):
                    asset = g._asset_id_in_url(val)
                    if asset:
                        ids.append(asset)
    return list(dict.fromkeys(ids))


def list_collection_posts(client: httpx.Client, auth_spec: g.RequestSpec, collection_id: str,
                          library_ids: set[str], req: dict[str, Any]) -> tuple[list[str], int, int, list[str]]:
    """(held media ids, posts listed, posts with nothing in the library, first page's post
    ids) for one collection."""
    held: list[str] = []
    posts = missing = 0
    first_page: list[str] = []
    for page in iter_collection_pages(client, auth_spec, collection_id, req):
        listed = _page_posts(page, req)
        if not first_page:
            first_page = [str(p.get("id") or "") for p in listed]
        for post in listed:
            posts += 1
            mine = [i for i in post_candidate_ids({"posts": [post]}) if i in library_ids]
            if mine:
                held.extend(mine)
            else:
                missing += 1
    return list(dict.fromkeys(held)), posts, missing, first_page


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "accounts": {}}
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
        return {"version": 1, "accounts": {}}
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    g._atomic_write_text(path, json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False))


IGNORED_WARNING = ("WARNING: Grok seems to be IGNORING the collection filter ({reason}). "
                   "Nothing was written — the previous collection tags are kept. Fix the request "
                   "in MEMBERSHIP_REQUEST (grokcollections.py) or grok_collections_request.json.")


def sync_collections(client: httpx.Client, auth_spec: g.RequestSpec, library_ids: set[str],
                     account: str, state_path: Path = STATE_FILE, req: dict[str, Any] | None = None) -> int:
    """List, map and store one account's collections. Returns a process exit code."""
    req = req or json.loads(json.dumps(MEMBERSHIP_REQUEST))
    collections = list_collections(client, auth_spec)
    skipped_default = [c["name"] for c in collections if c["isDefault"]]
    wanted = [c for c in collections if not c["isDefault"]]
    print(f"collections: {len(collections)} on Grok"
          + (f" ({len(skipped_default)} default skipped: {', '.join(skipped_default)})" if skipped_default else ""))
    # Determinism probe: the same collection's first page, twice. A filter Grok ignores
    # falls back to a random/explore feed, which differs between two identical requests.
    if wanted:
        probe = wanted[0]
        a = first_page_ids(client, auth_spec, probe["id"], req)
        b = first_page_ids(client, auth_spec, probe["id"], req)
        if set(a) != set(b):
            print(IGNORED_WARNING.format(reason=f"the same request for '{probe['name']}' returned "
                                                f"different posts twice — a random feed"))
            return 1
    members: dict[str, list[str]] = {}
    listed: dict[str, int] = {}
    first_pages: dict[str, tuple[str, ...]] = {}
    missing_total = 0
    for c in wanted:
        held, posts, missing, first = list_collection_posts(client, auth_spec, c["id"], library_ids, req)
        members[c["id"]] = held
        listed[c["id"]] = posts
        if first:
            first_pages[c["id"]] = tuple(first)
        missing_total += missing  # posts none of whose ids are in the library
        print(f"collection '{c['name']}': {posts} post(s) listed, {len(held)} library item(s) tagged")
        time.sleep(0.5)
    reason = filter_ignored_reason(first_pages, listed)
    if reason:
        print(IGNORED_WARNING.format(reason=reason))
        return 1
    if missing_total:
        print(f"collections: {missing_total} listed post(s) aren't in the library yet "
              f"(not downloaded here — a Sync / Deep sync fetches media)")
    state = load_state(state_path)
    state["accounts"][account] = {
        "collections": {c["id"]: {"name": c["name"], "isDefault": c["isDefault"], "updateTime": c["updateTime"]}
                        for c in wanted},
        "members": members,
        "listed": listed,
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_state(state_path, state)
    tagged = len({i for ids in members.values() for i in ids})
    print(f"collections: {len(wanted)} collection(s) stored, {tagged} library item(s) tagged")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tag library items with their Grok Imagine collections (no downloads).")
    ap.add_argument("--curl", type=Path, default=Path("grok_auth.txt"))
    ap.add_argument("--account", default=None,
                    help="Account id the collections belong to (default: derived from --curl).")
    ap.add_argument("--metadata", type=Path, default=METADATA_FILE)
    ap.add_argument("--state", type=Path, default=STATE_FILE)
    ap.add_argument("--request", type=Path, default=REQUEST_OVERRIDE_FILE,
                    help="JSON overrides for the membership request (default: grok_collections_request.json if present).")
    args = ap.parse_args(argv)
    req = membership_request(args.request)
    account = (args.account or "").strip() or g.account_from_curl_path(args.curl)
    curl_path = args.curl
    if not curl_path.exists() and curl_path.with_name("curl_samples.txt").exists():
        curl_path = curl_path.with_name("curl_samples.txt")
    auth_spec = g.choose_grok_auth_spec(g.parse_curl_samples(curl_path))
    library_ids = {str(r.get("id")) for r in g.load_metadata(args.metadata) if isinstance(r, dict) and r.get("id")}
    with httpx.Client(follow_redirects=True) as client:
        try:
            return sync_collections(client, auth_spec, library_ids, account, args.state, req)
        except httpx.HTTPStatusError as exc:
            print(f"collections: Grok answered HTTP {exc.response.status_code} — skipped "
                  f"({'session expired? Check auth in Config' if exc.response.status_code in (401, 403) else 'try again later'})")
            return 1
        except httpx.HTTPError as exc:
            print(f"collections: network error ({type(exc).__name__}) — skipped")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
