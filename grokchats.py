"""Grok chat (text conversation) backup + search index.

Grokive's media sync only walks *Imagine* conversations (kind=CONVERSATION_KIND_IMAGINE).
This module archives the account's ordinary Grok chats so old conversations can be found
again: one JSON file per conversation under ``chats/<account>/<id>.json`` (relative to
GROK_DATA_DIR) and a SQLite FTS5 index (``chats.db``) over titles + message text.

Reads only, all GET (the POST forms of the chat endpoints are refused by Grok's anti-bot
rules — see gdownloader.GROK_CHAT_CONVERSATIONS_ENDPOINT), reusing the per-account cURL
session files the media sync already uses.

  python grokchats.py sync  --curl grok_accounts/<id>.txt --account <id> [--deep]
  python grokchats.py probe --curl grok_accounts/<id>.txt --account <id>
  python grokchats.py index

Incremental like Imagine conversations: per account, ``chat_state.json`` records the
modifyTime each conversation had when last archived; unchanged ones are not re-read and
the listing stops early once a whole page is unchanged. --deep re-reads everything.

The API shape is handled defensively (several key spellings, missing fields); run
``probe`` once to dump raw responses to ``chat_probe_<account>.json`` for verification.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import gdownloader as g

CHATS_DIR = Path("chats")
STATE_FILE = Path("chat_state.json")
INDEX_FILE = Path("chats.db")
STAMP_NAME = ".stamp"  # touched after every write so the server knows to rebuild its index
PAGE_SIZE = 60
SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _request(client, spec):  # seam for tests
    return g.request_json_with_backoff(client, spec)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

def list_spec(auth: g.RequestSpec, token: str | None, page_size: int = PAGE_SIZE) -> g.RequestSpec:
    # No `kind` → Grok's default listing = normal chats (Imagine ones are hidden from it).
    query = {"pageSize": str(page_size)}
    if token:
        query["pageToken"] = token
    return g._grok_get_spec(auth, f"{g.GROK_CHAT_CONVERSATIONS_ENDPOINT}?{urlencode(query)}")


def responses_spec(auth: g.RequestSpec, conv_id: str) -> g.RequestSpec:
    return g._grok_get_spec(
        auth, f"{g.GROK_CHAT_CONVERSATIONS_ENDPOINT}/{quote(conv_id, safe='')}/responses?includeThreads=true")


def response_node_spec(auth: g.RequestSpec, conv_id: str) -> g.RequestSpec:
    return g._grok_get_spec(
        auth, f"{g.GROK_CHAT_CONVERSATIONS_ENDPOINT}/{quote(conv_id, safe='')}/response-node?includeThreads=true")


def load_responses_spec(auth: g.RequestSpec, conv_id: str, ids: list[str]) -> g.RequestSpec:
    headers = dict(auth.headers)
    headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
    headers["Content-Type"] = "application/json"
    return g.RequestSpec(
        method="POST",
        url=f"{g.GROK_CHAT_CONVERSATIONS_ENDPOINT}/{quote(conv_id, safe='')}/load-responses",
        headers=headers, cookies=auth.cookies, body=json.dumps({"responseIds": ids}))


# --------------------------------------------------------------------------- #
# Parsing (defensive about key spellings)
# --------------------------------------------------------------------------- #

def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _time(v: Any) -> str | None:
    """ISO string from an ISO string, epoch seconds/ms, or {seconds: N} proto timestamp."""
    if isinstance(v, dict):
        v = v.get("seconds") or v.get("epochSeconds")
    if isinstance(v, (int, float)) and v > 0:
        if v > 1e12:
            v = v / 1000
        return datetime.datetime.fromtimestamp(v, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(v, str) and v.strip():
        s = v.strip()
        if s.isdigit():
            return _time(int(s))
        return s
    return None


def parse_conversation_entry(c: dict) -> dict | None:
    if not isinstance(c, dict):
        return None
    cid = str(_first(c, "conversationId", "id", "conversation_id") or "")
    if not cid or not SAFE_ID.match(cid):
        return None
    return {
        "id": cid,
        "title": str(_first(c, "title", "name") or "").strip(),
        "create_time": _time(_first(c, "createTime", "createdAt", "create_time", "created_at")),
        "modify_time": _time(_first(c, "modifyTime", "updateTime", "modifiedAt", "updatedAt", "modify_time")),
        "starred": bool(c.get("starred")),
    }


def _list_from(data: Any, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
        # One level of wrapping ({"result": {...}} / {"data": {...}})
        for k in ("result", "data"):
            if isinstance(data.get(k), dict):
                return _list_from(data[k], *keys)
    return []


def _sender(r: dict) -> str:
    s = str(_first(r, "sender", "role", "author") or "").lower()
    if s in ("human", "user") or s.endswith("human") or s.endswith("user"):
        return "user"
    if s in ("system",):
        return "system"
    return "assistant"


_IMG_LIST_KEYS = ("generatedImageUrls", "imageAttachments", "fileAttachments", "fileUris", "imageUrls")


def _attachments(r: dict) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, ref: str, name: str | None = None, mime: str | None = None) -> None:
        if not ref or ref in seen:
            return
        seen.add(ref)
        url = ref if ref.startswith("http") else (g.GROK_ASSETS_BASE + ref.lstrip("/") if "/" in ref else None)
        out.append({"kind": kind, "ref": ref, "url": url, "name": name, "mime": mime})

    for meta in r.get("fileAttachmentAssetMetadata") or r.get("fileAttachmentsMetadata") or []:
        if isinstance(meta, dict):
            ref = str(meta.get("key") or meta.get("assetId") or meta.get("fileMetadataId") or meta.get("id") or "")
            add("file", ref, meta.get("name") or meta.get("fileName"), meta.get("mimeType") or meta.get("fileMimeType"))
    for key in _IMG_LIST_KEYS:
        for v in r.get(key) or []:
            if isinstance(v, str):
                add("generated" if key == "generatedImageUrls" else "file", v)
            elif isinstance(v, dict):
                ref = str(v.get("url") or v.get("key") or v.get("id") or "")
                add("file", ref, v.get("name"), v.get("mimeType"))
    return out


def _text(r: dict) -> str:
    v = _first(r, "message", "text", "content", "query", "body")
    if isinstance(v, list):  # [{"type":"text","text":..}]
        v = "\n".join(str(p.get("text") or "") if isinstance(p, dict) else str(p) for p in v)
    if isinstance(v, dict):
        v = v.get("text") or v.get("content") or ""
    return str(v or "")


def parse_responses(data: Any) -> list[dict]:
    """Ordered message list from a /responses or /load-responses payload."""
    msgs = []
    for i, r in enumerate(_list_from(data, "responses", "messages", "items")):
        if not isinstance(r, dict):
            continue
        m = {
            "id": str(_first(r, "responseId", "id") or i),
            "parent_id": _first(r, "parentResponseId", "parentId"),
            "sender": _sender(r),
            "text": _text(r),
            "time": _time(_first(r, "createTime", "createdAt", "timestamp", "time")),
            "model": _first(r, "model", "modelName"),
            "attachments": _attachments(r),
        }
        if r.get("partial") or r.get("isPartial"):
            m["partial"] = True
        msgs.append((m["time"] or "", i, m))
    msgs.sort(key=lambda t: (t[0], t[1]))
    return [m for _, _, m in msgs]


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def list_entries(client, auth, max_pages=None, stop_after_page=None) -> tuple[list[dict], bool]:
    out: list[dict] = []
    seen: set[str] = set()
    tokens: set[str] = set()
    token = None
    pages = 0
    while True:
        data = _request(client, list_spec(auth, token))
        page = []
        for c in _list_from(data, "conversations", "items"):
            e = parse_conversation_entry(c)
            if e and e["id"] not in seen:
                seen.add(e["id"])
                page.append(e)
        out.extend(page)
        pages += 1
        token = _first(data, "nextPageToken", "next_page_token", "pageToken", "cursor") if isinstance(data, dict) else None
        if not token or token in tokens:
            return out, True
        if max_pages is not None and pages >= max_pages:
            return out, False
        if stop_after_page is not None and stop_after_page(page):
            return out, False
        tokens.add(token)
        time.sleep(0.5)


def fetch_messages(client, auth, conv_id: str) -> list[dict]:
    """GET /responses first (the form the media sync already relies on); if that yields
    nothing, fall back to /response-node + POST /load-responses."""
    first_exc: Exception | None = None
    try:
        msgs = parse_responses(_request(client, responses_spec(auth, conv_id)))
        if msgs:
            return msgs
    except Exception as exc:  # noqa: BLE001
        first_exc = exc
    try:
        nodes = _list_from(_request(client, response_node_spec(auth, conv_id)), "responseNodes", "nodes")
        ids = [str(n.get("responseId") or n.get("id")) for n in nodes if isinstance(n, dict) and (n.get("responseId") or n.get("id"))]
        if ids:
            return parse_responses(_request(client, load_responses_spec(auth, conv_id, ids)))
    except Exception as exc:  # noqa: BLE001
        if first_exc is None:
            first_exc = exc
    if first_exc is not None:
        raise first_exc
    return []


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

def _safe_account(account: str) -> str:
    a = re.sub(r"[^A-Za-z0-9_-]", "_", account or "default")
    return a or "default"


def conv_path(root: Path, account: str, conv_id: str) -> Path:
    return root / CHATS_DIR / _safe_account(account) / f"{conv_id}.json"


def touch_stamp(root: Path) -> None:
    p = root / CHATS_DIR / STAMP_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(time.time()), encoding="utf-8")


def save_conversation(root: Path, account: str, entry: dict, messages: list[dict]) -> Path:
    path = conv_path(root, account, entry["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "id": entry["id"], "account": account,
        "title": entry.get("title") or "",
        "create_time": entry.get("create_time") or (messages[0]["time"] if messages else None),
        "modify_time": entry.get("modify_time") or (messages[-1]["time"] if messages else None),
        "starred": bool(entry.get("starred")),
        "url": f"https://grok.com/c/{entry['id']}",
        "archived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "messages": messages,
    }
    g._atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=1))
    return path


def load_state(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("accounts"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"version": 1, "accounts": {}}


def sync_account(client, auth, root: Path, account: str, deep: bool = False,
                 max_pages: int | None = None, log=print) -> dict:
    state_path = root / STATE_FILE
    state = load_state(state_path)
    acct = state["accounts"].get(account) or {}
    known: dict[str, str] = {str(k): str(v) for k, v in (acct.get("conversations") or {}).items()}
    pending: set[str] = set(acct.get("pending") or [])
    # Lost files (fresh restore) → don't trust state.
    if known and not (root / CHATS_DIR / _safe_account(account)).is_dir():
        log(f"chats: no archived files for '{account}' — ignoring saved state")
        known, pending = {}, set()
    incremental = bool(known) and not deep
    stop = g._make_early_stop(known, pending) if incremental else None
    entries, complete = list_entries(client, auth, max_pages, stop)
    log(f"found {len(entries)} chat conversation(s)" + ("" if complete else " (listing stopped early — rest unchanged)"))

    def unchanged(e):
        return incremental and e["id"] not in pending and g._conversation_unchanged(e, known) \
            and conv_path(root, account, e["id"]).exists()

    todo = [e for e in entries if not unchanged(e)]
    log(f"chats: {len(todo)} new/changed, {len(entries) - len(todo)} unchanged skipped")
    saved = failed = 0
    try:
        for n, e in enumerate(todo, 1):
            try:
                msgs = fetch_messages(client, auth, e["id"])
            except Exception as exc:  # noqa: BLE001 - one bad chat must not end the sync
                failed += 1
                pending.add(e["id"])
                known.pop(e["id"], None)
                log(f"chat {e['id']}: failed ({str(exc)[:160]})")
                continue
            save_conversation(root, account, e, msgs)
            saved += 1
            pending.discard(e["id"])
            if e.get("modify_time"):
                known[e["id"]] = e["modify_time"]
            if n % 25 == 0:
                log(f"chats: {n}/{len(todo)} read")
                _save_state(state_path, state, account, known, pending, entries if complete else None)
    finally:
        _save_state(state_path, state, account, known, pending, entries if complete else None)
        touch_stamp(root)
    log(f"chats: saved {saved}, failed {failed}")
    return {"listed": len(entries), "saved": saved, "failed": failed, "complete": complete}


def _save_state(path, state, account, known, pending, full_listing) -> None:
    if full_listing is not None:
        listed = {e["id"] for e in full_listing}
        known = {k: v for k, v in known.items() if k in listed}
        pending = pending & listed
    state["accounts"][account] = {"conversations": known, "pending": sorted(pending)}
    g._atomic_write_text(path, json.dumps(state, indent=1, sort_keys=True))


def probe(client, auth, root: Path, account: str, log=print) -> Path:
    """Dump one listing page + the first conversation's raw message endpoints."""
    out: dict[str, Any] = {"account": account, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def grab(name, spec):
        try:
            out[name] = _request(client, spec)
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": str(exc)[:500]}
        return out[name]

    listing = grab("list", list_spec(auth, None, 5))
    entries = [e for e in (parse_conversation_entry(c) for c in _list_from(listing, "conversations", "items")) if e]
    out["parsed_entries"] = entries
    if entries:
        cid = entries[0]["id"]
        grab("responses", responses_spec(auth, cid))
        nodes = grab("response_node", response_node_spec(auth, cid))
        ids = [str(n.get("responseId")) for n in _list_from(nodes, "responseNodes", "nodes") if isinstance(n, dict) and n.get("responseId")]
        if ids:
            grab("load_responses", load_responses_spec(auth, cid, ids[:5]))
        try:
            out["parsed_messages"] = fetch_messages(client, auth, cid)
        except Exception as exc:  # noqa: BLE001
            out["parsed_messages"] = {"error": str(exc)[:500]}
    path = root / f"chat_probe_{_safe_account(account)}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"probe written to {path}")
    log(f"  listed: {len(entries)}; messages parsed: "
        f"{len(out['parsed_messages']) if isinstance(out.get('parsed_messages'), list) else out.get('parsed_messages')}")
    return path


# --------------------------------------------------------------------------- #
# Search index (SQLite FTS5)
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS conv(
  id TEXT, account TEXT, title TEXT, create_time TEXT, modify_time TEXT, sort_time TEXT,
  n_messages INTEGER, starred INTEGER, first_prompt TEXT, PRIMARY KEY(account, id));
CREATE INDEX IF NOT EXISTS conv_sort ON conv(sort_time);
CREATE VIRTUAL TABLE IF NOT EXISTS chat_fts USING fts5(
  title, body, conv_id UNINDEXED, account UNINDEXED, msg_idx UNINDEXED, sender UNINDEXED,
  tokenize='unicode61 remove_diacritics 2');
"""


def iter_docs(root: Path):
    base = root / CHATS_DIR
    if not base.is_dir():
        return
    for acct_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for f in acct_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and d.get("id"):
                d.setdefault("account", acct_dir.name)
                yield d


def build_index(root: Path, db_path: Path | None = None) -> int:
    db_path = db_path or root / INDEX_FILE
    tmp = db_path.with_suffix(".db.tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    n = 0
    for d in iter_docs(root):
        msgs = [m for m in d.get("messages") or [] if isinstance(m, dict)]
        first = next((m.get("text") or "" for m in msgs if m.get("sender") == "user"), "")
        ct, mt = d.get("create_time"), d.get("modify_time")
        con.execute("INSERT OR REPLACE INTO conv VALUES(?,?,?,?,?,?,?,?,?)",
                    (d["id"], d["account"], d.get("title") or "", ct, mt, ct or mt or "",
                     len(msgs), int(bool(d.get("starred"))), first[:400]))
        con.execute("INSERT INTO chat_fts VALUES(?,?,?,?,?,?)", (d.get("title") or "", "", d["id"], d["account"], -1, ""))
        for i, m in enumerate(msgs):
            if m.get("text"):
                con.execute("INSERT INTO chat_fts VALUES(?,?,?,?,?,?)",
                            ("", m["text"], d["id"], d["account"], i, m.get("sender") or ""))
        n += 1
    con.commit()
    con.close()
    os.replace(tmp, db_path)
    return n


def ensure_index(root: Path) -> Path:
    """Rebuild chats.db when any archive write happened after it was built."""
    db = root / INDEX_FILE
    stamp = root / CHATS_DIR / STAMP_NAME
    if not db.exists() or (stamp.exists() and stamp.stat().st_mtime > db.stat().st_mtime):
        build_index(root, db)
    return db


def fts_query(q: str) -> str:
    """User text → safe FTS5 query: every word must match (prefix), "quoted phrases" kept."""
    parts = []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', q or ""):
        if phrase:
            toks = re.findall(r"\w+", phrase)
            if toks:
                parts.append('"' + " ".join(toks) + '"')
        else:
            for t in re.findall(r"\w+", word):
                parts.append(f'"{t}"*')
    return " ".join(parts)


HL_OPEN, HL_CLOSE = "\u0001", "\u0002"


def search(db_path: Path, q: str = "", account: str | None = None, start: str | None = None,
           end: str | None = None, sort: str = "new", limit: int = 50, offset: int = 0) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    where, args = [], []
    if account:
        where.append("c.account = ?"); args.append(account)
    if start:
        where.append("c.sort_time >= ?"); args.append(start)
    if end:
        where.append("c.sort_time < ?"); args.append(end)
    order = {"old": "c.sort_time ASC", "title": "lower(c.title) ASC", "updated": "c.modify_time DESC"}.get(sort, "c.sort_time DESC")
    fq = fts_query(q)
    try:
        if fq:
            hits = con.execute(
                f"""SELECT conv_id, account, msg_idx, sender,
                       snippet(chat_fts, -1, ?, ?, '…', 14) AS snip, bm25(chat_fts) AS rank
                    FROM chat_fts WHERE chat_fts MATCH ? ORDER BY rank LIMIT 5000""",
                (HL_OPEN, HL_CLOSE, fq)).fetchall()
            by_conv: dict[tuple, list] = {}
            for h in hits:
                by_conv.setdefault((h["account"], h["conv_id"]), []).append(
                    {"msg_idx": h["msg_idx"], "sender": h["sender"], "snippet": h["snip"]})
            if not by_conv:
                return {"total": 0, "items": []}
            con.execute("CREATE TEMP TABLE hit(account TEXT, id TEXT)")
            con.executemany("INSERT INTO hit VALUES(?,?)", list(by_conv))
            base = f"FROM conv c JOIN hit h ON h.account=c.account AND h.id=c.id {'WHERE ' + ' AND '.join(where) if where else ''}"
        else:
            by_conv = {}
            base = f"FROM conv c {'WHERE ' + ' AND '.join(where) if where else ''}"
        total = con.execute(f"SELECT count(*) {base}", args).fetchone()[0]
        rows = con.execute(f"SELECT c.* {base} ORDER BY {order} LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        items = []
        for r in rows:
            it = dict(r)
            if by_conv:
                hs = by_conv.get((r["account"], r["id"]), [])
                it["hits"] = len(hs)
                it["snippets"] = hs[:3]
            items.append(it)
        return {"total": total, "items": items}
    finally:
        con.close()


def months(db_path: Path, account: str | None = None, tz_offset_min: int = 0) -> list[dict]:
    con = sqlite3.connect(db_path)
    try:
        sql = ("SELECT strftime('%Y-%m', sort_time, ?) m, count(*) FROM conv WHERE sort_time != ''"
               + (" AND account = ?" if account else "") + " GROUP BY m ORDER BY m DESC")
        args = [f"{int(tz_offset_min):+d} minutes"] + ([account] if account else [])
        return [{"month": m, "count": c} for m, c in con.execute(sql, args) if m]
    finally:
        con.close()


def load_conversation(root: Path, account: str, conv_id: str) -> dict | None:
    if not SAFE_ID.match(conv_id or ""):
        return None
    p = conv_path(root, account, conv_id)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _auth(curl: Path) -> g.RequestSpec:
    if not curl.exists() and curl.with_name("curl_samples.txt").exists():
        curl = curl.with_name("curl_samples.txt")
    return g.choose_grok_auth_spec(g.parse_curl_samples(curl))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Back up and index normal Grok chats.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("sync", "probe"):
        sp = sub.add_parser(name)
        sp.add_argument("--curl", type=Path, default=Path("grok_auth.txt"))
        sp.add_argument("--account", default=None)
        if name == "sync":
            sp.add_argument("--deep", action="store_true")
            sp.add_argument("--max-pages", type=int, default=None)
    sub.add_parser("index")
    a = p.parse_args(argv)
    root = Path(".")
    if a.cmd == "index":
        print(f"indexed {build_index(root)} chat conversation(s)")
        return 0
    account = (a.account or "").strip() or g.account_from_curl_path(a.curl)
    auth = _auth(a.curl)
    with g.httpx.Client(follow_redirects=True) as client:
        if a.cmd == "probe":
            probe(client, auth, root, account)
            return 0
        res = sync_account(client, auth, root, account, deep=a.deep, max_pages=a.max_pages)
    n = build_index(root)
    print(f"chat index: {n} conversation(s)")
    return 1 if res["failed"] and not res["saved"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
