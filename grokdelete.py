"""Delete items on grok.com (the source account) — chats and Imagine media.

Off unless Config → "Allow deleting on grok.com" is enabled (server enforces). Uses the
same per-account cURL session files as sync. Local backup copies are never removed; the
server records each successful delete in ``source_deleted.json`` (which sync never writes).

Endpoints (as used by grok.com's own web client / public references, Oct 2026):
  chat  : DELETE https://grok.com/rest/app-chat/conversations/soft/<id>   (what the UI's
          "Delete" sends; Grok purges soft-deleted chats later). ``hard=True`` uses
          DELETE /rest/app-chat/conversations/<id> (the older permanent delete).
  media : POST   https://grok.com/rest/media/post/delete  {"id": "<post id>"}
A 404 counts as "already gone" (idempotent, like Grok's own client).

Probe one throwaway item first:  python server.py --probe-delete <account> <chat|media> <id> [--execute]
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable
from urllib.parse import quote

import gdownloader as g

CHAT_SOFT = g.GROK_CHAT_CONVERSATIONS_ENDPOINT + "/soft/{id}"
CHAT_HARD = g.GROK_CHAT_CONVERSATIONS_ENDPOINT + "/{id}"
MEDIA_DELETE = "https://grok.com/rest/media/post/delete"
DELAY_S = 1.2  # between requests: sequential + gentle


def build_spec(auth: g.RequestSpec, kind: str, item_id: str, hard: bool = False) -> g.RequestSpec:
    if not g.UUID_RE.match(item_id or "") and not (item_id or "").replace("-", "").isalnum():
        raise ValueError(f"bad id {item_id!r}")
    headers = {k: v for k, v in auth.headers.items() if k.lower() not in ("content-type", "content-length")}
    if kind == "chat":
        url = (CHAT_HARD if hard else CHAT_SOFT).format(id=quote(item_id, safe=""))
        return g.RequestSpec(method="DELETE", url=url, headers=headers, cookies=auth.cookies, body=None)
    if kind == "media":
        headers["Content-Type"] = "application/json"
        return g.RequestSpec(method="POST", url=MEDIA_DELETE, headers=headers, cookies=auth.cookies,
                             body=json.dumps({"id": item_id}))
    raise ValueError(f"unknown kind {kind!r}")


def _send(client, spec: g.RequestSpec):  # seam for tests
    headers = {k: v for k, v in spec.headers_with_cookies().items() if k.lower() != "accept-encoding"}
    return client.request(spec.method, spec.url, headers=headers, content=spec.body, timeout=60)


def delete_one(client, spec: g.RequestSpec, retries: int = 4) -> dict[str, Any]:
    """{ok, status, already, body} — 2xx ok, 404 ok+already, 429 backs off and retries."""
    delay = 3.0
    for attempt in range(retries + 1):
        try:
            r = _send(client, spec)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": None, "error": str(exc)[:300]}
        status = r.status_code
        text = (r.text or "")[:500]
        if status == 429 and attempt < retries:
            time.sleep(delay); delay *= 2
            continue
        if 200 <= status < 300:
            return {"ok": True, "status": status, "body": text}
        if status == 404:
            return {"ok": True, "already": True, "status": status, "body": text}
        hint = " (session expired? re-paste the account's cURL)" if status in (401, 403) else ""
        return {"ok": False, "status": status, "body": text, "error": f"HTTP {status}{hint}"}
    return {"ok": False, "status": 429, "error": "rate limited"}


def run_batch(client, jobs: list[dict], auth_for: Callable[[str], g.RequestSpec], hard: bool = False,
              on_result: Callable[[dict], None] = lambda r: None, should_stop=lambda: False,
              delay: float = DELAY_S) -> list[dict]:
    """jobs: [{kind, id, accounts: [candidate account ids]}]. Tries each candidate account in
    turn until one succeeds (media can be listed under several accounts). Sequential."""
    out = []
    first = True
    for job in jobs:
        if should_stop():
            res = {**job, "ok": False, "error": "cancelled"}
            out.append(res); on_result(res); continue
        res: dict[str, Any] = {"kind": job["kind"], "id": job["id"], "ok": False}
        cands = job.get("accounts") or []
        if not cands:
            res["error"] = "no Grok account known for this item"
        tried = []
        for acct in cands:
            if not first:
                time.sleep(delay)
            first = False
            try:
                spec = build_spec(auth_for(acct), job["kind"], job["id"], hard)
            except Exception as exc:  # noqa: BLE001 - missing/broken session file
                tried.append({"account": acct, "error": str(exc)[:200]})
                continue
            r = delete_one(client, spec)
            tried.append({"account": acct, **r})
            if r["ok"] and not (r.get("already") and len(cands) > 1 and acct != cands[-1]):
                res.update(ok=True, account=acct, status=r["status"], already=bool(r.get("already")))
                break
            if r.get("already"):
                # 404 under one account may just mean "not this account's post" — try the next,
                # but remember it in case nobody else has it either.
                res.update(account=acct, status=404, already=True)
        else:
            if res.get("already"):
                res["ok"] = True
            elif cands:
                last = tried[-1] if tried else {}
                res["error"] = last.get("error") or f"HTTP {last.get('status')}"
        res["attempts"] = tried
        out.append(res)
        on_result(res)
    return out
