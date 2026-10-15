"""Read-only external API helpers (API-key auth, localhost by default).

Endpoints live on the Flask app as ``/api/v1/...``. Keys are stored as SHA-256 hashes
in ``data/api_key.json``; the plaintext is shown once at generation time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import grokchats as gc

KEY_FILE = "api_key.json"
KEY_PREFIX = "gk_"
TAG_SAFE = re.compile(r"^[^/\x00-\x1f]{1,64}$")


def key_path(data_dir: Path) -> Path:
    return data_dir / KEY_FILE


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def load_key_record(data_dir: Path) -> dict | None:
    try:
        d = json.loads(key_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("hash"), str) or len(d["hash"]) != 64:
        return None
    return d


def generate_key(data_dir: Path) -> str:
    """Create a new key, persist only its hash, return the plaintext (shown once)."""
    raw = KEY_PREFIX + secrets.token_urlsafe(24)
    rec = {
        "version": 1,
        "hash": hash_key(raw),
        "prefix": raw[:7],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    path = key_path(data_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    tmp.replace(path)
    return raw


def revoke_key(data_dir: Path) -> bool:
    path = key_path(data_dir)
    if path.exists():
        path.unlink()
        return True
    return False


def verify_key(data_dir: Path, presented: str | None) -> bool:
    if not presented or not isinstance(presented, str):
        return False
    presented = presented.strip()
    if presented.lower().startswith("bearer "):
        presented = presented[7:].strip()
    rec = load_key_record(data_dir)
    if not rec:
        return False
    return hmac.compare_digest(rec["hash"], hash_key(presented))


def extract_key(headers) -> str | None:
    """From Flask request.headers: Authorization Bearer or X-API-Key."""
    x = headers.get("X-API-Key") or headers.get("x-api-key")
    if x:
        return x.strip()
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def is_loopback(addr: str | None) -> bool:
    if not addr:
        return False
    return addr in ("127.0.0.1", "::1", "localhost") or addr.startswith("127.")


# --------------------------------------------------------------------------- #
# Tag queries
# --------------------------------------------------------------------------- #

def list_tags(data_dir: Path, db_file: Path, chat_meta: dict | None = None) -> list[dict]:
    """Union of chat tags + media tags (all sources), with separate counts."""
    meta = chat_meta if chat_meta is not None else gc.load_meta(data_dir / "chat_meta.json")
    chat_counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for v in (meta.get("chats") or {}).values():
        for t in v.get("tags") or []:
            n = gc.norm_tag(t)
            if not n:
                continue
            low = n.lower()
            names.setdefault(low, n)
            chat_counts[low] = chat_counts.get(low, 0) + 1

    media_counts: dict[str, int] = {}
    if db_file.exists():
        con = sqlite3.connect(db_file)
        try:
            for tag, n in con.execute(
                "SELECT tag, count(DISTINCT media_id) FROM media_tags GROUP BY tag COLLATE NOCASE"
            ):
                ntag = gc.norm_tag(tag)
                if not ntag:
                    continue
                low = ntag.lower()
                names.setdefault(low, ntag)
                media_counts[low] = media_counts.get(low, 0) + int(n)
        finally:
            con.close()

    out = []
    for low, name in sorted(names.items(), key=lambda x: x[1].lower()):
        out.append({
            "name": name,
            "chats": chat_counts.get(low, 0),
            "media": media_counts.get(low, 0),
            "total": chat_counts.get(low, 0) + media_counts.get(low, 0),
        })
    return out


def _account_labels(accounts_file: Path) -> dict[str, str]:
    try:
        data = json.loads(accounts_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for e in data if isinstance(data, list) else []:
        if isinstance(e, dict) and e.get("id"):
            out[str(e["id"])] = str(e.get("name") or e["id"])
    return out


def query_chats_by_tag(
    data_dir: Path, tag: str, *, account: str | None = None, archived: str = "all",
    include: str = "summary", limit: int = 50, offset: int = 0,
    source_deleted: dict | None = None, srcdel: str = "all",
) -> dict:
    db = gc.ensure_index(data_dir)
    meta = gc.load_meta(data_dir / "chat_meta.json")
    labels = _account_labels(data_dir / "grok_accounts.json")
    res = gc.search(
        db, q="", account=account, sort="new", limit=limit, offset=offset,
        meta=meta, archived=archived, tags=[tag], tag_mode="any",
        source_deleted=source_deleted or {}, srcdel=srcdel,
    )
    items = []
    for it in res["items"]:
        entry = {
            "kind": "chat",
            "id": it["id"],
            "account": it["account"],
            "account_label": labels.get(it["account"], it["account"]),
            "title": it.get("title") or "",
            "create_time": it.get("create_time"),
            "modify_time": it.get("modify_time"),
            "url": f"https://grok.com/c/{it['id']}",
            "tags": it.get("tags") or [],
            "archived": bool(it.get("archived")),
            "source_deleted": it.get("source_deleted"),
            "n_messages": it.get("n_messages"),
        }
        if include == "full":
            doc = gc.load_conversation(data_dir, it["account"], it["id"]) or {}
            entry["messages"] = doc.get("messages") or []
        items.append(entry)
    return {"total": res["total"], "items": items}


def query_media_by_tag(
    db_file: Path, gallery_dir: Path, tag: str, *, account: str | None = None,
    limit: int = 50, offset: int = 0, base_url: str = "",
    accounts_file: Path | None = None, source_deleted: dict | None = None,
) -> dict:
    if not db_file.exists():
        return {"total": 0, "items": []}
    labels = _account_labels(accounts_file) if accounts_file else {}
    gone = source_deleted or {}
    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    try:
        params: list[Any] = [tag]
        acct_join = ""
        acct_where = ""
        if account:
            acct_join = " JOIN media_accounts ma ON ma.media_id = m.id"
            acct_where = " AND ma.account_id = ?"
            params.append(account)
        total = con.execute(
            f"""SELECT count(DISTINCT m.id) FROM media m
                JOIN media_tags t ON t.media_id = m.id{acct_join}
                WHERE t.tag = ? COLLATE NOCASE{acct_where}""",
            params,
        ).fetchone()[0]
        rows = con.execute(
            f"""SELECT m.* FROM media m
                JOIN media_tags t ON t.media_id = m.id{acct_join}
                WHERE t.tag = ? COLLATE NOCASE{acct_where}
                GROUP BY m.id
                ORDER BY m.created_at DESC
                LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()
        items = []
        for row in rows:
            d = dict(row)
            mid = d["id"]
            tags = [r[0] for r in con.execute(
                "SELECT tag FROM media_tags WHERE media_id = ? ORDER BY tag COLLATE NOCASE", (mid,))]
            accts = [r[0] for r in con.execute(
                "SELECT account_id FROM media_accounts WHERE media_id = ?", (mid,))]
            items.append(_shape_media(d, tags, accts, labels, gallery_dir, base_url, gone))
        return {"total": total, "items": items}
    finally:
        con.close()


def _shape_media(d: dict, tags: list, accts: list, labels: dict, gallery_dir: Path,
                 base_url: str, source_deleted: dict | None) -> dict:
    mid = d["id"]
    rel = (d.get("local_path") or "").replace("\\", "/")
    gone = (source_deleted or {}).get(mid)
    return {
        "kind": "media",
        "id": mid,
        "accounts": accts,
        "account_labels": [labels.get(a, a) for a in accts],
        "media_type": d.get("media_type"),
        "prompt": d.get("prompt") or "",
        "created_at": d.get("created_at"),
        "tags": tags,
        "local_path": str((gallery_dir / rel).resolve()) if rel else None,
        "local_rel": rel or None,
        "local_url": (base_url.rstrip("/") + "/media/" + quote(rel, safe="/")) if rel else None,
        "source_url": d.get("source_url"),
        "grok_url": f"https://grok.com/imagine/post/{mid}",
        "source_deleted": (gone or {}).get("at") if isinstance(gone, dict) else None,
        "width": d.get("media_w"),
        "height": d.get("media_h"),
        "model": d.get("model"),
    }


def media_detail(db_file: Path, gallery_dir: Path, media_id: str, *, base_url: str = "",
                 accounts_file: Path | None = None, source_deleted: dict | None = None) -> dict | None:
    if not db_file.exists() or not re.match(r"^[A-Za-z0-9_-]{1,128}$", media_id or ""):
        return None
    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        if not row:
            return None
        labels = _account_labels(accounts_file) if accounts_file else {}
        tags = [r[0] for r in con.execute(
            "SELECT tag FROM media_tags WHERE media_id = ? ORDER BY tag COLLATE NOCASE", (media_id,))]
        accts = [r[0] for r in con.execute(
            "SELECT account_id FROM media_accounts WHERE media_id = ?", (media_id,))]
        return _shape_media(dict(row), tags, accts, labels, gallery_dir, base_url, source_deleted)
    finally:
        con.close()
